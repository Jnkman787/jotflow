"""A scripted stand-in for the OpenAI Responses API, for checking Codex at no cost (#22).

It records each request Codex sends (the model, instructions, and the tools it
offers) and answers with scripted streamed output: first a tool call, such as
an ``apply_patch`` that tries to write a file, then a final message once the
tool's result comes back. Tests read the recorded requests to see exactly which
tools Codex would give a real model, and what each tool call returned.

    with FakeResponsesApi(first=apply_patch_call()) as api:
        ...  # point Codex at api.base_url as a custom model provider
        api.requests  # what Codex sent
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

# A patch that would create a file in the working directory.
PROBE_FILE = "jotflow-probe.txt"
PROBE_PATCH = f"*** Begin Patch\n*** Add File: {PROBE_FILE}\n+written\n*** End Patch\n"
TOOL_OUTPUTS = ("custom_tool_call_output", "function_call_output")


def apply_patch_call(patch: str = PROBE_PATCH) -> dict[str, Any]:
    """An output item calling Codex's freeform ``apply_patch`` tool."""
    return {
        "type": "custom_tool_call",
        "id": "ctc_probe",
        "call_id": "call_probe",
        "name": "apply_patch",
        "input": patch,
        "status": "completed",
    }


def final_message(text: str = "done") -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_done",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _event(name: str, data: Mapping[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(dict(data))}\n\n".encode()


class FakeResponsesApi:
    """Serves ``POST /v1/responses`` on 127.0.0.1 in a background thread.

    Every request's method and path is kept in :attr:`calls`, so a test can show
    Codex asked for nothing else, such as a model list (#290). Anything else gets 404.
    """

    def __init__(self, first: Mapping[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._first = dict(first) if first is not None else final_message()
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _elsewhere(self) -> None:
                api.calls.append(f"{self.command} {self.path}")
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

            do_GET = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _elsewhere

            def do_POST(self) -> None:
                api.calls.append(f"POST {self.path}")
                if self.path != "/v1/responses":
                    self._elsewhere_after_post()
                    return
                size = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                api.requests.append(body)
                answered = any(i.get("type") in TOOL_OUTPUTS for i in body.get("input", []))
                item = final_message() if answered else api._first
                rid = "resp_" + uuid.uuid4().hex[:8]
                usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
                payload = b"".join(
                    [
                        _event(
                            "response.created",
                            {"type": "response.created", "response": {"id": rid}},
                        ),
                        _event(
                            "response.output_item.done",
                            {"type": "response.output_item.done", "item": item},
                        ),
                        _event(
                            "response.completed",
                            {"type": "response.completed", "response": {"id": rid, "usage": usage}},
                        ),
                    ]
                )
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _elsewhere_after_post(self) -> None:
                size = int(self.headers.get("content-length") or 0)
                self.rfile.read(size)
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def builtin_overrides(self) -> list[str]:
        """``codex exec -c`` arguments that point OpenAI's own built-in provider here,
        as a deployment uses it, with the key in ``CODEX_API_KEY``. For tests only."""
        return ["-c", f'openai_base_url="{self.base_url}"']

    def provider_overrides(self, env_key: str = "FAKE_MODEL_KEY") -> list[str]:
        """``codex exec -c`` arguments that route Codex's model calls here, for tests only."""
        provider = (
            f'{{name="fake",base_url="{self.base_url}",wire_api="responses",env_key="{env_key}"}}'
        )
        return ["-c", 'model_provider="fake"', "-c", f"model_providers.fake={provider}"]

    def offered_tools(self) -> list[str]:
        """The tools Codex offered the model in its first request, by name or type."""
        if not self.requests:
            return []
        return [str(t.get("name") or t.get("type")) for t in self.requests[0].get("tools", [])]

    def tool_outputs(self) -> list[str]:
        """Every tool result Codex sent back, as text."""
        return [
            str(item.get("output"))
            for request in self.requests
            for item in request.get("input", [])
            if item.get("type") in TOOL_OUTPUTS
        ]

    def __enter__(self) -> FakeResponsesApi:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
