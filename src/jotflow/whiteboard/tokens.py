"""Who may call the MCP server: per-run bearer tokens (#16, D13).

Every request carries ``Authorization: Bearer <token>``. The gate hands the
token to a :class:`TokenCheck`, which returns the :class:`Grant` it stands for
or ``None``. :class:`TokenRegistry` is the real check: the run lifecycle (#53)
issues one token per agent run and revokes it when the run ends.

Tokens live only in memory. A restart kills every run, and re-queued runs get
fresh tokens (D8), so nothing is persisted and ``check`` never does I/O. The
registry keeps only SHA-256 digests, so neither a dump of it nor a stray
``repr`` holds a usable token. Looking a token up by its digest is what keeps
``check`` from leaking timing about stored tokens.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from jotflow.models import RunKind

# Longer than any token JotFlow mints; anything bigger is refused unread.
MAX_TOKEN_LENGTH = 512
# A recognisable prefix lets secret scanners spot a leaked token.
TOKEN_PREFIX = "jfr_"
# The longest a token lives. A run must not outlive its token, so run timeouts
# are capped to match, and the runner issues with ttl = deadline - now + grace.
MAX_TOKEN_TTL_SECONDS = 2 * 60 * 60
MAX_LIVE_TOKENS = 1024

Clock = Callable[[], float]


@dataclass(frozen=True)
class Grant:
    """What a valid token allows: one run, of one kind, in one channel.

    Drafting and question runs are bound to the one thread they work on. A
    run for a top-level post uses the post's own ts, as Slack does for a
    thread's parent, so ``thread_ts`` is never ``None`` for them and tools
    compare it exactly. An execution run is bound to the one approved
    proposal it executes and needs no thread.
    """

    run_id: str
    kind: RunKind
    channel_id: str
    thread_ts: str | None = None
    proposal_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.channel_id:
            raise ValueError("a grant needs a run id and a channel id")
        if self.kind is RunKind.EXECUTION and not self.proposal_id:
            raise ValueError("an execution grant must name its proposal")
        if self.kind is not RunKind.EXECUTION and self.proposal_id is not None:
            raise ValueError("only an execution grant names a proposal")
        if self.kind in (RunKind.DRAFTING, RunKind.QUESTION) and not self.thread_ts:
            raise ValueError(f"a {self.kind.value} grant must name its thread")


class TokenCheck(Protocol):
    def check(self, token: str) -> Grant | None:
        """The grant ``token`` stands for, or ``None`` to refuse it.

        Implementations must not leak timing about stored tokens, never log
        the token, and do no I/O, so a slow store cannot slow the gate.
        """
        ...


class RejectAllTokens:
    """Refuses every token. For a server with no runs at all."""

    def check(self, token: str) -> Grant | None:
        return None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    digest: str
    grant: Grant
    expires_at: float


class RegistryFullError(Exception):
    """Too many live tokens. A programming error, as the run cap is far lower."""


@dataclass
class TokenRegistry:
    """Issues, checks, and revokes per-run tokens. One live token per run."""

    clock: Clock = time.monotonic
    max_ttl: float = MAX_TOKEN_TTL_SECONDS
    max_live: int = MAX_LIVE_TOKENS
    _by_digest: dict[str, _Entry] = field(default_factory=dict, init=False, repr=False)
    _by_run: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __repr__(self) -> str:
        return f"TokenRegistry(live={self.active()})"

    def issue(self, grant: Grant, ttl: float) -> str:
        """A new token for ``grant``'s run, valid for ``ttl`` seconds.

        Issuing again for the same run revokes the run's previous token.
        """
        if not 0 < ttl <= self.max_ttl:
            raise ValueError(f"ttl must be more than 0 and at most {self.max_ttl:g} seconds")
        self.revoke(grant.run_id)
        self._purge()
        if len(self._by_digest) >= self.max_live:
            raise RegistryFullError(f"more than {self.max_live} live run tokens")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        digest = _digest(token)
        self._by_digest[digest] = _Entry(digest, grant, self.clock() + ttl)
        self._by_run[grant.run_id] = digest
        return token

    def check(self, token: str) -> Grant | None:
        if not token.startswith(TOKEN_PREFIX) or len(token) > MAX_TOKEN_LENGTH:
            return None
        # The lookup is keyed by the SHA-256 of the presented token, so its
        # timing reveals nothing about any stored token; no separate
        # constant-time compare is needed.
        entry = self._by_digest.get(_digest(token))
        if entry is None:
            return None
        if self.clock() >= entry.expires_at:
            self._drop(entry)
            return None
        return entry.grant

    def revoke(self, run_id: str) -> bool:
        """Drop ``run_id``'s token. Returns whether it had one."""
        digest = self._by_run.pop(run_id, None)
        if digest is None:
            return False
        self._by_digest.pop(digest, None)
        return True

    def active(self) -> int:
        """How many tokens are live, after dropping expired ones."""
        self._purge()
        return len(self._by_digest)

    def _drop(self, entry: _Entry) -> None:
        # A run has at most one digest: issue() revokes the old one first.
        self._by_digest.pop(entry.digest, None)
        self._by_run.pop(entry.grant.run_id, None)

    def _purge(self) -> None:
        now = self.clock()
        for entry in [e for e in self._by_digest.values() if now >= e.expires_at]:
            self._drop(entry)
