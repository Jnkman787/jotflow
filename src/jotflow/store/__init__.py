"""JotFlow's local state: pointers and derived state, never message bodies.

Use :class:`SqliteStore` in the service and :class:`MemoryStore` in tests; both
implement :class:`Store`. See docs/plans/0011-sqlite-store.md.
"""

from jotflow.store.base import (
    ALLOWED_TRANSITIONS,
    AUTO_APPROVER,
    ChannelModeSetting,
    ConflictError,
    ContentChangedError,
    IdentityLink,
    MessageLink,
    NotFoundError,
    Store,
    StoreError,
    TopicRecord,
    TransitionError,
)
from jotflow.store.hashing import content_hash
from jotflow.store.memory import MemoryStore
from jotflow.store.sqlite import SchemaVersionError, SqliteStore
from jotflow.store.titles import MAX_TITLE_LENGTH, clean_title

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AUTO_APPROVER",
    "MAX_TITLE_LENGTH",
    "ChannelModeSetting",
    "ConflictError",
    "ContentChangedError",
    "IdentityLink",
    "MemoryStore",
    "MessageLink",
    "NotFoundError",
    "SchemaVersionError",
    "SqliteStore",
    "Store",
    "StoreError",
    "TopicRecord",
    "TransitionError",
    "clean_title",
    "content_hash",
]
