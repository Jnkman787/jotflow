"""A fixture-backed stand-in for GitHub's MCP server, for ``jotflow replay --agent`` (#23).

The saved threads use fictional ``acme-co`` repos, so a real agent checked
against them cannot reach GitHub. This server answers the same read tools as
github-mcp-server 2.0.2, with the names, descriptions, and input schemas it
lists (``fake_github_tools.json``, captured from that release), from a
fixture's ``github`` block. It writes nothing and makes no network calls.

It speaks MCP over stdio, one JSON-RPC message per line, and accepts the
arguments the presets pass to the real server (``stdio``, ``--read-only``,
``--tools a,b``), plus ``--data <file>``: the ``github`` block as JSON.

    python -m jotflow.testing.fake_github_mcp --data github.json stdio --read-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from typing import IO, Any

from jotflow.testing.threads import GithubIssue, GithubRepo

SERVER_INFO = {"name": "jotflow-fake-github", "version": "2.0.2-fixture"}
PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")
_WORD = re.compile(r"[a-z0-9]+")
# Words a search query shares with almost every issue, so they never match alone.
_STOP = frozenset({"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "it"})


def tool_definitions() -> list[dict[str, Any]]:
    raw = resources.files("jotflow.testing").joinpath("fake_github_tools.json").read_text("utf-8")
    tools: list[dict[str, Any]] = json.loads(raw)["tools"]
    return tools


class ToolFailure(Exception):
    """A tool call GitHub would refuse; returned as an MCP tool error."""


class FakeGithub:
    def __init__(self, repos: Mapping[str, GithubRepo], tools: Sequence[str] | None = None):
        self._repos = {name.casefold(): (name, repo) for name, repo in repos.items()}
        known = tool_definitions()
        names = {t["name"] for t in known}
        wanted = set(tools) if tools else names
        self.tools = [t for t in known if t["name"] in wanted]
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "search_issues": self._search,
            "list_issues": self._list,
            "issue_read": self._read,
            "list_label": self._labels,
            "get_label": self._label,
        }

    # JSON-RPC

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        ident = message.get("id")
        if ident is None:  # a notification
            return None
        params = message.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
            result: Any = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            result = self.call(str(params.get("name")), dict(params.get("arguments") or {}))
        else:
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {t["name"] for t in self.tools}:
            return _error(f"unknown tool {name}")
        try:
            value = self._handlers[name](arguments)
        except ToolFailure as failure:
            return _error(str(failure))
        return {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}

    # Tools

    def _repo(self, arguments: Mapping[str, Any]) -> tuple[str, GithubRepo]:
        owner, repo = arguments.get("owner"), arguments.get("repo")
        if not owner or not repo:
            raise ToolFailure("missing required parameter: owner and repo")
        found = self._repos.get(f"{owner}/{repo}".casefold())
        if found is None:
            raise ToolFailure(f"failed to find repository {owner}/{repo}: 404 Not Found")
        return found

    def _scoped(self, arguments: Mapping[str, Any]) -> list[tuple[str, GithubRepo]]:
        if arguments.get("owner") or arguments.get("repo"):
            return [self._repo(arguments)]
        return list(self._repos.values())

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        if not query.strip():
            raise ToolFailure("missing required parameter: query")
        state: str | None = None
        scope = dict(arguments)
        words: set[str] = set()
        for part in query.split():
            key, _, value = part.partition(":")
            if value and key.lower() == "repo" and "/" in value:
                scope["owner"], scope["repo"] = value.split("/", 1)
            elif value and key.lower() in ("is", "state") and value.lower() in ("open", "closed"):
                state = value.lower()
            elif value and key.lower() == "is":
                continue
            else:
                words |= set(_WORD.findall(part.lower())) - _STOP
        hits: list[tuple[int, str, GithubIssue]] = []
        for name, repo in self._scoped(scope):
            for issue in repo.issues:
                if state and issue.state != state:
                    continue
                text = set(_WORD.findall(f"{issue.title} {issue.body}".lower()))
                score = len(words & text)
                if score:
                    hits.append((score, name, issue))
        hits.sort(key=lambda hit: (-hit[0], hit[2].number))
        per_page = int(arguments.get("perPage") or 30)
        items = [_issue(name, issue) for _, name, issue in hits[:per_page]]
        return {"total_count": len(hits), "incomplete_results": False, "items": items}

    def _list(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        name, repo = self._repo(arguments)
        # Like github-mcp-server 2.0.2: no state means open and closed issues.
        state = str(arguments.get("state") or "").lower()
        issues = [i for i in repo.issues if not state or i.state == state]
        issues.sort(key=lambda i: i.number, reverse=arguments.get("direction") != "ASC")
        return {
            "issues": [_issue(name, i) for i in issues],
            "pageInfo": {"hasNextPage": False, "endCursor": ""},
            "totalCount": len(issues),
        }

    def _read(self, arguments: Mapping[str, Any]) -> Any:
        name, repo = self._repo(arguments)
        number = arguments.get("issue_number")
        issue = next((i for i in repo.issues if i.number == number), None)
        if issue is None:
            raise ToolFailure(f"failed to get issue: 404 Not Found {number}")
        method = arguments.get("method", "get")
        if method == "get":
            return _issue(name, issue)
        if method == "get_labels":
            return [{"name": label} for label in issue.labels]
        if method in ("get_comments", "get_sub_issues"):
            return []
        if method == "get_parent":
            raise ToolFailure("issue has no parent")
        raise ToolFailure(f"unknown method {method}")

    def _labels(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _, repo = self._repo(arguments)
        labels = [{"name": label, "color": "ededed", "description": ""} for label in repo.labels]
        return {"labels": labels, "totalCount": len(labels)}

    def _label(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _, repo = self._repo(arguments)
        wanted = str(arguments.get("name") or "")
        for label in repo.labels:
            if label.casefold() == wanted.casefold():
                return {"name": label, "color": "ededed", "description": ""}
        raise ToolFailure(f"label {wanted} not found")


def _issue(repo: str, issue: GithubIssue) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "html_url": f"https://github.com/{repo}/issues/{issue.number}",
        "labels": [{"name": label} for label in issue.labels],
    }


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(server: FakeGithub, stdin: IO[str], stdout: IO[str]) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        else:
            reply = server.handle(message) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-github-mcp")
    parser.add_argument("mode", choices=["stdio"])
    parser.add_argument("--data", required=True)
    parser.add_argument("--read-only", action="store_true")  # it is read-only anyway
    parser.add_argument("--tools", default="")
    args = parser.parse_args(argv)
    with open(args.data, encoding="utf-8") as handle:
        raw = json.load(handle)
    repos = {name: GithubRepo.model_validate(repo) for name, repo in raw.items()}
    tools = [t for t in args.tools.split(",") if t]
    serve(FakeGithub(repos, tools or None), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - run as a module by replay --agent
    sys.exit(main())
