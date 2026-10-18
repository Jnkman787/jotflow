"""Versioned schema migrations for the SQLite store.

Each migration is a version number and a list of statements. The store records
the applied version in ``PRAGMA user_version`` and runs each pending migration in
one transaction, so a failed migration leaves the previous version intact.

Every column carries a ``--`` comment on its own line saying what it holds.
SQLite keeps these comments in ``sqlite_master``, and
``tests/test_store_schema.py`` reads them to check that no column holds a Slack
message body. The rule is in AGENTS.md: JotFlow stores pointers and derived
state, never what people wrote.

Statements must not contain ``;``. Add a new migration with the next version
number; never edit one that has shipped.

A column added with ``ALTER TABLE ... ADD COLUMN`` takes a ``/* ... */``
comment, not ``--``: SQLite copies the column text into the stored CREATE
TABLE, and a ``--`` comment there would swallow the closing parenthesis.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


_TOPICS = """
CREATE TABLE topics (
    id TEXT PRIMARY KEY, -- random topic id assigned by JotFlow
    channel_id TEXT NOT NULL, -- Slack channel id
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 80), -- short cleaned title
    repo TEXT, -- owner/name of the linked GitHub repo, once there is an issue
    issue_number INTEGER CHECK (issue_number > 0), -- linked GitHub issue number
    created_at TEXT NOT NULL, -- UTC, ISO 8601
    last_activity_at TEXT NOT NULL, -- UTC, ISO 8601, refreshed when a pointer is linked
    CHECK ((repo IS NULL) = (issue_number IS NULL))
)
"""

_MESSAGE_REFS = """
CREATE TABLE message_refs (
    channel_id TEXT NOT NULL, -- Slack channel id
    ts TEXT NOT NULL, -- Slack timestamp, unique within the channel
    thread_ts TEXT, -- timestamp of the thread's parent, if in a thread
    permalink TEXT, -- link into Slack, a pointer only
    topic_id TEXT REFERENCES topics (id) ON DELETE SET NULL, -- its topic, or null
    chatter INTEGER NOT NULL DEFAULT 0 CHECK (chatter IN (0, 1)), -- 1 when not an idea
    linked_by TEXT NOT NULL CHECK (linked_by IN ('agent', 'person')), -- who decided
    linked_at TEXT NOT NULL, -- UTC, ISO 8601
    PRIMARY KEY (channel_id, ts),
    CHECK (NOT (chatter = 1 AND topic_id IS NOT NULL))
)
"""

_STATES = (
    "'pending', 'approved', 'executing', 'reconciling', 'applied', 'rejected', "
    "'superseded', 'failed'"
)

_PROPOSALS = f"""
CREATE TABLE proposals (
    id TEXT PRIMARY KEY, -- random id, also the hidden marker in the issue body
    channel_id TEXT NOT NULL, -- Slack channel id
    thread_ts TEXT, -- Slack thread the proposal came from
    run_id TEXT NOT NULL, -- drafting run that produced it, see #53
    state TEXT NOT NULL CHECK (state IN ({_STATES})), -- see docs/architecture.md
    kind TEXT NOT NULL CHECK (kind IN ('create_issue', 'update_issue')), -- create or update
    repo TEXT NOT NULL, -- owner/name of the target GitHub repo
    topic_id TEXT REFERENCES topics (id) ON DELETE SET NULL, -- its topic, if any
    draft_json TEXT NOT NULL, -- the agent's structured issue draft as JSON
    content_hash TEXT, -- sha256 of the draft's canonical JSON, locked at approval
    approved_by TEXT, -- Slack user id of the approver, or auto
    approved_at TEXT, -- UTC, ISO 8601
    result_json TEXT, -- the agent's execution result as JSON: issue URL or error
    created_at TEXT NOT NULL, -- UTC, ISO 8601
    updated_at TEXT NOT NULL, -- UTC, ISO 8601
    CHECK (state IN ('pending', 'rejected', 'superseded') OR content_hash IS NOT NULL)
)
"""

_PARTICIPANTS = """
CREATE TABLE proposal_participants (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE, -- the proposal
    slack_user_id TEXT NOT NULL, -- a Slack user the proposal was drawn from
    PRIMARY KEY (proposal_id, slack_user_id)
)
"""

_IDENTITY_LINKS = """
CREATE TABLE identity_links (
    slack_user_id TEXT PRIMARY KEY, -- Slack user id
    github_login TEXT NOT NULL UNIQUE COLLATE NOCASE, -- GitHub login they claim
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)), -- 0: self-declared
    linked_at TEXT NOT NULL -- UTC, ISO 8601
)
"""

_CHANNEL_MODES = """
CREATE TABLE channel_modes (
    channel_id TEXT PRIMARY KEY, -- Slack channel id
    mode TEXT NOT NULL CHECK (mode IN ('propose', 'auto', 'paused')), -- the channel's mode
    changed_by TEXT NOT NULL, -- Slack user id of whoever set it
    changed_at TEXT NOT NULL -- UTC, ISO 8601
)
"""

_RUN_KINDS = "'drafting', 'execution', 'question'"
_RUN_STATUSES = "'queued', 'running', 'succeeded', 'failed', 'timed_out', 'interrupted'"

_RUNS = f"""
CREATE TABLE runs (
    id TEXT PRIMARY KEY, -- random run id assigned by JotFlow
    kind TEXT NOT NULL CHECK (kind IN ({_RUN_KINDS})), -- drafting, execution, or question
    status TEXT NOT NULL CHECK (status IN ({_RUN_STATUSES})), -- see docs/architecture.md
    channel_id TEXT NOT NULL, -- Slack channel id
    thread_ts TEXT NOT NULL, -- Slack thread the run is for; a top-level post is its own
    proposal_id TEXT, -- the approved proposal an execution run writes, unenforced like run_id
    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds > 0), -- how long a run may run
    created_at TEXT NOT NULL, -- UTC, ISO 8601
    deadline TEXT NOT NULL, -- UTC, ISO 8601, reset to start plus timeout when claimed
    started_at TEXT, -- UTC, ISO 8601
    finished_at TEXT, -- UTC, ISO 8601
    error TEXT CHECK (length(error) <= 2000), -- failure reason the agent gave, untrusted
    CHECK (kind != 'execution' OR proposal_id IS NOT NULL),
    CHECK (status != 'queued' OR started_at IS NULL),
    CHECK (status NOT IN ('running', 'succeeded', 'timed_out') OR started_at IS NOT NULL),
    CHECK ((status IN ('queued', 'running')) = (finished_at IS NULL))
)
"""

_TOOL_CALLS = """
CREATE TABLE tool_calls (
    run_id TEXT NOT NULL, -- the run that made the call
    seq INTEGER NOT NULL CHECK (seq > 0), -- 1 for the run's first recorded call, and so on
    tool TEXT NOT NULL, -- the write tool's name
    args_hash TEXT NOT NULL, -- sha256 of the tool name and canonical arguments
    channel_id TEXT NOT NULL, -- Slack channel id from the run's grant
    thread_ts TEXT, -- Slack thread from the run's grant
    result_json TEXT NOT NULL, -- the tool's result: ids and states only, see #15
    called_at TEXT NOT NULL, -- UTC, ISO 8601
    PRIMARY KEY (run_id, seq),
    UNIQUE (run_id, tool, args_hash)
)
"""

_PACKET_MESSAGES = """
CREATE TABLE packet_messages (
    run_id TEXT NOT NULL, -- the drafting run whose packet served the pointer
    channel_id TEXT NOT NULL, -- Slack channel id
    ts TEXT NOT NULL, -- Slack timestamp served in the packet
    thread_ts TEXT, -- timestamp of the served pointer's thread parent
    PRIMARY KEY (run_id, channel_id, ts)
)
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        (
            _TOPICS,
            "CREATE INDEX topics_channel_activity ON topics (channel_id, last_activity_at)",
            _MESSAGE_REFS,
            "CREATE INDEX message_refs_topic ON message_refs (topic_id)",
            "CREATE INDEX message_refs_thread ON message_refs (channel_id, thread_ts)",
            _PROPOSALS,
            "CREATE INDEX proposals_thread ON proposals (channel_id, thread_ts, state)",
            "CREATE INDEX proposals_run ON proposals (run_id)",
            _PARTICIPANTS,
            _IDENTITY_LINKS,
            _CHANNEL_MODES,
        ),
    ),
    Migration(
        2,
        (
            "ALTER TABLE proposals ADD COLUMN draft_cleared_at TEXT"
            " /* UTC, ISO 8601: when the drafted title and body were removed (#102) */",
        ),
    ),
    Migration(
        3,
        (
            _RUNS,
            # One running run per thread, of any kind. A top-level post's run
            # carries the post's own ts, so a reply to it shares the thread.
            "CREATE UNIQUE INDEX runs_one_running_per_thread"
            " ON runs (channel_id, thread_ts) WHERE status = 'running'",
            # A burst during a run queues one follow-up, not one per message.
            "CREATE UNIQUE INDEX runs_one_queued_drafting_per_thread"
            " ON runs (channel_id, thread_ts) WHERE status = 'queued' AND kind = 'drafting'",
            "CREATE UNIQUE INDEX runs_one_execution_per_proposal"
            " ON runs (proposal_id) WHERE status IN ('queued', 'running') AND kind = 'execution'",
            "CREATE INDEX runs_status ON runs (status, created_at)",
        ),
    ),
    Migration(
        4,
        (
            _TOOL_CALLS,
            "CREATE INDEX tool_calls_thread ON tool_calls (channel_id, thread_ts, tool, called_at)",
            _PACKET_MESSAGES,
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version
