"""Every word JotFlow says to help people in a channel (#80, D10).

``/jotflow help``, the pinned guide, the newcomer tip, the near-miss nudge, and
the README's "Using it" keyword list all come from here, so they never
disagree. Each line is built from the keyword module's own lists, so a new
keyword or reaction cannot be missing from one of them: a test checks that.

Everything here is posted as ``plain_text``, so nothing in it can ping or link.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from jotflow.models import ChannelMode
from jotflow.orchestrator.keywords import KEYWORDS, REACTIONS, Keyword, near_miss, split_keyword
from jotflow.policy import Policy
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
}

INTRO = "Start a message with a keyword to steer JotFlow. The colon is required."
CLOSING = "Most people never need these: just post, and JotFlow works out the rest."
LINK_LINE = "/jotflow link your-github-login lets proposals assign you; /jotflow unlink undoes it."
REPOS_LINE = "/jotflow repos lists the repos this channel feeds."
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
    return "\n".join([INTRO, *keyword_lines(), *reaction_lines(), LINK_LINE, REPOS_LINE, CLOSING])


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
    ChannelMode.PAUSED: "JotFlow is paused here: it reads nothing and writes nothing.",
}
NOT_SET_UP = (
    "This channel isn't set up for JotFlow yet, so it watches nothing here. "
    "An admin adds the channel to jotflow.toml."
)


def channel_help(policy: Policy, channel_id: str, mode: ChannelMode) -> str:
    """``/jotflow help`` in one channel: the keywords, its repos, and how approval works."""
    if channel_id not in policy.channels:
        return NOT_SET_UP
    channel = policy.channels[channel_id]
    repos = ", ".join(f"{r.nickname} ({r.repo})" for r in channel.repos)
    lines = [help_text(), "", f"This channel feeds: {repos}.", MODE_LINES[mode]]
    if mode is ChannelMode.PROPOSE:
        lines.append(
            "Only the channel's approvers can approve."
            if policy.approval.approvers_enabled
            else "Anyone in the channel can approve."
        )
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


# /jotflow repos (#91): the channel's repos, read-only


REPOS_CHANGE = (
    "JotFlow's repos live in jotflow.toml, which a running JotFlow can't change. On the "
    "machine that runs JotFlow, run `uvx jotflow init` from its folder, answer n to keeping "
    "jotflow.toml, and add or remove the repo there. Then restart with `docker compose up -d`. "
    "Also give the agents' GitHub tokens access to a repo you add, on GitHub: setup can't."
)


def channel_repos(policy: Policy, channel_id: str, argument: str) -> str:
    """``/jotflow repos``: list this channel's repos, or say how to change them."""
    if channel_id not in policy.channels:
        return NOT_SET_UP
    if argument.strip():
        return REPOS_CHANGE
    lines = ["This channel feeds:"]
    for repo in policy.channels[channel_id].repos:
        lines.append(f"  {repo.nickname}: {repo.repo}. {repo.description}")
    return "\n".join(lines)
