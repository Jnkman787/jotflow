"""A scripted stand-in for Anthropic's Messages API, for checking Copilot at no cost (#288).

It records each request (the model, the messages, and the tools offered) and
answers with scripted streamed events: first a ``tool_use`` block, such as a
``bash`` call that tries to write a file, then a text reply once the tool's
result comes back. Tests read the recorded requests to see exactly which tools
the CLI would give a real model, and what each tool call returned.

    with FakeMessagesApi(first=tool_use("bash", {"command": "true"})) as api:
        ...  # point the CLI at api.base_url as its Anthropic provider
        api.requests  # what it sent
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

USAGE = {"input_tokens": 1, "output_tokens": 1}


def tool_use(name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A content block calling the tool ``name``."""
    return {"type": "tool_use", "id": "toolu_probe", "name": name, "input": dict(arguments or {})}


def text(reply: str = "done") -> dict[str, Any]:
    return {"type": "text", "text": reply}


def _event(name: str, **data: Any) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n".encode()


def _stream(model: object, block: Mapping[str, Any]) -> bytes:
    """The real event sequence for a message holding one content block."""
    message = {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": USAGE,
    }
    if block["type"] == "tool_use":
        start = {**block, "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        stop = "tool_use"
    else:
        start = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": block["text"]}
        stop = "end_turn"
    return b"".join(
        [
            _event("message_start", message=message),
            _event("content_block_start", index=0, content_block=start),
            _event("content_block_delta", index=0, delta=delta),
            _event("content_block_stop", index=0),
            _event(
                "message_delta",
                delta={"stop_reason": stop, "stop_sequence": None},
                usage={"output_tokens": 1},
            ),
            _event("message_stop"),
        ]
    )


def _answered(body: Mapping[str, Any]) -> bool:
    """Whether the request carries a tool's result, so the scripted call already ran."""
    for message in body.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_result" for part in content
        ):
            return True
    return False


class FakeMessagesApi:
    """Serves ``POST /v1/messages`` on 127.0.0.1 in a background thread."""

    def __init__(self, first: Mapping[str, Any] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        # Each request's headers, lowercased, in the same order: anthropic-beta among them.
        self.headers: list[dict[str, str]] = []
        self._first = dict(first) if first is not None else text()
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                size = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                api.requests.append(body)
                api.headers.append({k.lower(): v for k, v in self.headers.items()})
                block = text() if _answered(body) or not body.get("tools") else api._first
                if body.get("stream"):
                    payload, kind = _stream(body.get("model"), block), "text/event-stream"
                else:
                    whole = {
                        "id": "msg_fake",
                        "type": "message",
                        "role": "assistant",
                        "model": body.get("model"),
                        "content": [block],
                        "stop_reason": "tool_use" if block["type"] == "tool_use" else "end_turn",
                        "stop_sequence": None,
                        "usage": USAGE,
                    }
                    payload, kind = json.dumps(whole).encode(), "application/json"
                self.send_response(200)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """The provider address, without ``/v1``: the CLI adds ``/v1/messages`` itself."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def betas(self) -> set[str]:
        """Every ``anthropic-beta`` value any request asked for."""
        return {
            beta.strip()
            for headers in self.headers
            for beta in headers.get("anthropic-beta", "").split(",")
            if beta.strip()
        }

    def offered_tools(self) -> list[str]:
        """The tools offered in the first request that offered any, by name."""
        for request in self.requests:
            if request.get("tools"):
                return [str(t.get("name")) for t in request["tools"]]
        return []

    def tool_outputs(self) -> list[str]:
        """Every tool result sent back, as text."""
        found: list[str] = []
        for request in self.requests:
            for message in request.get("messages", []):
                content = message.get("content") if isinstance(message, dict) else None
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        value = part.get("content")
                        if isinstance(value, list):
                            value = " ".join(str(v.get("text", v)) for v in value)
                        found.append(str(value))
        return found

    def __enter__(self) -> FakeMessagesApi:
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
