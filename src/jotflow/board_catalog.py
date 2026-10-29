"""Every Projects board a channel may use: its listed ones, and those its board scopes
find (#309, PR 2 for the finding).

The policy stays the allowlist. A listed board works as in PR 1. A board scope's
boards come from the drafting runner's own GitHub call, which applies the scope
itself, so an excluded board never leaves the runner. A found board is used only
while its listing is fresh, at most :data:`MAX_FOUND` a channel, and only under a
nickname nothing else in the channel uses.

Creators are matched by GitHub account id, never by login, since a login can be
renamed and then claimed by someone else (#305). Each scope's logins are resolved
to ids through the drafting runner, and one that doesn't resolve to a person's
account drops out: it fails closed.

Every place that reads a channel's boards goes through :class:`BoardCatalog`, so
a channel without board scopes behaves exactly as in PR 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jotflow.models import FoundBoard
from jotflow.policy import BoardScopePolicy, ChannelPolicy, Policy, UnknownChannelError
from jotflow.repo_catalog import REFRESH_EVERY, STALE_AFTER, clean_description
from jotflow.store.titles import clean_title

log = logging.getLogger("jotflow.boards")

# At most this many found boards a channel, after its listed ones: they reach every
# drafting packet and the card's menu, which Slack caps at 100 options.
MAX_FOUND = 20
TITLE_LIMIT = 100

Finder = Callable[
    [str, Sequence[int], "str | None", Sequence[int], Sequence[str]], Awaitable[list[FoundBoard]]
]
# A login's GitHub account id, or None when it isn't a person's account.
Resolver = Callable[[str], Awaitable["int | None"]]
# Whether the write token may update a board. Raises when it couldn't be asked.
AccessCheck = Callable[[str, int], Awaitable[bool]]


@dataclass(frozen=True)
class BoardEntry:
    """A board a channel may use, listed or found."""

    board: str  # owner/number
    nickname: str
    description: str  # the team's own words: a listed board's, or its scope's; may be ""
    found: bool = False  # found by a board scope, not listed in jotflow.toml
    title: str | None = None  # a found board's GitHub title, cleaned; untrusted
    github_description: str | None = None  # a found board's, cleaned; untrusted

    @property
    def owner(self) -> str:
        return self.board.partition("/")[0]

    @property
    def number(self) -> int:
        return int(self.board.partition("/")[2])


@dataclass
class _Listing:
    fetched_at: datetime | None = None
    boards: list[FoundBoard] = field(default_factory=list)


def _clean_title(board: FoundBoard) -> str:
    try:
        return clean_title(board.title, TITLE_LIMIT)
    except ValueError:
        return f"#{board.number}"


class BoardCatalog:
    """The channels' boards, listed and found, and the allowlist over them."""

    def __init__(
        self,
        policy: Policy,
        *,
        finder: Finder | None = None,
        resolve: Resolver | None = None,
        access: AccessCheck | None = None,
        off: Collection[str] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._policy = policy
        self._finder = finder
        self._resolve = resolve
        self._access = access
        self._clock = clock
        # Listed boards the write token can't update, from JotFlow's start-up check.
        self.off: set[str] = set(off)
        self._listings: dict[tuple[str, int], _Listing] = {
            (channel_id, i): _Listing()
            for channel_id, channel in policy.channels.items()
            for i, _ in enumerate(channel.board_scopes)
        }
        # login (casefolded) -> account id, for the logins that resolved to a person.
        # Each pinned creator id, with the id its login resolved to when checked (#348).
        self._checked: dict[int, int | None] = {}
        self._mismatched: set[int] = set()
        # The write token's answer for a found board, kept once it is a real one.
        # It only decides what is offered: every add checks again, live (decision 5).
        self._writable: dict[str, bool] = {}
        self._noted: set[tuple[str, str]] = set()

    @classmethod
    def static(cls, policy: Policy, off: Collection[str] = ()) -> BoardCatalog:
        """The policy's own boards, with no finding: PR 1's behaviour."""
        return cls(policy, off=off)

    @property
    def scoped(self) -> bool:
        return bool(self._listings)

    # Finding

    async def _creator_ids(self, scope: BoardScopePolicy) -> list[int]:
        """The scope's pinned creator ids, each matched as jotflow.toml gives it (#348).

        The id is the authority, never the login. Each pair is checked once: if the
        login now belongs to a different account, someone else may hold that name,
        so the creator is dropped with a warning until an admin fixes the line. A
        login that no longer resolves, or a lookup that fails, keeps the pinned id,
        which still names the same person after a rename.
        """
        ids: list[int] = []
        for creator in scope.creators:
            if creator.id not in self._checked and self._resolve is not None:
                try:
                    now = await self._resolve(creator.login)
                except Exception as exc:
                    log.warning(
                        "board creator %s couldn't be checked (%s); its pinned id still counts",
                        creator.login,
                        type(exc).__name__,
                    )
                    now = None
                else:
                    self._checked[creator.id] = now
            now = self._checked.get(creator.id)
            if now is not None and now != creator.id:
                if creator.id not in self._mismatched:
                    self._mismatched.add(creator.id)
                    log.warning(
                        "board creator %s is pinned to id %d, but that login is now id %d; "
                        "it isn't counted until jotflow.toml is fixed",
                        creator.login,
                        creator.id,
                        now,
                    )
                continue
            ids.append(creator.id)
        return ids

    async def refresh(self, channel_id: str, index: int) -> bool:
        """Find one board scope's boards again. False when the runner couldn't be asked."""
        listing = self._listings.get((channel_id, index))
        if listing is None or self._finder is None:
            return False
        scope = self._policy.channel(channel_id).board_scopes[index]
        creators = await self._creator_ids(scope) if scope.marker is not None else []
        try:
            boards = await self._finder(
                scope.owner, scope.numbers, scope.marker, creators, scope.exclude
            )
        except Exception as exc:  # down or refused: keep the last listing until it's stale
            log.warning("could not find %s's boards: %s", scope.owner, type(exc).__name__)
            return False
        self.load(channel_id, index, boards)
        await self._check_access(scope.owner, boards)
        log.info("found %d boards in scope for %s", len(boards), scope.owner)
        return True

    def load(self, channel_id: str, index: int, boards: Iterable[FoundBoard]) -> None:
        """Take ``boards`` as the scope's listing now, as a refresh would. For tests."""
        listing = self._listings[(channel_id, index)]
        listing.boards = list(boards)
        listing.fetched_at = self._clock()

    async def _check_access(self, owner: str, boards: Iterable[FoundBoard]) -> None:
        """Ask once, for a board first found, whether the write token may update it.

        Only a real no leaves it out, as at start-up (#323). A check that couldn't
        run keeps it, and is asked again at the next refresh. Either way the add
        checks for itself (decision 5): this only decides what is offered.
        """
        if self._access is None:
            return
        for board in list(boards)[:MAX_FOUND]:
            key = f"{owner}/{board.number}".casefold()
            if key in self._writable:
                continue
            try:
                self._writable[key] = await self._access(owner, board.number)
            except Exception as exc:
                log.warning(
                    "board %s/%d couldn't be checked (%s); it stays offered",
                    owner,
                    board.number,
                    type(exc).__name__,
                )
                continue
            if not self._writable[key]:
                log.warning(
                    "found board %s/%d is left out: the write token can't see or update it",
                    owner,
                    board.number,
                )

    async def refresh_all(self) -> None:
        for channel_id, index in sorted(self._listings):
            await self.refresh(channel_id, index)

    async def run(self, stop: asyncio.Event) -> None:
        """Find every scope's boards now, then every REFRESH_EVERY, until ``stop``."""
        while not stop.is_set():
            await self.refresh_all()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), REFRESH_EVERY.total_seconds())

    # The channel's boards

    def _fresh(self, channel_id: str, index: int) -> list[FoundBoard]:
        listing = self._listings.get((channel_id, index))
        if listing is None or listing.fetched_at is None:
            return []
        if self._clock() - listing.fetched_at > STALE_AFTER:
            return []  # too old to trust: fail closed
        return listing.boards

    def _note(self, channel_id: str, what: str, message: str, *args: object) -> None:
        if (channel_id, what) not in self._noted:
            self._noted.add((channel_id, what))
            log.warning(message, *args)

    def _found(self, channel_id: str, channel: ChannelPolicy) -> list[BoardEntry]:
        listed = {b.board.casefold() for b in channel.boards}
        taken = {r.nickname for r in channel.repos} | {b.nickname for b in channel.boards}
        for repo_scope in channel.scopes:
            taken |= set(repo_scope.aliases)
        for board_scope in channel.board_scopes:
            taken |= set(board_scope.aliases)
        out: list[BoardEntry] = []
        dropped = 0
        for index, scope in enumerate(channel.board_scopes):
            names = {number: alias for alias, number in scope.aliases.items()}
            for board in sorted(self._fresh(channel_id, index), key=lambda b: b.number):
                ref = f"{scope.owner}/{board.number}"
                if ref.casefold() in listed or any(
                    e.board.casefold() == ref.casefold() for e in out
                ):
                    continue  # a listed board wins, with the team's description
                nickname = names.get(board.number) or f"b{board.number}"
                if nickname not in names.values() and nickname in taken:
                    self._note(
                        channel_id,
                        f"clash:{ref}",
                        "found board %s is left out: its nickname %s is taken",
                        ref,
                        nickname,
                    )
                    continue
                if len(out) >= MAX_FOUND:
                    dropped += 1
                    continue
                taken.add(nickname)
                out.append(
                    BoardEntry(
                        board=ref,
                        nickname=nickname,
                        description=scope.description or "",
                        found=True,
                        title=_clean_title(board),
                        github_description=clean_description(board.description),
                    )
                )
        if dropped:
            self._note(
                channel_id,
                f"cap:{dropped}",
                "channel %s: %d found boards past the cap of %d are left out",
                channel_id,
                dropped,
                MAX_FOUND,
            )
        return out

    def boards(self, channel_id: str) -> list[BoardEntry]:
        """Every board the channel lists or finds now, usable or not: listed first."""
        try:
            channel = self._policy.channel(channel_id)
        except UnknownChannelError:
            return []
        listed = [BoardEntry(b.board, b.nickname, b.description) for b in channel.boards]
        return listed + self._found(channel_id, channel)

    def usable(self, entry: BoardEntry) -> bool:
        """Whether the write token may update it, as far as JotFlow knows."""
        if entry.board in self.off:
            return False
        return self._writable.get(entry.board.casefold(), True)

    def offered(self, channel_id: str) -> list[BoardEntry]:
        """The boards drafting may propose and the card's menu offers."""
        return [entry for entry in self.boards(channel_id) if self.usable(entry)]

    def board(self, channel_id: str, ref: str) -> BoardEntry | None:
        """A board the channel lists or finds now, by nickname or ``owner/number``."""
        wanted = ref.casefold()
        for entry in self.boards(channel_id):
            if wanted in (entry.board.casefold(), entry.nickname):
                return entry
        return None
