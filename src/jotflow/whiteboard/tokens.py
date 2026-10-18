"""Who may call the MCP server: the bearer-token interface.

Every request must carry ``Authorization: Bearer <token>``. The gate hands the
token to a :class:`TokenCheck`, which returns the :class:`Grant` it stands for
or ``None``. Per-run tokens, their scoping by run kind and channel, and expiry
are #16. Until then the server uses :class:`RejectAllTokens`, so nothing is
reachable without a token JotFlow issued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jotflow.models import RunKind

# Longer than any token JotFlow mints; anything bigger is refused unread.
MAX_TOKEN_LENGTH = 512


@dataclass(frozen=True)
class Grant:
    """What a valid token allows: one run, of one kind, in one channel."""

    run_id: str
    kind: RunKind
    channel_id: str


class TokenCheck(Protocol):
    def check(self, token: str) -> Grant | None:
        """The grant ``token`` stands for, or ``None`` to refuse it.

        Implementations compare in constant time and never log the token.
        """
        ...


class RejectAllTokens:
    """The default until #16: refuses every token."""

    def check(self, token: str) -> Grant | None:
        return None
