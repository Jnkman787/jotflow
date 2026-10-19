"""Admin-issued, read-only operator tokens, checked at the MCP gate (#58, D12).

An operator token lets a person's terminal agent call ``list_topics`` and
``get_topic`` on the channels an admin named, and nothing else. Admins create,
list and revoke them with ``jotflow operator``, which writes the store. The
running server holds an in-memory copy, so the gate's check does no I/O:

- the copy is reloaded every :data:`RELOAD_SECONDS`, so a revocation takes
  effect within that interval;
- a token the copy doesn't know triggers one reload, so a token created a
  moment ago works at once. Those reloads are at most one per
  :data:`MISS_RELOAD_SECONDS`, so a flood of bad tokens can't force reloads.

Only SHA-256 digests are held, and a token is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Callable
from datetime import datetime

from jotflow.models import RunKind
from jotflow.store.base import Clock, OperatorToken, Store, utc_now
from jotflow.whiteboard.tokens import MAX_TOKEN_LENGTH, TOKEN_PREFIX, Grant, TokenCheck

log = logging.getLogger("jotflow.operator")

OPERATOR_PREFIX = "jfo_"
RELOAD_SECONDS = 2.0
MISS_RELOAD_SECONDS = 1.0


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def grant_for(token: OperatorToken) -> Grant:
    return Grant(
        run_id=f"operator:{token.id}",
        kind=RunKind.OPERATOR,
        channel_id=token.channels[0],
        channels=frozenset(token.channels),
    )


class OperatorTokens:
    """The operator tokens the gate accepts: an in-memory copy of the store's."""

    def __init__(
        self,
        store: Store,
        *,
        clock: Clock = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        miss_seconds: float = MISS_RELOAD_SECONDS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._monotonic = monotonic
        self._miss_seconds = miss_seconds
        self._by_digest: dict[str, OperatorToken] = {}
        self._used: dict[str, datetime] = {}
        self._last_miss: float | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def check(self, token: str) -> Grant | None:
        if not token.startswith(OPERATOR_PREFIX) or len(token) > MAX_TOKEN_LENGTH:
            return None
        entry = self._by_digest.get(digest(token))
        now = self._clock()
        if entry is None or not entry.usable(now):
            return None
        self._used[entry.id] = now
        return grant_for(entry)

    async def check_on_miss(self, token: str) -> Grant | None:
        """For a token the copy doesn't know: reload once, rate-limited, and check again."""
        if self._closed or not token.startswith(OPERATOR_PREFIX):
            return None
        now = self._monotonic()
        if self._last_miss is not None and now - self._last_miss < self._miss_seconds:
            return None
        self._last_miss = now
        await self.reload()
        return self.check(token)

    async def reload(self) -> None:
        """Take the store's current tokens, and save when each was last used."""
        async with self._lock:
            await self._flush()
            now = self._clock()
            tokens = await self._store.operator_tokens()
            if not self._closed:
                self._by_digest = {t.digest: t for t in tokens if t.usable(now)}

    async def flush(self) -> None:
        """Save when each token was last used. Shutdown calls it before closing the store."""
        async with self._lock:
            await self._flush()

    async def _flush(self) -> None:
        used, self._used = self._used, {}
        if not used:
            return
        try:
            await self._store.touch_operator_tokens(used)
        except BaseException:
            # Keep them for the next try; a newer use wins.
            self._used = {**used, **self._used}
            raise

    def forget(self) -> None:
        """Accept no operator token from now on, for shutdown. Final: no reload refills it."""
        self._closed = True
        self._by_digest = {}

    async def run(self, stop: asyncio.Event, *, interval: float = RELOAD_SECONDS) -> None:
        """Reload every ``interval`` seconds until ``stop`` is set, then save last-used."""
        while not stop.is_set():
            try:
                await self.reload()
            except Exception:
                log.exception("could not reload operator tokens")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        await self.flush()


class GateTokens:
    """Run tokens and operator tokens, at one gate. Each prefix goes to its own check."""

    def __init__(self, runs: TokenCheck, operators: OperatorTokens) -> None:
        self._runs = runs
        self._operators = operators

    def check(self, token: str) -> Grant | None:
        if token.startswith(TOKEN_PREFIX):
            return self._runs.check(token)
        return self._operators.check(token)

    async def check_on_miss(self, token: str) -> Grant | None:
        return await self._operators.check_on_miss(token)
