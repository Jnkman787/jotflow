"""JotFlow's local state: pointers and derived state, never message bodies.

Use :class:`SqliteStore` in the service and :class:`MemoryStore` in tests; both
implement :class:`Store`. See docs/plans/0011-sqlite-store.md.
"""

from jotflow.store.base import (
    ALLOWED_RUN_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    AUTO_APPROVER,
    CLEARED_TITLE,
    DEFAULT_RUN_TIMEOUTS,
    FINAL_STATES,
    OPERATOR_REMOVER,
    VERIFICATION_REMOVER,
    ChannelModeSetting,
    ConflictError,
    ContentChangedError,
    DraftClearedError,
    IdentityCode,
    IdentityLink,
    IdentityUnlink,
    MessageLink,
    NotFoundError,
    OperatorToken,
    Recovery,
    Store,
    StoreError,
    ToolCallRecord,
    TopicRecord,
    TransitionError,
    Verified,
    try_transition,
)
from jotflow.store.hashing import content_hash
from jotflow.store.memory import MemoryStore
from jotflow.store.sqlite import SchemaVersionError, SqliteStore
from jotflow.store.titles import MAX_TITLE_LENGTH, clean_title

__all__ = [
    "ALLOWED_RUN_TRANSITIONS",
    "ALLOWED_TRANSITIONS",
    "AUTO_APPROVER",
    "CLEARED_TITLE",
    "DEFAULT_RUN_TIMEOUTS",
    "FINAL_STATES",
    "MAX_TITLE_LENGTH",
    "OPERATOR_REMOVER",
    "VERIFICATION_REMOVER",
    "ChannelModeSetting",
    "ConflictError",
    "ContentChangedError",
    "DraftClearedError",
    "IdentityCode",
    "IdentityLink",
    "IdentityUnlink",
    "MemoryStore",
    "MessageLink",
    "NotFoundError",
    "OperatorToken",
    "Recovery",
    "SchemaVersionError",
    "SqliteStore",
    "Store",
    "StoreError",
    "ToolCallRecord",
    "TopicRecord",
    "TransitionError",
    "Verified",
    "clean_title",
    "content_hash",
    "try_transition",
]
