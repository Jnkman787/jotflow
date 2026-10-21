"""The drafting runner's own reads of repo docs, with the read token (#209). Never the agent's.

JotFlow decides which paths an admin allowed. This module only fetches, and
only in fixed shapes:

- :func:`list_docs`: the default branch's tree, as regular text files only.
  ``GET /repos/{repo}`` for the default branch, then
  ``GET /repos/{repo}/git/trees/{branch}?recursive=1``
- :func:`read_doc`: one of those files, found in the tree by its exact path
  and read by the tree's blob sha, ``GET /repos/{repo}/git/blobs/{sha}``. A
  symlink or submodule is never in the list, so nothing is followed, and the
  content is exactly what the tree named.

Every answer is read with a cap while streaming, redirects are never followed,
proxy settings from the environment are ignored, and the content must be
strict UTF-8 with no NUL, or it is refused as not text. Paths and content are
never logged.
"""

from __future__ import annotations

import codecs
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jotflow.doc_paths import FILE_MODES, MAX_FILE_BYTES, PathRefused, check_path
from jotflow.models import is_repo_name

API = "https://api.github.com"
TIMEOUT_SECONDS = 15.0
MAX_REPO_BYTES = 256 * 1024
# GitHub cuts a recursive tree at 100,000 entries or 7 MB, and says so.
MAX_TREE_BYTES = 8 * 1024 * 1024
MAX_TREE_FILES = 10_000
# A tree is fetched again after this long, so a run's reads see one listing.
TREE_FOR_SECONDS = 60.0
MAX_CACHED_TREES = 32
_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[^\x00-\x20\x7f~^:?*\[\\]{1,255}")


class DocsError(Exception):
    """GitHub didn't give a usable answer. The message is fixed text, safe to show."""


class DocNotFound(DocsError):
    """The path isn't a readable text file in the default branch's tree."""


@dataclass(frozen=True)
class DocEntry:
    path: str
    sha: str
    size: int


@dataclass(frozen=True)
class DocTree:
    entries: tuple[DocEntry, ...]
    truncated: bool  # GitHub or this module left some files out

    def find(self, path: str) -> DocEntry | None:
        return next((e for e in self.entries if e.path == path), None)


@dataclass(frozen=True)
class DocText:
    path: str
    sha: str
    text: str
    truncated: bool  # cut at MAX_FILE_BYTES


class _Repo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_branch: str = Field(min_length=1, max_length=255)


class _TreeItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    mode: str
    type: str
    sha: str
    size: int | None = Field(default=None, ge=0)


class _Tree(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tree: list[Any]
    truncated: bool = False


def client(*, transport: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncClient:
    """An HTTP client for these calls: no redirects, no proxy settings from the environment."""
    return httpx2.AsyncClient(
        transport=transport,
        timeout=httpx2.Timeout(TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    )


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jotflow",
    }


def _checked_repo(repo: str) -> tuple[str, str]:
    if not is_repo_name(repo):
        raise DocsError("not a GitHub repo")
    owner, _, name = repo.partition("/")
    return owner, name


async def _get(
    http: httpx2.AsyncClient, url: str, token: str, cap: int, *, accept: str | None = None
) -> tuple[int, bytes, bool]:
    """(status, body up to ``cap`` bytes, whether the body went over ``cap``)."""
    headers = _headers(token) if accept is None else _headers(token, accept)
    try:
        async with http.stream("GET", url, headers=headers) as response:
            if response.status_code != 200:
                return response.status_code, b"", False
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > cap:
                    return 200, body[:cap], True
            return 200, body, False
    except httpx2.HTTPError:
        raise DocsError("GitHub could not be reached") from None


def _refused(status: int) -> DocsError:
    if status in (403, 404):
        return DocsError(f"GitHub answered {status}: the read token can't read this repo's files")
    return DocsError(f"GitHub answered {status}")


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise DocsError("GitHub's answer was not JSON") from None


async def list_docs(http: httpx2.AsyncClient, token: str, repo: str, *, api: str = API) -> DocTree:
    """The regular text files in ``repo``'s default branch, with their blob shas."""
    owner, name = _checked_repo(repo)
    status, body, over = await _get(http, f"{api}/repos/{owner}/{name}", token, MAX_REPO_BYTES)
    if status != 200:
        raise _refused(status)
    if over:
        raise DocsError("GitHub's answer was too large")
    try:
        branch = _Repo.model_validate(_json(body)).default_branch
    except ValidationError:
        raise DocsError("GitHub's answer was not a repo") from None
    if not _BRANCH.fullmatch(branch):
        raise DocsError("the repo's default branch has an unusable name")
    url = f"{api}/repos/{owner}/{name}/git/trees/{quote(branch, safe='')}?recursive=1"
    status, body, over = await _get(http, url, token, MAX_TREE_BYTES)
    if status != 200:
        raise _refused(status)
    if over:
        raise DocsError("the repo's file tree is too large to list")
    try:
        tree = _Tree.model_validate(_json(body))
    except ValidationError:
        raise DocsError("GitHub's answer was not a file tree") from None
    entries: list[DocEntry] = []
    truncated = tree.truncated
    for raw in tree.tree:
        try:
            item = _TreeItem.model_validate(raw)
        except ValidationError:
            continue
        if item.type != "blob" or item.mode not in FILE_MODES or not _SHA.fullmatch(item.sha):
            continue
        try:
            check_path(item.path)
        except PathRefused:
            continue
        if len(entries) >= MAX_TREE_FILES:
            truncated = True
            break
        entries.append(DocEntry(path=item.path, sha=item.sha, size=item.size or 0))
    return DocTree(entries=tuple(entries), truncated=truncated)


def decode_text(raw: bytes, *, cut: bool) -> str:
    """``raw`` as strict UTF-8 with no NUL, else DocsError.

    When ``cut`` is set, ``raw`` was cut at a byte cap, so a character split at
    the end is dropped rather than refused.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        text = decoder.decode(raw, final=not cut)
    except UnicodeDecodeError:
        raise DocsError("the file is not UTF-8 text") from None
    if "\x00" in text:
        raise DocsError("the file is not text")
    return text


async def read_doc(
    http: httpx2.AsyncClient, token: str, repo: str, entry: DocEntry, *, api: str = API
) -> DocText:
    """The text of one file from :func:`list_docs`, by its blob sha, cut at MAX_FILE_BYTES."""
    owner, name = _checked_repo(repo)
    if not _SHA.fullmatch(entry.sha):
        raise DocsError("not a blob sha")
    url = f"{api}/repos/{owner}/{name}/git/blobs/{entry.sha}"
    status, body, over = await _get(
        http, url, token, MAX_FILE_BYTES, accept="application/vnd.github.raw"
    )
    if status != 200:
        raise _refused(status)
    return DocText(path=entry.path, sha=entry.sha, text=decode_text(body, cut=over), truncated=over)


class DocReader:
    """The runner's doc reads, with a short-lived tree cache so a run's reads agree."""

    def __init__(
        self,
        token: str,
        *,
        http: httpx2.AsyncClient | None = None,
        api: str = API,
        mono: Any = time.monotonic,
    ) -> None:
        self._token = token
        self._http = http
        self._api = api
        self._mono = mono
        self._trees: dict[str, tuple[float, DocTree]] = {}

    async def _client(self) -> httpx2.AsyncClient:
        if self._http is None:
            self._http = client()
        return self._http

    async def tree(self, repo: str) -> DocTree:
        key = repo.casefold()
        now = self._mono()
        cached = self._trees.get(key)
        if cached is not None and now - cached[0] < TREE_FOR_SECONDS:
            return cached[1]
        tree = await list_docs(await self._client(), self._token, repo, api=self._api)
        if len(self._trees) >= MAX_CACHED_TREES:
            self._trees.pop(min(self._trees, key=lambda k: self._trees[k][0]))
        self._trees[key] = (now, tree)
        return tree

    async def read(self, repo: str, path: str) -> DocText:
        try:
            check_path(path)
        except PathRefused:
            raise DocNotFound("not a readable text file path") from None
        entry = (await self.tree(repo)).find(path)
        if entry is None:
            raise DocNotFound("no such text file in the default branch")
        return await read_doc(await self._client(), self._token, repo, entry, api=self._api)
