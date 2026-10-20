"""The drafting runner's own doc reads (#209): fixed requests, strict answers, caps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jotflow.adapters import github_docs
from jotflow.adapters.github_docs import DocReader, DocsError, decode_text, list_docs, read_doc
from jotflow.adapters.runner import Runner, build_runner_app
from jotflow.doc_paths import MAX_FILE_BYTES
from tests.test_runner import ENVIRON, HOST, READ, SECRET, config

REPO = "acme-co/web"
SHA_PLAN = "a" * 40
SHA_ADR = "b" * 40
SHA_LINK = "c" * 40
SHA_SUB = "d" * 40
SHA_BIN = "e" * 40
SHA_WF = "f" * 40


def tree_items() -> list[dict[str, Any]]:
    return [
        {"path": "docs", "mode": "040000", "type": "tree", "sha": "1" * 40},
        {"path": "docs/plans/x.md", "mode": "100644", "type": "blob", "sha": SHA_PLAN, "size": 5},
        {"path": "docs/adr/3.md", "mode": "100755", "type": "blob", "sha": SHA_ADR, "size": 5},
        # A symlink and a submodule are never listed, even with a text name.
        {"path": "docs/link.md", "mode": "120000", "type": "blob", "sha": SHA_LINK, "size": 9},
        {"path": "docs/sub.md", "mode": "160000", "type": "commit", "sha": SHA_SUB},
        # Not text, and refused paths.
        {"path": "docs/logo.png", "mode": "100644", "type": "blob", "sha": SHA_BIN, "size": 9},
        {"path": ".github/workflows/ci.md", "mode": "100644", "type": "blob", "sha": SHA_WF},
        {"path": ".env.md", "mode": "100644", "type": "blob", "sha": SHA_WF},
        {"path": "docs/bad​name.md", "mode": "100644", "type": "blob", "sha": SHA_WF},
        {"path": "docs/odd.md", "mode": "100644", "type": "blob", "sha": "not-a-sha"},
        "not an object",
    ]


class FakeGitHub:
    def __init__(self, **files: bytes) -> None:
        self.files = {SHA_PLAN: b"plans", SHA_ADR: b"adr 3"} | {
            k: v for k, v in files.items() if len(k) == 40
        }
        self.seen: list[httpx2.Request] = []
        self.repo: Any = {"default_branch": "main", "private": True}
        self.tree: Any = {"tree": tree_items(), "truncated": False}
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        path = request.url.path
        for prefix, status in self.status.items():
            if path.startswith(prefix):
                return httpx2.Response(status, json={"message": "no"})
        if path == f"/repos/{REPO}":
            return httpx2.Response(200, json=self.repo)
        if path.startswith(f"/repos/{REPO}/git/trees/"):
            return httpx2.Response(200, json=self.tree)
        if path.startswith(f"/repos/{REPO}/git/blobs/"):
            sha = path.rsplit("/", 1)[1]
            if sha in self.files:
                return httpx2.Response(200, content=self.files[sha])
        return httpx2.Response(404, json={"message": "Not Found"})


def http_for(github: FakeGitHub) -> httpx2.AsyncClient:
    return github_docs.client(transport=httpx2.MockTransport(github))


async def test_the_listing_is_regular_text_files_only() -> None:
    github = FakeGitHub()
    async with http_for(github) as http:
        tree = await list_docs(http, "ghp_read", REPO)
    assert [e.path for e in tree.entries] == ["docs/plans/x.md", "docs/adr/3.md"]
    assert tree.truncated is False
    repo_call, tree_call = github.seen
    assert tree_call.url.path == f"/repos/{REPO}/git/trees/main"
    assert tree_call.url.query == b"recursive=1"
    assert repo_call.headers["authorization"] == "Bearer ghp_read"


async def test_a_default_branch_is_quoted_into_one_path_segment() -> None:
    github = FakeGitHub()
    github.repo = {"default_branch": "release/2026"}
    async with http_for(github) as http:
        await list_docs(http, "t", REPO)
    assert github.seen[1].url.raw_path.startswith(b"/repos/acme-co/web/git/trees/release%2F2026?")


@pytest.mark.parametrize("branch", ["a b", "x..y~1", "", "a\x00b"])
async def test_an_unusable_branch_name_is_refused(branch: str) -> None:
    github = FakeGitHub()
    github.repo = {"default_branch": branch}
    async with http_for(github) as http:
        with pytest.raises(DocsError):
            await list_docs(http, "t", REPO)
    assert len(github.seen) == 1


async def test_githubs_own_truncation_is_reported() -> None:
    github = FakeGitHub()
    github.tree = {"tree": tree_items(), "truncated": True}
    async with http_for(github) as http:
        assert (await list_docs(http, "t", REPO)).truncated is True


@pytest.mark.parametrize(
    ("prefix", "status", "reason"),
    [
        (f"/repos/{REPO}/git", 404, "can't read this repo's files"),
        (f"/repos/{REPO}", 403, "can't read this repo's files"),
        (f"/repos/{REPO}", 302, "GitHub answered 302"),
        (f"/repos/{REPO}/git", 500, "GitHub answered 500"),
    ],
)
async def test_githubs_refusals_are_errors_and_redirects_are_not_followed(
    prefix: str, status: int, reason: str
) -> None:
    github = FakeGitHub()
    github.status[prefix] = status
    async with http_for(github) as http:
        with pytest.raises(DocsError, match=reason):
            await list_docs(http, "t", REPO)


async def test_an_oversized_tree_is_refused_while_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_docs, "MAX_TREE_BYTES", 200)
    async with http_for(FakeGitHub()) as http:
        with pytest.raises(DocsError, match="too large"):
            await list_docs(http, "t", REPO)


@pytest.mark.parametrize("repo", ["acme-co", "acme-co/web/x", "../web", "acme co/web"])
async def test_only_a_valid_repo_is_asked_for(repo: str) -> None:
    github = FakeGitHub()
    async with http_for(github) as http:
        with pytest.raises(DocsError, match="not a GitHub repo"):
            await list_docs(http, "t", repo)
    assert github.seen == []


async def test_a_read_is_by_the_trees_blob_sha_in_raw_form() -> None:
    github = FakeGitHub()
    async with http_for(github) as http:
        tree = await list_docs(http, "t", REPO)
        entry = tree.find("docs/plans/x.md")
        assert entry is not None
        doc = await read_doc(http, "t", REPO, entry)
    assert (doc.text, doc.sha, doc.truncated) == ("plans", SHA_PLAN, False)
    blob = github.seen[-1]
    assert blob.url.path == f"/repos/{REPO}/git/blobs/{SHA_PLAN}"
    assert blob.headers["accept"] == "application/vnd.github.raw"


async def test_a_big_file_is_cut_without_splitting_a_character() -> None:
    body = ("é" * MAX_FILE_BYTES).encode()  # two bytes each, so the cap lands mid-way
    github = FakeGitHub(**{SHA_PLAN: body})
    async with http_for(github) as http:
        entry = (await list_docs(http, "t", REPO)).find("docs/plans/x.md")
        assert entry is not None
        doc = await read_doc(http, "t", REPO, entry)
    assert doc.truncated is True
    assert len(doc.text.encode()) <= MAX_FILE_BYTES
    assert set(doc.text) == {"é"}


@pytest.mark.parametrize("raw", [b"\xff\xfe bad", b"text\x00more", b"\xc3"])
def test_anything_but_strict_utf8_text_is_refused(raw: bytes) -> None:
    with pytest.raises(DocsError):
        decode_text(raw, cut=False)


async def test_the_reader_finds_paths_only_in_its_tree_and_caches_it() -> None:
    github = FakeGitHub()
    now = [0.0]
    reader = DocReader("t", http=http_for(github), mono=lambda: now[0])
    assert (await reader.read(REPO, "docs/adr/3.md")).text == "adr 3"
    for path in ("docs/link.md", "docs/sub.md", "docs/missing.md", "../etc/passwd.md", "docs"):
        with pytest.raises(github_docs.DocNotFound):
            await reader.read(REPO, path)
    trees = [r for r in github.seen if "/git/trees/" in r.url.path]
    assert len(trees) == 1
    now[0] = 61.0
    await reader.read(REPO, "docs/adr/3.md")
    assert len([r for r in github.seen if "/git/trees/" in r.url.path]) == 2


# The runner's routes


@asynccontextmanager
async def runner_http(
    tmp_path: Path, github: FakeGitHub, *, execution: bool = False
) -> AsyncIterator[httpx2.AsyncClient]:
    cfg = config(tmp_path, execution=execution)
    app = build_runner_app(
        Runner(cfg, environ=ENVIRON), cfg, github_transport=httpx2.MockTransport(github)
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=f"http://{HOST}",
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as http:
        yield http


async def test_the_drafting_runner_lists_and_reads(tmp_path: Path) -> None:
    github = FakeGitHub()
    async with runner_http(tmp_path, github) as http:
        listed = await http.post("/docs/list", json={"repo": REPO})
        assert listed.status_code == 200
        assert listed.json() == {"paths": ["docs/plans/x.md", "docs/adr/3.md"], "truncated": False}
        read = await http.post("/docs/read", json={"repo": REPO, "path": "docs/plans/x.md"})
        assert read.status_code == 200
        assert read.json() == {
            "path": "docs/plans/x.md",
            "sha": SHA_PLAN,
            "text": "plans",
            "truncated": False,
        }
        missing = await http.post("/docs/read", json={"repo": REPO, "path": "docs/link.md"})
        assert missing.status_code == 404
    # The read token, and only GitHub's API.
    assert {r.url.host for r in github.seen} == {"api.github.com"}
    assert {r.headers["authorization"] for r in github.seen} == {f"Bearer {READ}"}


async def test_the_execution_runner_never_reads_docs(tmp_path: Path) -> None:
    github = FakeGitHub()
    async with runner_http(tmp_path, github, execution=True) as http:
        for route, body in (
            ("/docs/list", {"repo": REPO}),
            ("/docs/read", {"repo": REPO, "path": "docs/plans/x.md"}),
        ):
            reply = await http.post(route, json=body)
            assert reply.status_code == 403
    assert github.seen == []


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/docs/list", {}),
        ("/docs/list", {"repo": REPO, "extra": 1}),
        ("/docs/list", {"repo": 5}),
        ("/docs/read", {"repo": REPO}),
        ("/docs/read", {"repo": REPO, "path": "x.md", "sha": SHA_PLAN}),
    ],
)
async def test_a_malformed_docs_request_is_refused_before_github(
    tmp_path: Path, route: str, body: dict[str, Any]
) -> None:
    github = FakeGitHub()
    async with runner_http(tmp_path, github) as http:
        reply = await http.post(route, json=body)
    assert reply.status_code == 400
    assert github.seen == []


async def test_a_query_string_is_refused_by_the_gate(tmp_path: Path) -> None:
    github = FakeGitHub()
    async with runner_http(tmp_path, github) as http:
        reply = await http.post("/docs/list?repo=acme-co/web", json={"repo": REPO})
    assert reply.status_code >= 400
    assert github.seen == []


async def test_the_docs_routes_need_the_runner_secret(tmp_path: Path) -> None:
    github = FakeGitHub()
    async with runner_http(tmp_path, github) as http:
        reply = await http.post(
            "/docs/list", json={"repo": REPO}, headers={"Authorization": "Bearer wrong"}
        )
    assert reply.status_code == 401
    assert github.seen == []


async def test_githubs_failure_is_a_502_with_fixed_text(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.status[f"/repos/{REPO}"] = 403
    async with runner_http(tmp_path, github) as http:
        reply = await http.post("/docs/list", json={"repo": REPO})
    assert reply.status_code == 502
    assert reply.json() == {
        "error": "GitHub answered 403: the read token can't read this repo's files"
    }
