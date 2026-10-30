"""Reading a Slack file's text for drafting and for the issue (#333).

Only JotFlow's orchestrator reads a file, never an agent or a runner, and only
under security's conditions:

- the URL is the file's own ``url_private`` from Slack's data, on exactly
  ``https://files.slack.com``, never one taken from message text;
- the bot token goes in that one request's Authorization header;
- no redirect is followed, and no proxy is taken from the environment;
- the file's type and mimetype are both on the allowlist, whatever its name;
- at most :data:`MAX_ATTACHMENT_BYTES` are read, and anything longer is too large;
- the text is strict UTF-8 with no NUL, and invisible characters are removed;
- a file whose text looks like it holds a credential is never offered.

A file that fails any of these gets a fixed reason instead of its text. A file's
text is never logged, and nothing here stores it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2

from jotflow.execution.writes import clean_name as clean_issue_name
from jotflow.models import MAX_ATTACHMENT_BYTES, SLACK_FILE_ID
from jotflow.text import without_unsafe_chars

log = logging.getLogger("jotflow.files")

# The tool-call ledger's record of a file a run's packet gave text for.
SERVED_FILE = "packet_file"
# The ledger's record of an approved file execution left out: deleted or changed.
LEFT_OUT = "attachment_left_out"
FILE_HOST = "files.slack.com"
MAX_FILES_PER_MESSAGE = 5
MAX_THREAD_TEXT = 64 * 1024
# Read attempts a thread's packet makes, whatever each file's size or outcome:
# empty or failing files cost a download too (CodeRabbit on #346).
MAX_THREAD_READS = 10
NAME_LIMIT = 100
TIMEOUT_SECONDS = 10.0

# Slack's filetype for text files, and the mimetypes JotFlow reads. Both must match.
TEXT_FILETYPES = frozenset({"text", "markdown", "csv", "json", "yaml", "plain"})
TEXT_MIMETYPES = frozenset({"application/json", "application/x-yaml", "application/yaml"})

NOT_TEXT = "not a text file JotFlow reads"
TOO_LARGE = "too large: JotFlow reads text files up to 32 KiB"
THREAD_FULL = "not read: the thread's files already reach 64 KiB"
READ_LIMIT = "not read: JotFlow reads at most 10 files a thread"
NOT_READABLE = "not readable as plain UTF-8 text"
LOOKS_SECRET = "not read: it looks like it holds a password, key or token"
UNAVAILABLE = "couldn't be read from Slack just now"
TEXT_OFF = "this channel's attachments setting shows names only"

_FILE_ID = re.compile(SLACK_FILE_ID)
_PATH = re.compile(r"/files-pri/[A-Z0-9]+-F[A-Z0-9]+/[^?#]+")

# Credential shapes (security's condition 1). A false positive only means "not attached".
_SECRETS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)
# A line that sets a password, secret, token or key: the keyword, then within a few
# characters ":" or "=" and a value. Found with str.find and one bounded, anchored
# match per hit, never a pattern with overlapping repeats, so it's linear (#346 review).
_SECRET_WORDS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "privatekey",
    "private_key",
    "private-key",
)
# Quotes too, so a JSON or YAML key such as "password": "x" counts.
_ASSIGNED = re.compile(r"[\w.\"'-]{0,32}\s{0,8}[:=]\s{0,8}\S")


def _sets_a_secret(text: str) -> bool:
    for line in text.splitlines():
        # Each hit costs one bounded match, so a whole line is still linear.
        lowered = line.lower()
        for word in _SECRET_WORDS:
            at = lowered.find(word)
            while at != -1:
                if _ASSIGNED.match(lowered, at + len(word)):
                    return True
                at = lowered.find(word, at + 1)
    return False


def looks_secret(text: str) -> bool:
    """Whether ``text`` has a credential's shape: a token, key, JWT or a line that sets
    a password or secret. Best-effort: a safety net for common shapes, not a guarantee.
    """
    return _sets_a_secret(text) or any(pattern.search(text) for pattern in _SECRETS)


class FileSlack(Protocol):
    """The two Slack calls reading a file makes. Tests use a fake."""

    async def info(self, file_id: str) -> Mapping[str, Any]: ...

    async def download(self, url: str, limit: int) -> bytes: ...


class TooLarge(Exception):
    """The download passed its byte limit."""


@dataclass(frozen=True)
class FileFacts:
    """What Slack's own data says about one file: never its URL to anyone else."""

    file_id: str
    name: str
    filetype: str
    mimetype: str
    size: int
    url: str | None  # url_private, for JotFlow's own fetch only
    channels: frozenset[str]  # where Slack says it's shared


@dataclass(frozen=True)
class FileText:
    """A file's text as read, or the fixed reason it wasn't."""

    text: str | None = None
    sha256: str | None = None
    reason: str | None = None


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def clean_name(name: str) -> str:
    """A file name as plain text: no invisible characters, capped, never empty."""
    cleaned = " ".join(without_unsafe_chars(name).split())[:NAME_LIMIT]
    return cleaned or "file"


def facts(raw: Mapping[str, Any]) -> FileFacts | None:
    """One file from Slack's message or ``files.info`` data, or None if it's malformed."""
    file_id = _str(raw.get("id"))
    if not _FILE_ID.fullmatch(file_id):
        return None
    size = raw.get("size")
    shared: set[str] = set()
    for key in ("channels", "groups", "ims"):
        found = raw.get(key)
        if isinstance(found, list):
            shared |= {c for c in found if isinstance(c, str)}
    url = _str(raw.get("url_private")) or None
    return FileFacts(
        file_id=file_id,
        name=clean_name(_str(raw.get("name")) or _str(raw.get("title"))),
        filetype=_str(raw.get("filetype")).lower(),
        mimetype=_str(raw.get("mimetype")).lower(),
        size=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
        url=url,
        channels=frozenset(shared),
    )


def message_files(raw: Mapping[str, Any]) -> list[FileFacts]:
    """A message's files, at most :data:`MAX_FILES_PER_MESSAGE`."""
    found = raw.get("files")
    if not isinstance(found, list):
        return []
    out = [f for f in (facts(item) for item in found if isinstance(item, Mapping)) if f]
    return out[:MAX_FILES_PER_MESSAGE]


def is_text(file: FileFacts) -> bool:
    """Both Slack's filetype and the mimetype are text, whatever the file is named."""
    mime = file.mimetype.split(";", 1)[0].strip()
    return file.filetype in TEXT_FILETYPES and (mime.startswith("text/") or mime in TEXT_MIMETYPES)


def safe_url(url: str | None) -> bool:
    """Exactly https://files.slack.com, a url_private path, and nothing else."""
    if not url:
        return False
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == FILE_HOST
        and parts.port is None
        and not parts.username
        and not parts.password
        and _PATH.fullmatch(parts.path) is not None
        and not parts.fragment
    )


def decode(data: bytes) -> str | None:
    """Strict UTF-8, no NUL, invisible characters removed, or None."""
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Line by line, so newlines and tabs survive: the cleaner is for one-line text.
    parts = re.split(r"(\r?\n|\t)", text.removeprefix("\N{BYTE ORDER MARK}"))
    kept = ("\n", "\r\n", "\t")
    return "".join(part if part in kept else without_unsafe_chars(part) for part in parts)


async def read_text(file: FileFacts, slack: FileSlack) -> FileText:
    """``file``'s text, if it's a small text file JotFlow may read, else the reason."""
    if not is_text(file):
        return FileText(reason=NOT_TEXT)
    if file.size > MAX_ATTACHMENT_BYTES:
        return FileText(reason=TOO_LARGE)
    if not safe_url(file.url):
        return FileText(reason=UNAVAILABLE)
    assert file.url is not None
    try:
        data = await slack.download(file.url, MAX_ATTACHMENT_BYTES)
    except TooLarge:
        return FileText(reason=TOO_LARGE)
    except Exception as exc:
        log.warning("file %s couldn't be read: %s", file.file_id, type(exc).__name__)
        return FileText(reason=UNAVAILABLE)
    text = decode(data)
    if text is None:
        return FileText(reason=NOT_READABLE)
    # Off the event loop, so no regex cost ever stalls JotFlow (#346 review).
    if await asyncio.to_thread(looks_secret, text):
        log.info("file %s not read: it has a credential's shape", file.file_id)
        return FileText(reason=LOOKS_SECRET)
    return FileText(text=text, sha256=hashlib.sha256(data).hexdigest())


async def current(file_id: str, slack: FileSlack) -> FileFacts | None:
    """The file as Slack says it is now, through ``files.info``, or None if it's gone."""
    try:
        raw = await slack.info(file_id)
    except Exception as exc:
        log.warning("file %s couldn't be looked up: %s", file_id, type(exc).__name__)
        return None
    found = facts(raw)
    return found if found is not None and found.file_id == file_id else None


class BoltFileSlack:
    """:class:`FileSlack` over slack-sdk for ``files.info``, and a strict client for
    the download: no redirects, no environment proxy, and a byte cap."""

    def __init__(self, client: Any, *, http: httpx2.AsyncClient | None = None) -> None:
        self._client = client
        self._http = http

    async def info(self, file_id: str) -> Mapping[str, Any]:
        response = await self._client.files_info(file=file_id)
        data = response.data if isinstance(response.data, dict) else {}
        found = data.get("file")
        return found if isinstance(found, Mapping) else {}

    async def download(self, url: str, limit: int) -> bytes:
        if not safe_url(url):
            raise ValueError("not a Slack file URL")
        http = self._http or httpx2.AsyncClient(
            timeout=httpx2.Timeout(TIMEOUT_SECONDS), follow_redirects=False, trust_env=False
        )
        headers = {"Authorization": f"Bearer {self._client.token}"}
        try:
            async with http.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"Slack answered {response.status_code}")
                data = b""
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise TooLarge
                return data
        finally:
            if self._http is None:
                await http.aclose()


def attachments_section(files: Sequence[tuple[str, str]]) -> str:
    """The issue's ``## Attachments`` section, built by JotFlow, never the agent.

    Each file's text goes in a fenced code block one backtick longer than its
    longest run, so nothing in it can ping, link, render or close the block.
    """
    if not files:
        return ""
    parts = ["## Attachments"]
    for name, text in files:
        longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        # No @, links or markup: a file name could ping or link as well as text can.
        safe_name = clean_issue_name(name) or "file"
        parts.append(f"**{safe_name}** (attached in Slack)\n\n{fence}\n{text.rstrip()}\n{fence}")
    return "\n\n".join(parts)
