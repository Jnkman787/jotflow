"""``/jotflow help`` and ``/jotflow repos`` as headed, bulleted sections (#257).

They are Slack ``mrkdwn``, so they can have headings and bullets, which
``plain_text`` can't. Three rules keep that from reopening pings or links:

- every mrkdwn object sets ``verbatim: true``, so Slack never turns a URL, a
  channel name or a user name into a link
- every admin-written field is escaped with :func:`escaped`, which turns ``&``,
  ``<`` and ``>`` into entities, so ``<!here>`` or ``<@U123>`` stays text, and
  swaps ``*``, ``_``, ``~`` and backticks for lookalikes Slack doesn't format
- nicknames and ``owner/name`` go in code spans. Their validators allow no
  backtick, so nothing can close the span early.

The ``text`` that goes with the blocks stays plain, as the notification
fallback. The limits of #231 still hold: 3,000 characters a section, 50 blocks
a message, and "and N more" when a list doesn't fit.
"""

from __future__ import annotations

from typing import Any

from jotflow.models import ChannelMode
from jotflow.orchestrator.help import (
    CLOSING,
    HELP_REPOS_LIMIT,
    INTRO,
    KEYWORD_LINES,
    MODE_LINES,
    NOT_SET_UP,
    REPOS_LIMIT,
    REPOS_MORE,
    SECTION_LIMIT,
    UNLINK_OTHER_LINE,
    RepoRow,
    _capped,
    _named,
    _who_approves,
    as_text,
    channel_help,
    help_text,
    plain_row,
    plain_sections,
    repos_view,
)
from jotflow.orchestrator.keywords import KEYWORDS, REACTIONS
from jotflow.policy import Policy
from jotflow.repo_catalog import RepoCatalog

# Slack's limits for one message.
MAX_BLOCKS = 50
HEADER_LIMIT = 150

_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# Lookalikes for Slack's formatting marks, so an admin's text shows as written
# without turning bold, italic, struck or code.
LOOKALIKES = {
    "*": chr(0x2217),  # asterisk operator
    "_": chr(0x02CD),  # modifier letter low macron
    "~": chr(0x223C),  # tilde operator
    "`": chr(0x02CB),  # modifier letter grave accent
}
_ESCAPES = str.maketrans({**_ENTITIES, **LOOKALIKES})
_ENTITY_ESCAPES = str.maketrans(dict(_ENTITIES))

Blocks = list[dict[str, Any]]


def escaped(value: str) -> str:
    """Admin-written text, safe in a verbatim mrkdwn object: no ping, link or formatting."""
    return value.translate(_ESCAPES)


def code(value: str) -> str:
    """``value`` in a code span, or escaped text if it could close the span itself."""
    if "`" in value:
        return escaped(value)
    return f"`{value.translate(_ENTITY_ESCAPES)}`"


def _mrkdwn(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": _mrkdwn(text)}


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:HEADER_LIMIT]}}


def _pack(heading: str, lines: list[str]) -> Blocks:
    """``heading`` and ``lines`` in as few sections as fit, none over SECTION_LIMIT."""
    sections: list[str] = []
    current = heading
    for line in lines:
        if len(current) + 1 + len(line) > SECTION_LIMIT:
            sections.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    sections.append(current)
    return [_section(text) for text in sections if text.strip()]


def _shown(items: list[str], limit: int, more: str) -> int:
    """How many of ``items`` the plain text keeps within ``limit``, as ``_capped`` decides."""
    kept = _capped(items, limit, more)
    return len(items) if kept == items else len(kept) - 1


def _keywords() -> list[str]:
    return [f"• {code(KEYWORD_LINES[k][0])} {KEYWORD_LINES[k][1]}" for k in KEYWORDS]


def _reactions() -> list[str]:
    lines = [f"• :{name}: works like {code(KEYWORD_LINES[k][0])}" for name, k in REACTIONS.items()]
    return [*lines, "Remove your reaction to take it back."]


def _commands() -> list[str]:
    return [
        f"• {code('/jotflow repos')} lists the repos this channel feeds",
        f"• {code('/jotflow link your-github-login')} lets proposals assign you, "
        f"and {code('/jotflow unlink')} undoes it",
        f"• {UNLINK_OTHER_LINE}",
        f"• {code('/jotflow help')} shows this again",
    ]


def help_reply(
    policy: Policy | None,
    channel_id: str,
    mode: ChannelMode = ChannelMode.PROPOSE,
    repos: RepoCatalog | None = None,
) -> tuple[str, Blocks]:
    """``/jotflow help``: the plain fallback text, and its headed sections."""
    if policy is not None and channel_id not in policy.channels:
        return NOT_SET_UP, plain_sections(NOT_SET_UP)
    text = help_text() if policy is None else channel_help(policy, channel_id, mode, repos)
    blocks: Blocks = [_header(":wave: JotFlow help")]
    blocks += _pack(f"*:label: Keywords*\n{INTRO}", _keywords())
    blocks += _pack("*:raised_hand: Reactions*", _reactions())
    if policy is not None:
        catalog = repos if repos is not None else RepoCatalog.static(policy)
        fed = catalog.repos(channel_id)
        named = [_named(r.nickname, r.repo) for r in fed]
        shown = _shown(named, HELP_REPOS_LIMIT, "and {n} more; see /jotflow repos")
        lines = [_bullet(r.nickname, r.repo) for r in fed[:shown]]
        if shown < len(fed):
            lines.append(f"and {len(fed) - shown} more; see {code('/jotflow repos')}")
        lines += [escaped(s) for s in catalog.summaries(channel_id)]
        blocks += _pack("*:file_folder: Repos*", lines)
        approval = [MODE_LINES[mode]]
        if mode is ChannelMode.PROPOSE:
            approval.append(_who_approves(policy))
        blocks += _pack("*:white_check_mark: Approval*", approval)
    blocks += _pack("*:keyboard: Commands*", _commands())
    blocks.append(_section(f"_{CLOSING}_"))
    return text, blocks


def _bullet(nickname: str, repo: str) -> str:
    return f"• {code(nickname)} {code(repo)}" if nickname else f"• {code(repo)}"


def _row_bullet(row: RepoRow) -> str:
    head = _bullet(row.nickname, row.repo)
    return f"{head}: {escaped(row.description)}" if row.description else head


def repos_reply(
    policy: Policy, channel_id: str, argument: str, repos: RepoCatalog | None = None
) -> tuple[str, Blocks]:
    """``/jotflow repos``: every repo as a bullet, within Slack's limits."""
    view = repos_view(policy, channel_id, argument, repos)
    if isinstance(view, str):
        return view, plain_sections(view)
    rows = view.rows
    title = view.heading.rstrip(":") if view.word else "Repos this channel feeds"
    heading = f"*:file_folder: {escaped(title)}*"
    bullets = [_row_bullet(row) for row in rows]
    plain = [plain_row(row) for row in rows]
    scopes_text = [as_text(s) for s in view.scopes]
    scopes_blocks = [escaped(s) for s in view.scopes]

    def build(shown: int) -> tuple[str, Blocks]:
        cut = shown < len(rows)
        more = [REPOS_MORE.format(n=len(rows) - shown)] if cut else []
        lines = [*bullets[:shown], *more, *scopes_blocks]
        text_lines = [*plain[:shown], *("  " + m for m in more), *scopes_text]
        return "\n".join([as_text(view.heading), *text_lines]), _pack(heading, lines)

    def fits(reply: tuple[str, Blocks]) -> bool:
        return len(reply[1]) <= MAX_BLOCKS and len(reply[0]) <= REPOS_LIMIT

    # As many as fit both the plain text and the blocks, with room for the "more" line.
    # A scope can find a thousand repos, so search rather than drop one at a time.
    if fits(whole := build(len(rows))):
        return whole
    low, high = 0, len(rows)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if fits(build(middle)) else (low, middle)
    return build(low)
