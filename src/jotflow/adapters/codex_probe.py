"""The codex preset's startup probe (#220, #274): what tools would Codex offer, really?

Codex sets each model's tools from catalog metadata, not from config: a model in
"code mode" gets a JavaScript ``exec`` tool and sub-agents whatever the features
say. So instead of a list of trusted models, the runner asks Codex itself, once per
configured model, at startup:

- **The model's side.** The pinned Codex runs one turn with the preset's own config
  for that run kind, against a local fake Responses API that records the request.
  Every tool offered must be one of :data:`SAFE_BUILTINS`, and no nested tool
  namespace, where code mode puts ``exec`` and ``spawn_agent``, may appear.
- **The GitHub server's side (#274).** Codex's MCP resource tools can't be turned
  off, so the GitHub server must serve nothing behind them. Started exactly as the
  preset starts it, with the runner's own read token, since the server may register
  more for a token that allows more, it must list no resource templates, no
  resource but its own ``ui://`` pages, on one page, and refuse a ``repo://`` read
  of a repo that doesn't exist.

Anything else, including a probe that can't run or can't be read, refuses the
runner's start: fail closed. Only the model provider differs from a real run, a
custom one routed to 127.0.0.1, since the probe must not reach OpenAI. With
``api_key_model_discovery`` off (#284), both read the pinned release's bundled
catalog, so the tools match; the threat model names this as a residual difference.

The probe runs at startup only. With discovery off and the CLI version pinned, the
tool set can't change between startup and a run.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jotflow.adapters.presets import CODEX, GITHUB_TOOLS, CodexPreset, RunContext
from jotflow.adapters.runner import write_private
from jotflow.models import RunKind
from jotflow.testing.fake_responses_api import FakeResponsesApi

# The built-ins Codex 0.163.0 offers this preset's config, and nothing else may be.
# A run kind with no MCP server gets fewer: no resource tools, no tool_search.
SAFE_BUILTINS = frozenset(
    {
        "apply_patch",  # refused by the read-only sandbox; the CI gate shows it
        "tool_search",  # how Codex reaches MCP tools
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
    }
)
PROBE_SECONDS = 60
SERVER_SECONDS = 10
# For the model probe only, where Codex lists the GitHub server's tools and calls none.
DUMMY_TOKEN = "github_pat_probe"
_PROBE_REPO = "repo://jotflow-probe/probe/contents/README.md"


class ProbeError(Exception):
    """The probe found a tool or a resource the preset must not have. The message says which."""


def offered(request: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """The tools a Responses request offers the model, and any nested namespaces."""
    tools = {str(t.get("name") or t.get("type")) for t in request.get("tools", [])}
    namespaces: set[str] = set()
    for item in request.get("input", []):
        if item.get("type") == "additional_tools":
            for space in item.get("tools", []):
                namespaces.add(str(space.get("name") or space.get("type") or "?"))
                tools |= {str(t.get("name")) for t in space.get("tools", []) if t.get("name")}
    return tools, namespaces


def check_request(request: Mapping[str, Any], model: str | None) -> None:
    """Raise :class:`ProbeError` unless the request offers only safe tools, for ``model``."""
    tools, namespaces = offered(request)
    extra = sorted(tools - SAFE_BUILTINS)
    if namespaces or extra:
        named = ", ".join([*extra, *(f"namespace {n}" for n in sorted(namespaces))])
        raise ProbeError(f"Codex would offer {model} tools the codex preset forbids: {named}")
    if model is not None and request.get("model") != model:
        raise ProbeError(f"Codex asked for model {request.get('model')!r}, not {model!r}")


def probe_model(
    kind: RunKind,
    model: str | None,
    *,
    github_mcp: str,
    preset: CodexPreset = CODEX,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run the pinned Codex once for ``kind`` and ``model``, and check its tools."""
    env_in = os.environ if environ is None else environ
    with tempfile.TemporaryDirectory(prefix="jotflow-probe-") as tmp:
        root = Path(tmp)
        root.chmod(0o700)
        run_dir = root / "run"
        run_dir.mkdir(mode=0o700)
        context = RunContext(
            kind=kind,
            run_id="probe",
            run_dir=run_dir,
            config_path=run_dir / "mcp.json",
            mcp_url="http://127.0.0.1:9/mcp",
            mcp_token="jfr_probe",
            github_token=DUMMY_TOKEN,
            model=model,
            github_mcp=github_mcp,
        )
        plan = preset.plan(context)
        check_config(plan.files["codex/config.toml"])
        for relative, text in plan.files.items():
            write_private(run_dir, relative, text)
        with FakeResponsesApi() as api:
            # No JotFlow server: a probe has no run token, and the GitHub server
            # alone adds the resource tools a real run would get.
            skip = ["-c", "mcp_servers.jotflow.enabled=false"]
            argv = [*plan.argv[:2], *api.provider_overrides(), *skip, *plan.argv[2:]]
            env = {
                "PATH": env_in.get("PATH", "/usr/bin:/bin"),
                "HOME": str(root / "home"),
                "TMPDIR": str(run_dir),
                "FAKE_MODEL_KEY": "probe",
                **plan.env,
            }
            try:
                done = subprocess.run(
                    argv,
                    env=env,
                    cwd=run_dir,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=PROBE_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ProbeError(f"Codex could not be probed ({type(exc).__name__})") from None
        if done.returncode != 0 or not api.requests:
            raise ProbeError(
                f"Codex did not finish its probe for {model or 'its default model'} cleanly"
            )
        check_request(api.requests[0], model)


def check_config(config_toml: str) -> None:
    """The run's config must keep the remote model catalog off (#284): the probe's
    answer holds only while every run reads the release's bundled catalog."""
    import tomllib

    features = tomllib.loads(config_toml).get("features", {})
    if features.get("api_key_model_discovery") is not False:
        raise ProbeError("the codex config doesn't turn off api_key_model_discovery")


def probe_github_server(
    kind: RunKind, *, github_mcp: str, token: str | None, preset: CodexPreset = CODEX
) -> None:
    """The GitHub server, started as ``kind``'s runs start it, with the runner's own
    read token, must serve no repo content."""
    if not GITHUB_TOOLS[kind]:
        return  # this kind's runs have no GitHub server
    if not token:
        raise ProbeError("the GitHub MCP server can't be checked without the read token")
    with tempfile.TemporaryDirectory(prefix="jotflow-probe-") as tmp:
        run_dir = Path(tmp)
        context = RunContext(
            kind=kind,
            run_id="probe",
            run_dir=run_dir,
            config_path=run_dir / "mcp.json",
            mcp_url="http://127.0.0.1:9/mcp",
            mcp_token="jfr_probe",
            github_token=token,
            github_mcp=github_mcp,
        )
        server = preset.plan(context).files["codex/config.toml"]
    args, env = _github_server(server)
    answers = _ask_server(
        [github_mcp, *args],
        {"PATH": "/usr/bin:/bin", **env},
        [
            ("resources/templates/list", {}),
            ("resources/list", {}),
            ("resources/read", {"uri": _PROBE_REPO}),
        ],
    )
    check_server(*answers)


def check_server(
    templates: Mapping[str, Any], listed: Mapping[str, Any], read: Mapping[str, Any]
) -> None:
    """Raise :class:`ProbeError` unless the server serves no repository resources.

    An error answer to a list means the server serves none of that kind. A result
    must be one complete page, of the shape MCP gives it: anything else is refused.
    """
    found = _listed(templates, "resourceTemplates")
    if found:
        uris = ", ".join(str(t.get("uriTemplate")) for t in found)
        raise ProbeError(f"the GitHub MCP server serves repository resources: {uris}")
    for resource in _listed(listed, "resources"):
        uri = str(resource.get("uri"))
        if not uri.startswith("ui://"):
            raise ProbeError(f"the GitHub MCP server serves a resource it mustn't: {uri}")
    if "error" not in read:
        raise ProbeError("the GitHub MCP server answered a repository read")


def _listed(answer: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    if "error" in answer and "result" not in answer:
        return []
    result = answer.get("result")
    if not isinstance(result, dict):
        raise ProbeError(f"the GitHub MCP server's {key} answer could not be read")
    if result.get("nextCursor"):
        raise ProbeError(f"the GitHub MCP server's {key} list has more than one page")
    items = result.get(key, [])
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ProbeError(f"the GitHub MCP server's {key} answer could not be read")
    return items


def _github_server(config_toml: str) -> tuple[list[str], dict[str, str]]:
    """The GitHub server's args and environment, exactly as a run's config gives them."""
    import tomllib

    server = tomllib.loads(config_toml)["mcp_servers"]["github"]
    env = {str(k): str(v) for k, v in server.get("env", {}).items()}
    return [str(a) for a in server["args"]], env


def _ask_server(
    argv: list[str], env: Mapping[str, str], calls: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Initialize an MCP server over stdio, then make ``calls`` and return each answer."""
    try:
        proc = subprocess.Popen(
            argv,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ProbeError(
            f"the GitHub MCP server could not be started ({type(exc).__name__})"
        ) from None
    assert proc.stdin is not None and proc.stdout is not None
    deadline = time.monotonic() + SERVER_SECONDS
    try:
        init = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "jotflow-probe", "version": "1"},
        }
        _send(proc, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": init})
        _receive(proc, 0, deadline)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        answers = []
        for index, (method, params) in enumerate(calls, start=1):
            _send(proc, {"jsonrpc": "2.0", "id": index, "method": method, "params": params})
            answers.append(_receive(proc, index, deadline))
        return answers
    finally:
        proc.kill()
        proc.wait(timeout=5)


def _send(proc: subprocess.Popen[bytes], message: Mapping[str, Any]) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
    except OSError:
        raise ProbeError("the GitHub MCP server closed its input") from None


def _receive(proc: subprocess.Popen[bytes], wanted: int, deadline: float) -> dict[str, Any]:
    """The answer to request ``wanted``, skipping notifications, before ``deadline``."""
    assert proc.stdout is not None
    while True:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([proc.stdout], [], [], left)[0]:
            raise ProbeError("the GitHub MCP server did not answer in time")
        line = proc.stdout.readline()
        if not line:
            raise ProbeError("the GitHub MCP server stopped")
        try:
            message = json.loads(line)
        except ValueError:
            raise ProbeError("the GitHub MCP server's answer could not be read") from None
        if isinstance(message, dict) and message.get("id") == wanted:
            return message


def probe_runner(
    kinds_models: Mapping[RunKind, str | None],
    *,
    github_mcp: str,
    read_token: str | None,
    preset: CodexPreset = CODEX,
) -> None:
    """Probe every model the runner will use, and each kind's GitHub server. Fails closed."""
    for kind, model in sorted(kinds_models.items(), key=lambda pair: pair[0].value):
        probe_model(kind, model, github_mcp=github_mcp, preset=preset)
        probe_github_server(kind, github_mcp=github_mcp, token=read_token, preset=preset)
