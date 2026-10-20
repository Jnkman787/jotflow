"""A fixture-backed stand-in for GitHub's MCP server, for ``jotflow replay --agent`` (#23).

The saved threads use fictional ``acme-co`` repos, so a real agent checked
against them cannot reach GitHub. This server answers the same read tools as
github-mcp-server 2.0.2, with the names, descriptions, and input schemas it
lists (``fake_github_tools.json``, captured from that release), from a
fixture's ``github`` block. It makes no network calls.

Without ``--read-only`` it also serves the two write tools an execution run
uses (#31), ``issue_write`` and ``add_issue_comment``. A write changes the data
file, so a second run sees what the first wrote, and is appended to ``--log``
as one JSON line with the tool and its arguments, so tests can compare what an
agent wrote with what JotFlow planned.

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
from pathlib import Path
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


WRITE_TOOLS = frozenset({"issue_write", "add_issue_comment"})


class FakeGithub:
    def __init__(
        self,
        repos: Mapping[str, GithubRepo],
        tools: Sequence[str] | None = None,
        *,
        read_only: bool = True,
        save: Callable[[dict[str, GithubRepo]], None] | None = None,
        log: Path | None = None,
    ) -> None:
        self._repos = {name.casefold(): (name, repo) for name, repo in repos.items()}
        known = tool_definitions()
        names = {t["name"] for t in known}
        wanted = set(tools) if tools else names
        if read_only:  # like the real server: read-only wins over --tools
            wanted -= WRITE_TOOLS
        self.tools = [t for t in known if t["name"] in wanted]
        self._save = save
        self._log = log
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "search_issues": self._search,
            "list_issues": self._list,
            "issue_read": self._read,
            "list_label": self._labels,
            "get_label": self._label,
            "issue_write": self._write,
            "add_issue_comment": self._comment,
        }

    @property
    def repos(self) -> dict[str, GithubRepo]:
        return {name: repo for name, repo in self._repos.values()}

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

    def _changed(
        self, name: str, repo: GithubRepo, tool: str, arguments: Mapping[str, Any]
    ) -> None:
        self._repos[name.casefold()] = (name, repo)
        if self._log is not None:
            with self._log.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"tool": tool, "arguments": dict(arguments)}) + "\n")
        if self._save is not None:
            self._save(self.repos)

    def _write(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        name, repo = self._repo(arguments)
        method = arguments.get("method")
        issues = list(repo.issues)
        if method == "create":
            if not arguments.get("title"):
                raise ToolFailure("missing required parameter: title")
            unknown = sorted(set(arguments.get("labels") or ()) - set(repo.labels))
            if unknown:  # the real API would create them; the fake refuses, so a test sees it
                raise ToolFailure(f"labels do not exist: {', '.join(unknown)}")
            number = max((i.number for i in issues), default=0) + 1
            issue = GithubIssue(
                number=number,
                title=str(arguments["title"]),
                body=str(arguments.get("body") or ""),
                labels=tuple(arguments.get("labels") or ()),
                assignees=tuple(arguments.get("assignees") or ()),
            )
            issues.append(issue)
        elif method == "update":
            target = arguments.get("issue_number")
            found = next((i for i in issues if i.number == target), None)
            if found is None:
                raise ToolFailure(f"failed to update issue: 404 Not Found {target}")
            update: dict[str, Any] = {}
            for key in ("title", "body", "state"):
                if key in arguments:
                    update[key] = arguments[key]
            for key in ("labels", "assignees"):
                if key in arguments:
                    update[key] = tuple(arguments[key] or ())
            issue = found.model_copy(update=update)
            issues = [issue if i.number == target else i for i in issues]
        else:
            raise ToolFailure(f"unknown method {method}")
        self._changed(
            name, repo.model_copy(update={"issues": tuple(issues)}), "issue_write", arguments
        )
        return {
            "number": issue.number,
            "html_url": _url(name, issue.number),
            "url": _url(name, issue.number),
        }

    def _comment(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        name, repo = self._repo(arguments)
        number = arguments.get("issue_number")
        body = arguments.get("body")
        if not body:
            raise ToolFailure("missing required parameter: body")
        found = next((i for i in repo.issues if i.number == number), None)
        if found is None:
            raise ToolFailure(f"failed to comment: 404 Not Found {number}")
        issue = found.model_copy(update={"comments": (*found.comments, str(body))})
        issues = tuple(issue if i.number == number else i for i in repo.issues)
        self._changed(
            name, repo.model_copy(update={"issues": issues}), "add_issue_comment", arguments
        )
        ident = len(issue.comments)
        return {"id": ident, "html_url": f"{_url(name, found.number)}#issuecomment-{ident}"}

    def _repo(self, arguments: Mapping[str, Any]) -> tuple[str, GithubRepo]:
        owner, repo = arguments.get("owner"), arguments.get("repo")
        if not owner or not repo:
            raise ToolFailure("missing required parameter: owner and repo")
        found = self._repos.get(f"{owner}/{repo}".casefold())
        if found is None:
            raise ToolFailure(f"failed to find repository {owner}/{repo}: 404 Not Found")
        return found

    def _scoped(self, arguments: Mapping[str, Any]) -> list[tuple[str, GithubRepo]]:
        if arguments.get("repo"):
            return [self._repo(arguments)]
        if owner := arguments.get("owner"):
            # One owner's every repo, as `org:<owner>` searches (#236).
            prefix = f"{str(owner).casefold()}/"
            return [(n, r) for n, r in self._repos.values() if n.casefold().startswith(prefix)]
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
            elif value and key.lower() in ("org", "user"):
                scope["owner"] = value
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
        if method == "get_comments":
            return [
                {"id": n, "body": body, "html_url": f"{_url(name, issue.number)}#issuecomment-{n}"}
                for n, body in enumerate(issue.comments, 1)
            ]
        if method == "get_sub_issues":
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


def _url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _issue(repo: str, issue: GithubIssue) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "html_url": _url(repo, issue.number),
        "labels": [{"name": label} for label in issue.labels],
        "assignees": [{"login": login} for login in issue.assignees],
        "comments": len(issue.comments),
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
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--tools", default="")
    parser.add_argument("--log", type=Path, default=None, help="append each write here")
    args = parser.parse_args(argv)
    data = Path(args.data)
    raw = json.loads(data.read_text(encoding="utf-8"))
    repos = {name: GithubRepo.model_validate(repo) for name, repo in raw.items()}
    tools = [t for t in args.tools.split(",") if t]

    def save(changed: dict[str, GithubRepo]) -> None:
        text = json.dumps({n: r.model_dump(mode="json") for n, r in changed.items()})
        data.write_text(text, encoding="utf-8")

    server = FakeGithub(repos, tools or None, read_only=args.read_only, save=save, log=args.log)
    serve(server, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - run as a module by replay --agent
    sys.exit(main())
