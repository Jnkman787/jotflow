"""Logging that never carries Slack message text or a secret (#25).

Every line JotFlow writes goes through :class:`RedactingFormatter`, which
formats the record, including any traceback, and then redacts the finished
text. Redacting the final string, rather than a record's arguments, catches
``%r`` arguments, exception messages, and whatever a library logs.

Two kinds of redaction:

- **Secrets, always.** Slack, GitHub, and JotFlow run tokens, bearer values,
  and private keys are replaced, even with ``--log-bodies``.
- **Message text, by default.** Slack payload fields that carry what people
  wrote, ``text`` and the attachment, unfurl, and file fields, are replaced in
  library lines. JotFlow's own modules never log text in the first place.

Third-party loggers, which print whole payloads at DEBUG, stay capped at INFO.
``jotflow run --log-bodies`` lifts that cap and the text redaction for local
development only: it warns loudly, and it refuses inside JotFlow's image and
in CI.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
REDACTED = "[redacted]"

# These libraries print whole event payloads or request bodies at DEBUG.
THIRD_PARTY_LOGGERS = ("slack_bolt", "slack_sdk", "aiohttp", "mcp", "uvicorn", "httpx2")

BODIES_WARNING = (
    "--log-bodies is on: Slack message text will appear in these logs. "
    "Use it only on your own machine, and never share the output."
)

_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys, before anything else touches their lines.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[redacted private key]",
    ),
    # Slack: xoxb-, xoxp-, xoxa-, xoxe-, xoxs-, xoxr-, and app-level xapp-.
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]+"), "[redacted token]"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]+"), "[redacted token]"),
    # JotFlow's own MCP tokens: per-run (#16) and operator (#58).
    (re.compile(r"\bjf[ro]_[A-Za-z0-9_-]+"), "[redacted token]"),
    # GitHub personal, OAuth, user, server, and refresh tokens.
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]+"), "[redacted token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[redacted token]"),
    # Any bearer value, in a header or anywhere else.
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted token]"),
)

# Fields where Slack carries what people wrote: message text, and the attachment,
# unfurl, block, and shared-file fields that bots and integrations fill in.
_KEYS = r"(?:text|fallback|pretext|title|value|footer|comment|preview|plain_text)"

_TEXT_FIELDS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JSON: "text": "..."
    (re.compile(rf'("{_KEYS}"\s*:\s*)"(?:\\.|[^"\\])*"'), rf'\1"{REDACTED}"'),
    # JSON serialized inside a JSON string: \"text\": \"...\". The value ends at the
    # first \" whose backslash is not itself escaped, so \\\" inside it is skipped.
    (re.compile(rf'(\\"{_KEYS}\\"\s*:\s*\\").*?(?<!\\)\\"'), rf'\1{REDACTED}\\"'),
    # Python reprs: 'text': '...' or 'text': "..."
    (re.compile(rf"('{_KEYS}'\s*:\s*)'(?:\\.|[^'\\])*'"), rf"\1'{REDACTED}'"),
    (re.compile(rf"""('{_KEYS}'\s*:\s*)"(?:\\.|[^"\\])*\""""), rf'\1"{REDACTED}"'),
    # Keyword reprs and form bodies: text=...
    (re.compile(rf"(\b{_KEYS}=)'(?:\\.|[^'\\])*'"), rf"\1'{REDACTED}'"),
    (re.compile(rf'(\b{_KEYS}=)"(?:\\.|[^"\\])*"'), rf'\1"{REDACTED}"'),
    (re.compile(rf"(\b{_KEYS}=)(?!['\"\[])[^&\s,)]*"), rf"\1{REDACTED}"),
)


def redact(text: str, *, bodies: bool = False) -> str:
    """``text`` with secrets removed, and with message text removed unless ``bodies``."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    if not bodies:
        for pattern, replacement in _TEXT_FIELDS:
            text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats a record, traceback and stack included, then redacts the whole line."""

    def __init__(self, fmt: str = FORMAT, *, bodies: bool = False) -> None:
        super().__init__(fmt)
        self.bodies = bodies

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), bodies=self.bodies)


class BodiesRefused(Exception):
    """``--log-bodies`` was asked for where it must not be used."""


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "0", "false", "no"}


def check_bodies_allowed(environ: Mapping[str, str]) -> None:
    """Refuse ``--log-bodies`` in CI and inside JotFlow's image, where logs are kept."""
    if _truthy(environ.get("CI")):
        raise BodiesRefused("--log-bodies never runs in CI")
    if _truthy(environ.get("JOTFLOW_IMAGE")):
        raise BodiesRefused(
            "--log-bodies is for local development; it is refused inside JotFlow's image, "
            "whose logs Docker keeps"
        )


def configure_logging(level: str, *, bodies: bool = False) -> None:
    """Send every log line through the redacting formatter, at ``level``."""
    numeric = logging.getLevelName(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(bodies=bodies))
    logging.basicConfig(level=numeric, handlers=[handler], force=True)
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(numeric if bodies else max(numeric, logging.INFO))
    if bodies:
        if numeric > logging.WARNING:
            # A quieter level would hide the warning; this one must always show.
            print(f"WARNING {BODIES_WARNING}", file=sys.stderr)
        else:
            logging.getLogger("jotflow").warning(BODIES_WARNING)
