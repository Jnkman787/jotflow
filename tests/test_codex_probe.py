"""The codex preset's startup probe (#220, #274), with stand-in binaries.

The real Codex and GitHub server are exercised in tests/test_codex_gate.py. Here a
small script plays Codex, sending the fake API whatever request a test needs, and
others play the GitHub server, so every refusal can be shown anywhere.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from jotflow.adapters import codex_probe
from jotflow.adapters.codex_probe import (
    SAFE_BUILTINS,
    ProbeError,
    check_request,
    check_server,
    offered,
    probe_github_server,
    probe_model,
    probe_runner,
)
from jotflow.models import RunKind

SAFE = [{"type": "function", "name": name} for name in sorted(SAFE_BUILTINS)]


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(stat.S_IRWXU)
    return path


def fake_codex(bin_dir: Path, request: dict[str, Any] | None, *, exit_code: int = 0) -> None:
    """A ``codex`` that sends ``request`` to the probe's fake API, then exits."""
    bin_dir.mkdir(exist_ok=True)
    _script(
        bin_dir / "codex",
        f"""
import json, re, sys, urllib.request
request = json.loads({json.dumps(json.dumps(request))})
args = " ".join(sys.argv)
if request is not None:
    url = re.search(r'base_url="([^"]+)"', args).group(1) + "/responses"
    body = json.dumps(request).encode()
    headers = {{"content-type": "application/json"}}
    urllib.request.urlopen(urllib.request.Request(url, body, headers)).read()
sys.exit({exit_code})
""",
    )


def fake_server(path: Path, answers: dict[str, Any] | None) -> Path:
    """A GitHub MCP server that answers each method from ``answers``, or never answers."""
    return _script(
        path,
        f"""
import json, sys, time
answers = json.loads({json.dumps(json.dumps(answers))})
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if answers is None:
        time.sleep(60)
    answer = answers.get(message["method"], {{"result": {{}}}})
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": message["id"], **answer}}) + "\\n")
    sys.stdout.flush()
""",
    )


def environ(bin_dir: Path) -> dict[str, str]:
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


# The request check


def test_only_safe_tools_and_no_namespace_pass() -> None:
    check_request({"model": "gpt-5.5", "tools": SAFE}, "gpt-5.5")
    check_request({"model": "x", "tools": SAFE[:1]}, None)  # fewer is fine, and no model named


@pytest.mark.parametrize(
    ("request_body", "named"),
    [
        ({"model": "m", "tools": [*SAFE, {"type": "function", "name": "exec"}]}, "exec"),
        ({"model": "m", "tools": [{"type": "local_shell"}]}, "local_shell"),
        (
            {
                "model": "m",
                "tools": SAFE,
                "input": [
                    {
                        "type": "additional_tools",
                        "tools": [{"name": "collaboration", "tools": [{"name": "spawn_agent"}]}],
                    }
                ],
            },
            "spawn_agent, namespace collaboration",
        ),
    ],
)
def test_any_other_tool_or_a_namespace_is_refused_by_name(
    request_body: dict[str, Any], named: str
) -> None:
    with pytest.raises(ProbeError, match=named):
        check_request(request_body, "m")


def test_the_request_must_be_for_the_configured_model() -> None:
    with pytest.raises(ProbeError, match="asked for model 'gpt-6'"):
        check_request({"model": "gpt-6", "tools": SAFE}, "gpt-5.5")
    tools, spaces = offered({"tools": [{"type": "apply_patch"}]})
    assert tools == {"apply_patch"} and spaces == set()


# The server check


def test_a_server_with_no_repository_resources_passes() -> None:
    check_server(
        {"error": {"code": -32601}},
        {"result": {"resources": [{"uri": "ui://github-mcp-server/get-me"}]}},
        {"error": {"code": -32602, "message": "Resource not found"}},
    )


@pytest.mark.parametrize(
    ("templates", "listed", "read", "problem"),
    [
        (
            {"result": {"resourceTemplates": [{"uriTemplate": "repo://{owner}/{repo}/contents"}]}},
            {},
            {"error": {}},
            "serves repository resources: repo://",
        ),
        ({}, {"result": {"resources": [{"uri": "repo://a/b/x"}]}}, {"error": {}}, "repo://a/b/x"),
        ({}, {}, {"result": {"contents": []}}, "answered a repository read"),
    ],
)
def test_a_server_serving_repository_content_is_refused(
    templates: dict[str, Any], listed: dict[str, Any], read: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ProbeError, match=problem):
        check_server(templates, listed, read)


# The probe end to end, with stand-ins


def test_a_safe_codex_passes_and_an_unsafe_one_is_refused(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    server = fake_server(tmp_path / "github", {})
    fake_codex(bin_dir, {"model": "gpt-5.5", "tools": SAFE})
    probe_model(RunKind.DRAFTING, "gpt-5.5", github_mcp=str(server), environ=environ(bin_dir))
    fake_codex(bin_dir, {"model": "gpt-5.5", "tools": [*SAFE, {"name": "exec"}]})
    with pytest.raises(ProbeError, match="forbids: exec"):
        probe_model(RunKind.DRAFTING, "gpt-5.5", github_mcp=str(server), environ=environ(bin_dir))


@pytest.mark.parametrize(("request_body", "code"), [(None, 0), ({"model": "m", "tools": []}, 1)])
def test_a_probe_that_sends_nothing_or_fails_refuses_the_start(
    tmp_path: Path, request_body: dict[str, Any] | None, code: int
) -> None:
    bin_dir = tmp_path / "bin"
    fake_codex(bin_dir, request_body, exit_code=code)
    with pytest.raises(ProbeError, match="did not finish its probe for m cleanly"):
        probe_model(RunKind.DRAFTING, "m", github_mcp="/nowhere", environ=environ(bin_dir))


def test_a_missing_codex_refuses_the_start(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="could not be probed"):
        probe_model(RunKind.DRAFTING, None, github_mcp="/nowhere", environ={"PATH": str(tmp_path)})


def test_the_github_server_is_asked_as_the_preset_starts_it(tmp_path: Path) -> None:
    calls = tmp_path / "argv.json"
    server = _script(
        tmp_path / "github",
        f"""
import json, sys
open({str(calls)!r}, "w").write(json.dumps(sys.argv[1:]))
for line in sys.stdin:
    message = json.loads(line)
    if "id" in message:
        read = message["method"] == "resources/read"
        answer = {{"error": {{"code": -32602}}}} if read else {{"result": {{}}}}
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": message["id"], **answer}}) + "\\n")
        sys.stdout.flush()
""",
    )
    probe_github_server(RunKind.DRAFTING, github_mcp=str(server))
    argv = json.loads(calls.read_text())
    assert argv[:2] == ["stdio", "--read-only"] and argv[2] == "--tools"
    probe_github_server(RunKind.QUESTION, github_mcp="/nowhere")  # no server for questions


def test_a_server_that_serves_repos_or_stays_silent_refuses_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serving = fake_server(
        tmp_path / "serving",
        {
            "resources/templates/list": {
                "result": {"resourceTemplates": [{"uriTemplate": "repo://x"}]}
            }
        },
    )
    with pytest.raises(ProbeError, match="serves repository resources"):
        probe_github_server(RunKind.DRAFTING, github_mcp=str(serving))
    monkeypatch.setattr(codex_probe, "SERVER_SECONDS", 1)
    silent = fake_server(tmp_path / "silent", None)
    with pytest.raises(ProbeError, match="did not answer in time"):
        probe_github_server(RunKind.DRAFTING, github_mcp=str(silent))
    with pytest.raises(ProbeError, match="could not be started"):
        probe_github_server(RunKind.DRAFTING, github_mcp=str(tmp_path / "missing"))


def test_the_runner_probes_each_kinds_model_and_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        codex_probe, "probe_model", lambda kind, model, **_: seen.append((kind.value, model))
    )
    monkeypatch.setattr(
        codex_probe, "probe_github_server", lambda kind, **_: seen.append((kind.value, "server"))
    )
    probe_runner({RunKind.QUESTION: "b", RunKind.DRAFTING: "a"}, github_mcp="/x")
    assert seen == [
        ("drafting", "a"),
        ("drafting", "server"),
        ("question", "b"),
        ("question", "server"),
    ]


def test_a_codex_runner_will_not_start_when_the_probe_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jotflow.adapters.runner import ConfigError
    from jotflow.commands import runner as command

    def refuse(*_: Any, **__: Any) -> None:
        raise ProbeError("Codex would offer gpt-6 tools the codex preset forbids: exec")

    monkeypatch.setattr(command, "probe_runner", refuse)
    monkeypatch.setattr("jotflow.adapters.presets.CodexPreset.check_install", lambda self: None)
    monkeypatch.setattr(os.path, "isfile", lambda _: True)
    monkeypatch.setattr(os, "access", lambda *_: True)
    settings = command.RunnerSettings(
        _env_file=None,  # type: ignore[call-arg]
        runner_secret="s" * 32,
        agent_preset="codex",
        github_read_token="github_pat_x",
    )
    with pytest.raises(ConfigError, match="forbids: exec"):
        command.build_config(settings)
