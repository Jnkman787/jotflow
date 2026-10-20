"""Every word JotFlow says to help people in a channel (#80, D10).

``/jotflow help``, the pinned guide, the newcomer tip, the near-miss nudge, and
the keyword list in docs/usage.md all come from here, so they never
disagree. Each line is built from the keyword module's own lists, so a new
keyword or reaction cannot be missing from one of them: a test checks that.

Everything here is posted as ``plain_text``, so nothing in it can ping or link,
except the ``/jotflow help`` and ``/jotflow repos`` replies. help_blocks.py
formats those as verbatim mrkdwn, with every admin-written field escaped
(#257). This module's text stays their plain fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from jotflow.models import ChannelMode
from jotflow.orchestrator.keywords import KEYWORDS, REACTIONS, Keyword, near_miss, split_keyword
from jotflow.policy import Policy
from jotflow.repo_catalog import RepoCatalog
from jotflow.store.base import Store

log = logging.getLogger("jotflow.help")

# One line per keyword: what to type, and what it does.
KEYWORD_LINES: dict[Keyword, tuple[str, str]] = {
    "idea": ("idea:", "this is an idea; the agent takes it as a hint"),
    "bug": ("bug:", "this is a bug; the agent takes it as a hint"),
    "re": (
        "re: web#42",
        "add this to an existing issue; re: 42 works when the channel has one repo",
    ),
    "skip": ("skip:", "not an idea; on a top-level message it keeps the whole thread out"),
    "ask": ("ask:", "a question for JotFlow; the agent answers in the thread (#57)"),
}

INTRO = "Start a message with a keyword to steer JotFlow. The colon is required."
CLOSING = "Most people never need these: just post, and JotFlow works out the rest."
LINK_LINE = "/jotflow link your-github-login lets proposals assign you; /jotflow unlink undoes it."
# Removing someone else's link takes their member ID, never a name (#173).
UNLINK_OTHER_LINE = (
    "Approvers can remove someone else's link with /jotflow unlink and their Slack member ID."
)
REPOS_LINE = (
    "/jotflow repos lists the repos this channel feeds; add a word to narrow it, "
    "or change to see how to edit them."
)
# Proving a linked login is yours (#195).
VERIFY_LINE = (
    "/jotflow verify proves your GitHub login is yours, so cards stop marking it unverified."
)
COMMAND = "/jotflow help"


def keyword_lines() -> list[str]:
    """The keyword list, in the order the keyword module defines them."""
    width = max(len(form) for form, _ in KEYWORD_LINES.values())
    return [f"  {KEYWORD_LINES[k][0].ljust(width)}  {KEYWORD_LINES[k][1]}" for k in KEYWORDS]


def reaction_lines() -> list[str]:
    """One line naming each keyword's reaction, from the single list in reactions.py."""
    pairs = [f":{name}: for {keyword}:" for name, keyword in REACTIONS.items()]
    listed = ", ".join(pairs[:-1]) + (", or " if len(pairs) > 1 else "") + pairs[-1]
    return [f"Or react instead: {listed}. Remove your reaction to take it back."]


def help_text() -> str:
    """What ``/jotflow help`` shows, privately."""
    return "\n".join(
        [
            INTRO,
            *keyword_lines(),
            *reaction_lines(),
            LINK_LINE,
            UNLINK_OTHER_LINE,
            VERIFY_LINE,
            REPOS_LINE,
            CLOSING,
        ]
    )


def guide_text(bot: str = "JotFlow") -> str:
    """The short guide JotFlow pins once in each channel it watches."""
    forms = ", ".join(KEYWORD_LINES[k][0] for k in KEYWORDS)
    return "\n".join(
        [
            f"{bot} turns ideas and bugs posted here into GitHub issues, after a person approves.",
            "Just post as usual. To steer it, start a message with a keyword and a colon: "
            f"{forms}.",
            f"Type {COMMAND} to see what each one does. Only you see the answer.",
        ]
    )


TIP = (
    f"Tip: JotFlow is watching this channel for ideas and bugs. Type {COMMAND} to see the "
    "keywords that steer it. Only you can see this, and it appears once."
)


def nudge(keyword: Keyword) -> str:
    """The private suggestion for a mistyped keyword. JotFlow did not act on the message."""
    form = KEYWORD_LINES[keyword][0]
    return (
        f"Did you mean {form}? Keywords need a colon right after the word, as in {form} ... "
        f"JotFlow did not act on this message. Type {COMMAND} for the full list. "
        "Only you can see this, and it appears once."
    )


# The channel's own facts for /jotflow help


MODE_LINES = {
    ChannelMode.PROPOSE: (
        "Here, JotFlow posts each proposal as a card and waits for a person to approve it."
    ),
    ChannelMode.AUTO: "Here, JotFlow writes each issue straight away and then posts it as a card.",
    ChannelMode.PAUSED: (
        "JotFlow is paused here: it starts nothing new and posts nothing. "
        "A run already in progress may finish."
    ),
}
NOT_SET_UP = (
    "This channel isn't set up for JotFlow yet, so it watches nothing here. "
    "An admin adds the channel to jotflow.toml."
)


# Slack's limits: a plain_text section holds 3,000 characters, a message 50
# blocks, and a message's text 40,000 characters (#231).
SECTION_LIMIT = 3000
# Help names the channel's repos in one line, cut here; /jotflow repos lists them all.
HELP_REPOS_LIMIT = 1000
# /jotflow repos stops here: under the text cap, and at most 13 sections.
REPOS_LIMIT = 39_000


def _capped(items: list[str], limit: int, more: str) -> list[str]:
    """As many of ``items`` as fit in ``limit`` characters, then ``more`` with the count."""
    kept: list[str] = []
    used = 0
    for index, item in enumerate(items):
        rest = len(items) - index
        tail = len(more.format(n=rest - 1)) if rest > 1 else 0
        if used + len(item) + tail > limit:
            return [*kept, more.format(n=rest)]
        kept.append(item)
        used += len(item) + 2
    return kept


def _who_approves(policy: Policy) -> str:
    approval = policy.approval
    if not approval.approvers_enabled:
        return "Anyone in the channel can approve."
    if approval.require_second_approver:
        return (
            "Only the channel's approvers can approve, and never a proposal drawn from their "
            "own messages."
        )
    return (
        "Only the channel's approvers can approve, including proposals drawn from their "
        "own messages."
    )


def channel_help(
    policy: Policy, channel_id: str, mode: ChannelMode, repos: RepoCatalog | None = None
) -> str:
    """``/jotflow help`` in one channel: the keywords, its repos, and how approval works."""
    if channel_id not in policy.channels:
        return NOT_SET_UP
    catalog = repos if repos is not None else RepoCatalog.static(policy)
    named = [_named(r.nickname, r.repo) for r in catalog.repos(channel_id)]
    fed = ", ".join(_capped(named, HELP_REPOS_LIMIT, "and {n} more; see /jotflow repos"))
    lines = [help_text(), ""]
    if named:
        lines.append(f"This channel feeds: {fed}.")
    lines += [*catalog.summaries(channel_id), MODE_LINES[mode]]
    if mode is ChannelMode.PROPOSE:
        lines.append(_who_approves(policy))
    return "\n".join(lines)


# Posting help, quietly


class HelpSlack(Protocol):
    """The Slack calls help makes. Tests use a fake."""

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        """An ephemeral message only ``user_id`` sees."""
        ...

    async def post_guide(self, channel_id: str, text: str) -> str:
        """Post ``text`` in the channel. Returns the message's ts."""
        ...

    async def pin(self, channel_id: str, ts: str) -> None: ...


class HelpHandler:
    """The pinned guide, the newcomer tip, and the near-miss nudge (#80).

    Each is sent at most once, recorded in the store before it is sent, so it
    holds across restarts and two quick messages never both get one. At most
    one notice goes with any message. A failed send is not retried.
    """

    def __init__(self, store: Store, policy: Policy, slack: HelpSlack) -> None:
        self._store = store
        self._policy = policy
        self._slack = slack
        self._guided: set[str] = set()

    async def ensure_guide(self, channel_id: str) -> None:
        """Post and pin the channel's guide, unless it was posted before or is turned off."""
        if channel_id in self._guided or channel_id not in self._policy.channels:
            return
        self._guided.add(channel_id)
        if not self._policy.help_for(channel_id).guide:
            return
        if not await self._store.claim_notice(channel_id, "", "guide"):
            return
        try:
            ts = await self._slack.post_guide(channel_id, guide_text())
        except Exception:
            # Nothing was posted, so free the claim: the next message tries again.
            await self._store.release_notice(channel_id, "", "guide")
            self._guided.discard(channel_id)
            raise
        try:
            await self._slack.pin(channel_id, ts)
        except Exception:
            # The guide is posted; pinning it again later could only duplicate it.
            log.warning("posted the help guide in %s but could not pin it", channel_id)
            return
        log.info("pinned the help guide in %s", channel_id)

    async def on_message(self, event: Mapping[str, Any]) -> str | None:
        """Maybe help the person who posted ``event``. Returns the notice sent, if any."""
        channel = str(event.get("channel") or "")
        user = str(event.get("user") or "")
        if not channel or not user or channel not in self._policy.channels:
            return None
        await self.ensure_guide(channel)
        settings = self._policy.help_for(channel)
        text = str(event.get("text") or "")
        thread = event.get("thread_ts") if isinstance(event.get("thread_ts"), str) else None
        if split_keyword(text)[0] is not None:
            # They already know the keywords: no tip, now or later.
            await self._store.claim_notice(channel, user, "tip")
            return None
        missed = near_miss(text)
        if (
            missed is not None
            and settings.near_miss_nudge
            and await self._store.claim_notice(channel, user, "nudge")
        ):
            await self._store.claim_notice(channel, user, "tip")
            await self._slack.whisper(channel, user, thread, nudge(missed))
            return "nudge"
        if settings.newcomer_tip and await self._store.claim_notice(channel, user, "tip"):
            await self._slack.whisper(channel, user, thread, TIP)
            return "tip"
        return None


def _plain_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "section", "text": {"type": "plain_text", "text": text, "emoji": False}}]


def _pieces(line: str) -> list[str]:
    """``line`` in parts of at most SECTION_LIMIT characters, broken at a space if one fits."""
    parts: list[str] = []
    while len(line) > SECTION_LIMIT:
        cut = line.rfind(" ", 0, SECTION_LIMIT + 1)
        if cut <= 0:
            cut = SECTION_LIMIT
        parts.append(line[:cut])
        line = line[cut:].lstrip(" ")
    parts.append(line)
    return parts


def plain_sections(text: str) -> list[dict[str, Any]]:
    """``text`` as plain_text sections of at most SECTION_LIMIT characters, split on lines.

    Slack rejects a section with no visible text, so a blank chunk is left out, and
    blank text gives no sections at all.
    """
    chunks: list[str] = []
    current: str | None = None
    for line in (piece for raw in text.split("\n") for piece in _pieces(raw)):
        if current is None:
            current = line
        elif len(current) + 1 + len(line) <= SECTION_LIMIT:
            current += "\n" + line
        else:
            chunks.append(current)
            current = line
    chunks.append(current or "")
    return [block for chunk in chunks if chunk.strip() for block in _plain_blocks(chunk)]


class BoltHelpSlack:
    """:class:`HelpSlack` over slack-sdk's client. Text is plain, and links never unfurl."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def whisper(
        self, channel_id: str, user_id: str, thread_ts: str | None, text: str
    ) -> None:
        arguments: dict[str, Any] = {
            "channel": channel_id,
            "user": user_id,
            "text": text,
            "blocks": _plain_blocks(text),
        }
        if thread_ts:
            arguments["thread_ts"] = thread_ts
        await self._client.chat_postEphemeral(**arguments)

    async def post_guide(self, channel_id: str, text: str) -> str:
        response = await self._client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=_plain_blocks(text),
            unfurl_links=False,
            unfurl_media=False,
        )
        return str(response["ts"])

    async def pin(self, channel_id: str, ts: str) -> None:
        await self._client.pins_add(channel=channel_id, timestamp=ts)


def _named(nickname: str, repo: str) -> str:
    return f"{nickname} ({repo})" if nickname else repo


# /jotflow repos (#91): the channel's repos, read-only


REPOS_CHANGE = (
    "JotFlow's repos live in jotflow.toml, which a running JotFlow can't change. On the "
    "machine that runs JotFlow, edit that file's [[channels.<id>.repos]] or "
    "[[channels.<id>.scopes]] entries; "
    "examples/jotflow.example.toml in JotFlow's repo explains each line. Then restart JotFlow with "
    "`docker compose restart jotflow`, so it reads the file again. Also give the agents' "
    "GitHub tokens access to a repo you add, on GitHub: JotFlow can't. A scope finds a new "
    "repo by itself within 15 minutes, once the tokens can reach it."
)
# `/jotflow repos change` says how; any other word filters the list (#236).
# `add …` and `remove …` say how too, as they did before (#91).
CHANGE_WORD = "change"
CHANGE_WORDS = frozenset({CHANGE_WORD, "add", "remove"})
FILTER_LIMIT = 40
REPOS_MORE = "and {n} more; add a word to narrow the list, such as /jotflow repos web."


@dataclass(frozen=True)
class RepoRow:
    """One repo as /jotflow repos shows it. ``description`` may be GitHub's, cleaned."""

    nickname: str  # "" when a found repo has none free
    repo: str
    description: str
    topics: tuple[str, ...] = ()

    def matches(self, word: str) -> bool:
        wanted = word.casefold()
        fields = (self.nickname, self.repo, self.description, *self.topics)
        return any(wanted in field.casefold() for field in fields)


@dataclass(frozen=True)
class ReposView:
    """What /jotflow repos shows: a heading, the repos, and a line per scope."""

    heading: str
    rows: list[RepoRow]
    scopes: list[str]
    word: str = ""


def repos_view(
    policy: Policy, channel_id: str, argument: str, repos: RepoCatalog | None = None
) -> ReposView | str:
    """The repos to list, or the whole reply when there is no list to show."""
    if channel_id not in policy.channels:
        return NOT_SET_UP
    word = argument.strip()
    first = word.split(maxsplit=1)[0].casefold() if word else ""
    if first in CHANGE_WORDS:
        return REPOS_CHANGE
    catalog = repos if repos is not None else RepoCatalog.static(policy)
    rows = []
    for entry in catalog.repos(channel_id):
        found = catalog.details(channel_id, entry.repo)
        rows.append(
            RepoRow(
                entry.nickname,
                entry.repo,
                found.description or entry.description,
                found.topics,
            )
        )
    scopes = catalog.summaries(channel_id)
    if not word:
        return ReposView("This channel feeds:", rows, scopes)
    word = word[:FILTER_LIMIT]
    hits = [row for row in rows if row.matches(word)]
    if not hits:
        return ReposView(
            f"No repo this channel feeds matches {word}. "
            f"/jotflow repos {CHANGE_WORD} says how to add one.",
            [],
            scopes,
            word,
        )
    return ReposView(f"Repos this channel feeds matching {word}:", hits, scopes, word)


def plain_row(row: RepoRow) -> str:
    label = f"{row.nickname}: {row.repo}" if row.nickname else row.repo
    return f"  {label}. {as_text(row.description)}" if row.description else f"  {label}"


_FALLBACK_ENTITIES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def as_text(value: str) -> str:
    """Admin-written text for a reply's plain ``text``: no ``<!here>``, ``<@U…>`` or link.

    Slack still reads ``<…>`` markup in a message's top-level text, which it shows
    in notifications, so escape it the same way the blocks do (#259).
    """
    return value.translate(_FALLBACK_ENTITIES)


def channel_repos(
    policy: Policy, channel_id: str, argument: str, repos: RepoCatalog | None = None
) -> str:
    """``/jotflow repos``: list this channel's repos, filter them, or say how to change them."""
    view = repos_view(policy, channel_id, argument, repos)
    if isinstance(view, str):
        return view
    listed = [plain_row(row) for row in view.rows]
    lines = [as_text(view.heading), *_capped(listed, REPOS_LIMIT, "  " + REPOS_MORE)]
    return "\n".join([*lines, *(as_text(s) for s in view.scopes)])
