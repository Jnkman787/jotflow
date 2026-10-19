"""The fixture-backed GitHub MCP stand-in that replay --agent gives a real agent (#23)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jotflow.testing.fake_github_mcp import FakeGithub, main, serve, tool_definitions
from jotflow.testing.threads import GithubRepo, builtin_thread

READ_TOOLS = {"issue_read", "list_issues", "search_issues", "list_label", "get_label"}


def fake(tools: list[str] | None = None) -> FakeGithub:
    return FakeGithub(builtin_thread("existing-topic").github, tools)


def call(server: FakeGithub, tool: str, /, **arguments: Any) -> Any:
    result = server.call(tool, arguments)
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def refused(server: FakeGithub, tool: str, /, **arguments: Any) -> str:
    result = server.call(tool, arguments)
    assert result["isError"] is True
    return str(result["content"][0]["text"])


def test_it_lists_github_mcp_server_2_0_2s_read_tools() -> None:
    assert {t["name"] for t in tool_definitions()} == READ_TOOLS | {
        "issue_write",
        "add_issue_comment",
    }
    assert {t["name"] for t in fake().tools} == READ_TOOLS  # read-only by default
    for tool in tool_definitions():
        assert tool["inputSchema"]["type"] == "object"
    assert {t["name"] for t in fake(["list_label", "nope"]).tools} == {"list_label"}


def test_initialize_ping_and_unknown_methods() -> None:
    server = fake()
    init = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert init is not None and init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    odd = server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    assert odd is not None and odd["result"]["protocolVersion"] == "2026-07-28"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {},
    }
    missing = server.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert missing is not None and missing["error"]["code"] == -32601
    listed = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    assert listed is not None and len(listed["result"]["tools"]) == 5
    called = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "list_label", "arguments": {"owner": "acme-co", "repo": "web"}},
        }
    )
    assert called is not None and called["result"]["isError"] is False


def test_search_scores_words_across_open_and_closed_issues() -> None:
    server = fake()
    found = call(server, "search_issues", query="export the dashboard table as csv")
    assert [i["number"] for i in found["items"]][:1] == [42]
    assert found["items"][0]["html_url"] == "https://github.com/acme-co/web/issues/42"
    scoped = call(server, "search_issues", query="retries backoff", owner="acme-co", repo="api")
    assert [i["number"] for i in scoped["items"]] == [12]  # closed, and still found
    assert (
        call(server, "search_issues", query="retries", owner="acme-co", repo="web")["items"] == []
    )
    by_query = call(server, "search_issues", query="repo:acme-co/api is:open is:issue retries")
    assert by_query["total_count"] == 0  # #12 is closed
    closed = call(server, "search_issues", query="state:closed password")
    assert [i["number"] for i in closed["items"]] == [17]
    assert call(server, "search_issues", query="the and of")["total_count"] == 0
    assert "query" in refused(server, "search_issues", query=" ")


def test_list_read_and_labels() -> None:
    server = fake()
    every = call(server, "list_issues", owner="acme-co", repo="web")
    assert [i["number"] for i in every["issues"]] == [42, 31, 23, 17]  # both states
    open_web = call(server, "list_issues", owner="acme-co", repo="web", state="OPEN")
    assert [i["number"] for i in open_web["issues"]] == [42, 31, 23]
    closed = call(
        server, "list_issues", owner="acme-co", repo="web", state="CLOSED", direction="ASC"
    )
    assert [i["number"] for i in closed["issues"]] == [17]
    issue = call(server, "issue_read", method="get", owner="acme-co", repo="web", issue_number=42)
    assert issue["title"] == "Export the dashboard table as CSV"
    labels = call(
        server, "issue_read", method="get_labels", owner="acme-co", repo="web", issue_number=42
    )
    assert labels == [{"name": "enhancement"}]
    no_comments = call(
        server, "issue_read", method="get_comments", owner="acme-co", repo="web", issue_number=42
    )
    assert no_comments == []
    assert "parent" in refused(
        server, "issue_read", method="get_parent", owner="acme-co", repo="web", issue_number=42
    )
    assert "unknown method" in refused(
        server, "issue_read", method="edit", owner="acme-co", repo="web", issue_number=42
    )
    assert "404" in refused(
        server, "issue_read", method="get", owner="acme-co", repo="web", issue_number=9
    )
    names = [
        label["name"] for label in call(server, "list_label", owner="acme-co", repo="web")["labels"]
    ]
    assert names == ["bug", "documentation", "enhancement"]
    assert call(server, "get_label", owner="acme-co", repo="web", name="BUG")["name"] == "bug"
    assert "not found" in refused(server, "get_label", owner="acme-co", repo="web", name="wontfix")
    assert "404" in refused(server, "list_label", owner="acme-co", repo="elsewhere")
    assert "owner and repo" in refused(server, "list_label", owner="acme-co")
    assert "unknown tool" in refused(server, "issue_write", owner="acme-co", repo="web")


def test_it_has_no_write_tools_even_when_asked() -> None:
    server = fake(["issue_write", "add_issue_comment", "issue_read"])
    assert [t["name"] for t in server.tools] == ["issue_read"]


def test_serve_answers_line_by_line() -> None:
    lines = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            "",
            "not json",
            json.dumps(["batch"]),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        ]
    )
    out = io.StringIO()
    serve(fake(), io.StringIO(lines), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert len(replies) == 2


def test_it_runs_as_the_preset_starts_github_mcp_server(tmp_path: Path) -> None:
    data = tmp_path / "github.json"
    repos = builtin_thread("existing-topic").github
    data.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in repos.items()}))
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "jotflow.testing.fake_github_mcp",
            "--data",
            str(data),
            "stdio",
            "--read-only",
            "--tools",
            "list_label,get_label",
        ],
        input=request,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    tools = json.loads(done.stdout)["result"]["tools"]
    assert [t["name"] for t in tools] == ["get_label", "list_label"]


def test_main_reads_its_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "github.json"
    data.write_text(json.dumps({"acme-co/web": GithubRepo(labels=("bug",)).model_dump()}))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
    )
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert main(["stdio", "--data", str(data)]) == 0
    assert json.loads(out.getvalue())["result"] == {}


def writer(tmp_path: Path) -> tuple[FakeGithub, list[dict[str, GithubRepo]], Path]:
    saved: list[dict[str, GithubRepo]] = []
    log = tmp_path / "writes.jsonl"
    server = FakeGithub(
        builtin_thread("existing-topic").github, read_only=False, save=saved.append, log=log
    )
    return server, saved, log


def test_writes_change_the_data_and_are_logged(tmp_path: Path) -> None:
    server, saved, log = writer(tmp_path)
    assert {"issue_write", "add_issue_comment"} <= {t["name"] for t in server.tools}
    made = call(
        server,
        "issue_write",
        method="create",
        owner="acme-co",
        repo="web",
        title="New thing",
        body="Body <!-- jotflow:proposal:p1 -->",
        labels=["enhancement"],
        assignees=["acme-bob"],
    )
    assert made["html_url"] == "https://github.com/acme-co/web/issues/43"
    found = call(server, "issue_read", method="get", owner="acme-co", repo="web", issue_number=43)
    assert found["body"].endswith("<!-- jotflow:proposal:p1 -->")
    assert found["assignees"] == [{"login": "acme-bob"}]
    call(server, "add_issue_comment", owner="acme-co", repo="web", issue_number=43, body="More.")
    comments = call(
        server, "issue_read", method="get_comments", owner="acme-co", repo="web", issue_number=43
    )
    assert [c["body"] for c in comments] == ["More."]
    call(
        server,
        "issue_write",
        method="update",
        owner="acme-co",
        repo="web",
        issue_number=43,
        labels=["bug"],
        state="closed",
    )
    updated = call(server, "issue_read", method="get", owner="acme-co", repo="web", issue_number=43)
    assert (updated["state"], updated["labels"]) == ("closed", [{"name": "bug"}])
    assert [json.loads(line)["tool"] for line in log.read_text().splitlines()] == [
        "issue_write",
        "add_issue_comment",
        "issue_write",
    ]
    assert len(saved) == 3 and saved[-1]["acme-co/web"].issues[-1].comments == ("More.",)


def test_bad_writes_are_refused_and_not_logged(tmp_path: Path) -> None:
    server, saved, log = writer(tmp_path)
    base = {"owner": "acme-co", "repo": "web"}
    assert "title" in refused(server, "issue_write", method="create", **base)
    assert "labels do not exist: wontfix" in refused(
        server, "issue_write", method="create", title="t", labels=["wontfix"], **base
    )
    assert "404" in refused(server, "issue_write", method="update", issue_number=99, **base)
    assert "unknown method" in refused(server, "issue_write", method="delete", **base)
    assert "body" in refused(server, "add_issue_comment", issue_number=42, **base)
    assert "404" in refused(server, "add_issue_comment", issue_number=99, body="x", **base)
    assert saved == [] and not log.exists()


def test_a_second_process_sees_the_first_ones_write(tmp_path: Path) -> None:
    data = tmp_path / "github.json"
    repos = builtin_thread("existing-topic").github
    data.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in repos.items()}))
    create = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "issue_write",
            "arguments": {
                "method": "create",
                "owner": "acme-co",
                "repo": "web",
                "title": "T",
                "body": "B",
            },
        },
    }
    run = [sys.executable, "-I", "-m", "jotflow.testing.fake_github_mcp", "--data", str(data)]
    subprocess.run(
        [*run, "--log", str(tmp_path / "log"), "stdio"],
        input=json.dumps(create) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    listed = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_issues", "arguments": {"owner": "acme-co", "repo": "web"}},
    }
    done = subprocess.run(
        [*run, "stdio", "--read-only"],
        input=json.dumps(listed) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    text = json.loads(done.stdout)["result"]["content"][0]["text"]
    assert json.loads(text)["issues"][0]["number"] == 43
    assert len((tmp_path / "log").read_text().splitlines()) == 1
