"""Every repo a channel may feed: the listed ones, and those its scopes find (#236).

The policy stays the write allowlist. A listed repo works as it always has. A
scope's repos come from the drafting runner's own GitHub call, cached here per
owner, and one is allowed only when it is under the scope's owner, matches its
globs, isn't excluded, is in a fresh listing, and is neither archived nor a
fork unless the scope says so. Anything else is refused with a fixed reason.

Every place that reads a channel's repos goes through :class:`RepoCatalog`, so
a channel without scopes behaves exactly as before: :meth:`RepoCatalog.static`
is the policy's own list.

Discovered repos are :class:`RepoPolicy` values too, built from the scope, so
callers need no second shape. What only GitHub knows, the cleaned description,
topics, and last push, is in :meth:`RepoCatalog.details`, and is untrusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jotflow.models import DiscoveredRepo
from jotflow.policy import (
    ChannelPolicy,
    Policy,
    PolicyViolation,
    RepoNotAllowedError,
    RepoPolicy,
    ScopePolicy,
)
from jotflow.store.titles import clean_title

log = logging.getLogger("jotflow.repos")

REFRESH_EVERY = timedelta(minutes=15)
# A listing older than this, with the runner unreachable, counts as none: fail closed.
STALE_AFTER = timedelta(hours=24)
# A miss triggers at most one refresh a minute per owner.
MISS_REFRESH_SECONDS = 60.0
# An old name keeps resolving to the renamed repo for this long.
RENAME_GRACE = timedelta(days=7)
# A positive write-access answer is kept this long; a negative one never is.
ACCESS_FOR = timedelta(minutes=15)
DESCRIPTION_LIMIT = 200
# How many found repos a drafting packet shows, beside the listed ones.
PACKET_DISCOVERED = 30
_WORD = re.compile(r"[a-z0-9]{3,}")
MAX_TOPICS = 10
_TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
_NICKNAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

NOT_LISTED_YET = (
    "repo {repo!r} isn't in channel {channel}'s repo list yet; JotFlow is refreshing it, so "
    "try again shortly"
)
ARCHIVED = "repo {repo!r} is archived, so channel {channel}'s scope leaves it out"
A_FORK = "repo {repo!r} is a fork, so channel {channel}'s scope leaves it out"


class RepoRefused(PolicyViolation):
    """A repo the channel's scope covers by name, but that can't be used now."""


Lister = Callable[[str], Awaitable[list[DiscoveredRepo]]]
AccessCheck = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class RepoDetails:
    """What GitHub says about a discovered repo, cleaned. All of it untrusted."""

    description: str | None = None
    topics: tuple[str, ...] = ()
    pushed_at: str | None = None
    discovered: bool = False


@dataclass
class _Owner:
    fetched_at: datetime | None = None
    by_id: dict[int, DiscoveredRepo] = field(default_factory=dict)
    renamed: dict[str, tuple[int, datetime]] = field(default_factory=dict)
    last_miss_refresh: float = float("-inf")


def clean_description(text: str | None) -> str | None:
    """A GitHub description as the packet and Slack may show it, or None."""
    if not text:
        return None
    try:
        return clean_title(text, DESCRIPTION_LIMIT)
    except ValueError:
        return None


def clean_topics(topics: Iterable[str]) -> tuple[str, ...]:
    return tuple(t for t in topics if _TOPIC.fullmatch(t))[:MAX_TOPICS]


def default_nickname(name: str) -> str | None:
    """A repo name as a nickname: lower case, "." as "-", other characters dropped."""
    nick = re.sub(r"[^a-z0-9_-]", "", name.lower().replace(".", "-"))[:32]
    return nick if _NICKNAME.fullmatch(nick) else None


class RepoCatalog:
    """The channels' repos, listed and discovered, and the write allowlist over them."""

    def __init__(
        self,
        policy: Policy,
        *,
        lister: Lister | None = None,
        access: AccessCheck | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._lister = lister
        self._access = access
        self._clock = clock
        self._mono = mono
        self._owners: dict[str, _Owner] = {
            scope.owner.casefold(): _Owner()
            for channel in policy.channels.values()
            for scope in channel.scopes
        }
        self._writable: dict[str, datetime] = {}
        self._tasks: set[asyncio.Task[bool]] = set()

    @classmethod
    def static(cls, policy: Policy) -> RepoCatalog:
        """The policy's own lists, with no discovery: today's behaviour."""
        return cls(policy)

    # Discovery

    @property
    def owners(self) -> list[str]:
        return sorted(self._owners)

    async def refresh(self, owner: str) -> bool:
        """Fetch ``owner``'s repos again. False when the runner could not be asked."""
        state = self._owners.get(owner.casefold())
        if state is None or self._lister is None:
            return False
        try:
            repos = await self._lister(owner)
        except Exception as exc:  # the runner is down or refused: keep the last listing
            log.warning("could not list %s's repos: %s", owner, type(exc).__name__)
            return False
        now = self._clock()
        fresh = {r.id: r for r in repos}
        for repo_id, old in state.by_id.items():
            new = fresh.get(repo_id)
            if new is not None and new.full_name.casefold() != old.full_name.casefold():
                state.renamed[old.full_name.casefold()] = (repo_id, now + RENAME_GRACE)
        state.renamed = {k: v for k, v in state.renamed.items() if v[1] > now}
        state.by_id = fresh
        state.fetched_at = now
        log.info("listed %d repos for %s", len(fresh), owner)
        return True

    async def refresh_all(self) -> None:
        for owner in self.owners:
            await self.refresh(owner)

    async def run(self, stop: asyncio.Event) -> None:
        """Refresh every owner now, then every REFRESH_EVERY, until ``stop`` is set."""
        while not stop.is_set():
            await self.refresh_all()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), REFRESH_EVERY.total_seconds())

    def _refresh_soon(self, owner: str) -> None:
        state = self._owners.get(owner.casefold())
        if state is None or self._lister is None:
            return
        now = self._mono()
        if now - state.last_miss_refresh < MISS_REFRESH_SECONDS:
            return
        state.last_miss_refresh = now
        try:
            task = asyncio.get_running_loop().create_task(self.refresh(owner))
        except RuntimeError:
            return  # no event loop, as in some tests: the next scheduled refresh will do
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fresh(self, owner: str) -> _Owner | None:
        state = self._owners.get(owner.casefold())
        if state is None or state.fetched_at is None:
            return None
        if self._clock() - state.fetched_at > STALE_AFTER:
            return None
        return state

    # The channel's repos

    def _channel(self, channel_id: str) -> ChannelPolicy:
        return self._policy.channel(channel_id)

    def _in_scope(self, scope: ScopePolicy, repo: DiscoveredRepo) -> bool:
        owner, _, name = repo.full_name.partition("/")
        if owner.casefold() != scope.owner.casefold() or not scope.covers(name):
            return False
        if repo.archived and not scope.include_archived:
            return False
        return scope.include_forks or not repo.fork

    def _discovered(self, channel_id: str) -> list[tuple[RepoPolicy, RepoDetails]]:
        """The channel's discovered repos, with nicknames, newest push first."""
        channel = self._channel(channel_id)
        listed = {r.repo.casefold() for r in channel.repos}
        taken = {r.nickname for r in channel.repos}
        for scope in channel.scopes:
            taken |= set(scope.aliases)
        found: list[tuple[ScopePolicy, DiscoveredRepo]] = []
        for scope in channel.scopes:
            state = self._fresh(scope.owner)
            if state is None:
                continue
            for repo in state.by_id.values():
                if repo.full_name.casefold() not in listed and self._in_scope(scope, repo):
                    found.append((scope, repo))
        found.sort(key=lambda pair: (pair[1].pushed_at or "", pair[1].full_name), reverse=True)
        # Nicknames: aliases first, then defaults, in jotflow.toml's scope order (#236).
        order = {scope.owner.casefold(): i for i, scope in enumerate(channel.scopes)}
        by_scope = sorted(found, key=lambda pair: (order[pair[0].owner.casefold()], pair[1].id))
        nicknames: dict[int, str] = {}
        for scope, repo in by_scope:
            name = repo.full_name.partition("/")[2]
            for alias, target in scope.aliases.items():
                if target.casefold() == name.casefold():
                    nicknames[repo.id] = alias
        for _scope, repo in by_scope:
            if repo.id in nicknames:
                continue
            owner, _, name = repo.full_name.partition("/")
            for candidate in (default_nickname(name), default_nickname(f"{owner}-{name}")):
                if candidate is not None and candidate not in taken:
                    nicknames[repo.id] = candidate
                    taken.add(candidate)
                    break
        out: list[tuple[RepoPolicy, RepoDetails]] = []
        for scope, repo in found:
            visibility = "private" if repo.private else "public"
            entry = RepoPolicy.model_construct(
                repo=repo.full_name,
                nickname=nicknames.get(repo.id, ""),
                description=scope.description or "",
                labels=scope.labels,
                visibility=visibility,
                conventions=None,
                title_pattern=None,
            )
            details = RepoDetails(
                description=clean_description(repo.description),
                topics=clean_topics(repo.topics),
                pushed_at=repo.pushed_at,
                discovered=True,
            )
            out.append((entry, details))
        return out

    def repos(self, channel_id: str) -> tuple[RepoPolicy, ...]:
        """Every repo the channel may feed: listed ones first, then discovered by last push."""
        channel = self._channel(channel_id)
        return (*channel.repos, *(entry for entry, _ in self._discovered(channel_id)))

    def details(self, channel_id: str, repo: str) -> RepoDetails:
        """GitHub's own description and topics for a discovered repo, else nothing."""
        for entry, details in self._discovered(channel_id):
            if entry.repo.casefold() == repo.casefold():
                return details
        return RepoDetails()

    def listed(
        self, channel_id: str, text: str, limit: int = PACKET_DISCOVERED
    ) -> list[tuple[RepoPolicy, RepoDetails]]:
        """What a drafting packet shows: every listed repo, then up to ``limit`` found ones.

        Found repos are ranked by words they share with ``text``, the thread, then by
        last push. This only decides what is shown; any repo in scope may be proposed.
        """
        channel = self._channel(channel_id)
        words = set(_WORD.findall(text.casefold()))

        def overlap(pair: tuple[RepoPolicy, RepoDetails]) -> int:
            entry, details = pair
            theirs = set(_WORD.findall(entry.repo.partition("/")[2].casefold()))
            theirs |= set(_WORD.findall((details.description or "").casefold()))
            theirs |= set(details.topics)
            return len(words & theirs)

        found = self._discovered(channel_id)  # newest push first
        ranked = sorted(found, key=overlap, reverse=True)  # stable: ties keep push order
        return [(entry, RepoDetails()) for entry in channel.repos] + ranked[:limit]

    def scope_total(self, channel_id: str) -> int:
        """How many repos the channel's scopes reach right now."""
        return len(self._discovered(channel_id))

    def repo(self, channel_id: str, ref: str) -> RepoPolicy:
        """The repo ``ref`` names, by ``owner/name`` or nickname, if the channel may use it.

        Raises :class:`RepoNotAllowedError` outside the channel's repos and scopes,
        and :class:`RepoRefused` for a repo a scope names but can't use now.
        """
        channel = self._channel(channel_id)
        wanted = ref.casefold()
        for entry in channel.repos:
            if wanted in (entry.repo.casefold(), entry.nickname):
                return entry
        discovered = self._discovered(channel_id)
        for entry, _ in discovered:
            if wanted == entry.repo.casefold() or (entry.nickname and wanted == entry.nickname):
                return entry
        owner, slash, name = ref.partition("/")
        for scope in channel.scopes:
            state = self._owners.get(scope.owner.casefold())
            if slash and state is not None and wanted in state.renamed:
                repo_id, _ = state.renamed[wanted]
                for entry, _ in discovered:
                    current = state.by_id.get(repo_id)
                    if current is not None and entry.repo == current.full_name:
                        return entry
            if not slash or owner.casefold() != scope.owner.casefold() or not scope.covers(name):
                continue
            fresh = self._fresh(scope.owner)
            known = (
                next((r for r in fresh.by_id.values() if r.full_name.casefold() == wanted), None)
                if fresh is not None
                else None
            )
            if known is not None and known.archived and not scope.include_archived:
                raise RepoRefused(ARCHIVED.format(repo=ref, channel=channel_id))
            if known is not None and known.fork and not scope.include_forks:
                raise RepoRefused(A_FORK.format(repo=ref, channel=channel_id))
            self._refresh_soon(scope.owner)
            raise RepoRefused(NOT_LISTED_YET.format(repo=ref, channel=channel_id))
        raise RepoNotAllowedError(channel_id, ref)

    def is_discovered(self, channel_id: str, repo: str) -> bool:
        listed = {r.repo.casefold() for r in self._channel(channel_id).repos}
        return repo.casefold() not in listed

    # The write token's reach

    async def writable(self, channel_id: str, repo: str) -> bool:
        """Whether the write token can see ``repo``. Listed repos aren't checked.

        Raises whatever the check raises when the execution runner can't be
        asked, so the caller fails closed. Only a positive answer is cached.
        """
        if not self.is_discovered(channel_id, repo):
            return True
        if self._access is None:
            return False
        now = self._clock()
        key = repo.casefold()
        if (until := self._writable.get(key)) is not None and until > now:
            return True
        owner, _, name = repo.partition("/")
        ok = await self._access(owner, name)
        if ok:
            self._writable[key] = now + ACCESS_FOR
        else:
            self._writable.pop(key, None)
        return ok

    def summaries(self, channel_id: str) -> list[str]:
        """One line per scope, for help and /jotflow repos."""
        lines = []
        for scope in self._channel(channel_id).scopes:
            state = self._owners.get(scope.owner.casefold())
            when = state.fetched_at if state is not None else None
            age = "not listed yet" if when is None else f"listed {_ago(self._clock() - when)}"
            lines.append(f"Also {scope.summary()} ({age}).")
        return lines


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"
