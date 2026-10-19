"""A stand-in agent for execution runs: it follows the packet literally, over real MCP (#31).

``jotflow replay --execute --agent scripted`` launches it through the real exec
runner, so CI checks the whole execution path (the runner, the run's private
MCP config, the token, JotFlow's MCP server, and the GitHub stand-in) without a
model. It does what ``instructions/execution.md`` asks, in code: the marker
check, the label check, each planned write exactly, then ``record_result``.

``--crash-after-write`` makes the writes and exits without reporting, as an
interrupted run would, so the next run's marker check can be tested.
``--tamper`` changes each body it writes, and ``--skip-marker-check`` skips
the marker check, so tests can show the comparison catches an agent that
deviates from the approved content or writes twice.

    python -m jotflow.testing.scripted_executor <mcp.json> [--crash-after-write]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

EXIT_CRASHED = 5
EXIT_REFUSED = 3


class Github:
    """A minimal MCP stdio client for the GitHub server the config names."""

    def __init__(self, server: dict[str, Any]) -> None:
        env = {**os.environ, **server.get("env", {})}
        self._process = subprocess.Popen(
            [server["command"], *server.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        self._next = 0
        self._send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    def _send(self, method: str, params: dict[str, Any]) -> Any:
        assert self._process.stdin is not None and self._process.stdout is not None
        self._next += 1
        message = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()
        reply = json.loads(self._process.stdout.readline())
        return reply["result"]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        result = self._send("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"]
        if result.get("isError"):
            return False, text
        return True, json.loads(text)

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait(timeout=10)


def issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _found_marker(github: Github, repo: str, checks: dict[str, Any]) -> str | None:
    owner, name = repo.split("/", 1)
    where, mark = checks["marker_in"], checks["marker"]
    number = int(checks.get("issue_number") or 0)
    if where == "recent_issues":
        ok, listed = github.call("list_issues", {"owner": owner, "repo": name})
        for issue in listed["issues"] if ok else []:
            if mark in (issue.get("body") or ""):
                return issue_url(repo, int(issue["number"]))
    elif where == "issue_comments":
        ok, comments = github.call(
            "issue_read",
            {"method": "get_comments", "owner": owner, "repo": name, "issue_number": number},
        )
        if ok and any(mark in (c.get("body") or "") for c in comments):
            return issue_url(repo, number)
    elif where == "issue_body":
        ok, issue = github.call(
            "issue_read", {"method": "get", "owner": owner, "repo": name, "issue_number": number}
        )
        if ok and mark in (issue.get("body") or ""):
            return issue_url(repo, number)
    return None


def _with_sets(github: Github, repo: str, write: dict[str, Any]) -> dict[str, Any]:
    arguments = dict(write["arguments"])
    owner, name = repo.split("/", 1)
    number = arguments.get("issue_number")
    if write.get("markers_from_current"):
        from jotflow.execution.writes import with_kept_markers

        _, issue = github.call(
            "issue_read", {"method": "get", "owner": owner, "repo": name, "issue_number": number}
        )
        arguments["body"] = with_kept_markers(arguments["body"], issue.get("body") or "")
    for key, change in (
        ("labels", write.get("labels_from_current")),
        ("assignees", write.get("assignees_from_current")),
    ):
        if not change:
            continue
        _, issue = github.call(
            "issue_read", {"method": "get", "owner": owner, "repo": name, "issue_number": number}
        )
        field = "name" if key == "labels" else "login"
        current = [item[field] for item in issue.get(key) or []]
        wanted = [v for v in current if v not in change["remove"]]
        wanted += [v for v in change["add"] if v not in wanted]
        arguments[key] = wanted
    return arguments


async def play(
    config_path: Path, *, crash_after_write: bool, tamper: bool = False, skip_marker: bool = False
) -> int:
    from jotflow.whiteboard.server import PINNED_VERSIONS

    servers = json.loads(config_path.read_text("utf-8"))["mcpServers"]
    jotflow, github = servers["jotflow"], Github(servers["github"])
    try:
        async with httpx2.AsyncClient(headers=jotflow["headers"], timeout=30) as http:
            transport = streamable_http_client(jotflow["url"], http_client=http)
            async with Client(transport, mode=PINNED_VERSIONS[0]) as client:
                work = await client.call_tool("get_work", {})
                if work.is_error or work.structured_content is None:
                    return EXIT_REFUSED
                execution: dict[str, Any] = work.structured_content["execution"]
                repo, checks = execution["repo"], execution["checks"]

                async def report(**result: str) -> int:
                    done = await client.call_tool("record_result", result)
                    return EXIT_REFUSED if done.is_error else 0

                if not skip_marker and (found := _found_marker(github, repo, checks)):
                    return await report(issue_url=found)
                owner, name = repo.split("/", 1)
                missing = [
                    label
                    for label in checks["labels"]
                    if not github.call("get_label", {"owner": owner, "repo": name, "name": label})[
                        0
                    ]
                ]
                if missing:
                    return await report(error=f"labels missing on {repo}: {', '.join(missing)}")
                url = None
                for write in execution["writes"]:
                    arguments = _with_sets(github, repo, write)
                    if tamper and "body" in arguments:
                        arguments["body"] += "\n\nAlso, a small improvement."
                    ok, result = github.call(write["tool"], arguments)
                    if not ok:
                        return await report(error=f"{write['tool']} failed: {result}")
                    if write["arguments"].get("method") == "create":
                        url = issue_url(repo, int(result["number"]))
                if crash_after_write:
                    return EXIT_CRASHED
                number = execution["proposal"].get("issue_number")
                return await report(issue_url=url or issue_url(repo, int(number)))
    finally:
        github.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripted-executor")
    parser.add_argument("config", type=Path, help="the MCP config the runner wrote")
    parser.add_argument("--crash-after-write", action="store_true")
    parser.add_argument("--tamper", action="store_true", help="for tests: change each body")
    parser.add_argument("--skip-marker-check", action="store_true", help="for tests only")
    args = parser.parse_args(argv)
    return asyncio.run(
        play(
            args.config,
            crash_after_write=args.crash_after_write,
            tamper=args.tamper,
            skip_marker=args.skip_marker_check,
        )
    )


if __name__ == "__main__":  # pragma: no cover - started by the runner
    sys.exit(main())
