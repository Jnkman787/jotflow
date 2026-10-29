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

# Someone else removed a person's link (#173). Never the login, never any text.
_IDENTITY_UNLINKS = """
CREATE TABLE identity_unlinks (
    id TEXT PRIMARY KEY, -- random id
    slack_user_id TEXT NOT NULL, -- Slack user id whose link was removed
    removed_by TEXT NOT NULL, -- Slack user id of who removed it, or 'operator' for the host CLI
    removed_at TEXT NOT NULL -- UTC, ISO 8601
)
"""

# A pending one-time code for verifying a GitHub link (#195). Only its digest.
_IDENTITY_CODES = """
CREATE TABLE identity_codes (
    slack_user_id TEXT PRIMARY KEY, -- Slack user id: one pending code per person
    github_login TEXT NOT NULL, -- GitHub login the code is for
    code_digest TEXT NOT NULL, -- SHA-256 of the code, never the code itself
    expires_at TEXT NOT NULL, -- UTC, ISO 8601
    checks INTEGER NOT NULL DEFAULT 0 -- how many times it was checked
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

# Threads waiting out their quiet window, so a restart forgets none (#130).
# Pointers and a time only, never message text.
_PENDING_THREADS = """
CREATE TABLE pending_threads (
    channel_id TEXT NOT NULL, -- Slack channel id
    thread_ts TEXT NOT NULL, -- the thread's parent ts; a top-level post's own ts
    due_at TEXT NOT NULL, -- UTC, ISO 8601: when the quiet window ends
    PRIMARY KEY (channel_id, thread_ts)
)
"""

_HELP_NOTICES = """
CREATE TABLE help_notices (
    channel_id TEXT NOT NULL, -- Slack channel id
    user_id TEXT NOT NULL, -- Slack user id the notice was for; '' for the channel's guide
    kind TEXT NOT NULL CHECK (kind IN ('guide', 'tip', 'nudge')), -- which notice (#80)
    sent_at TEXT NOT NULL, -- UTC, ISO 8601: when it was claimed, just before sending
    PRIMARY KEY (channel_id, user_id, kind)
)
"""

_PROPOSAL_EDITS = """
CREATE TABLE proposal_edits (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE, -- the proposal
    edited_by TEXT NOT NULL, -- Slack user id of who changed it from the card
    field TEXT NOT NULL CHECK (field IN ('repo', 'assign', 'unassign')), -- what, never the value
    edited_at TEXT NOT NULL -- UTC, ISO 8601
)
"""

# proposal_edits as #309 has it: a person may also edit the card's boards.
_PROPOSAL_EDITS_WITH_BOARDS = """
CREATE TABLE proposal_edits_new (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE, -- the proposal
    edited_by TEXT NOT NULL, -- Slack user id of who changed it from the card
    field TEXT NOT NULL
        CHECK (field IN ('repo', 'assign', 'unassign', 'boards')), -- what, never the value
    edited_at TEXT NOT NULL -- UTC, ISO 8601
)
"""

# A drafting run's revision of a pending draft, from new messages (#304): which
# fields, and whose messages, one row per person. Never a value.
_PROPOSAL_REVISIONS = """
CREATE TABLE proposal_revisions (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE, -- the proposal
    revision INTEGER NOT NULL, -- the proposal's revision after this one
    run_id TEXT NOT NULL, -- the drafting run that revised it
    changed TEXT NOT NULL, -- comma-separated fields: title, body, comment, labels, ...
    revised_at TEXT NOT NULL -- UTC, ISO 8601
)
"""

_PROPOSAL_REVISION_AUTHORS = """
CREATE TABLE proposal_revision_authors (
    proposal_id TEXT NOT NULL REFERENCES proposals (id) ON DELETE CASCADE, -- the proposal
    revision INTEGER NOT NULL, -- which revision
    user_id TEXT NOT NULL -- Slack user id whose message the revision added
)
"""

_REACTION_MARKS = """
CREATE TABLE reaction_marks (
    channel_id TEXT NOT NULL, -- Slack channel id
    ts TEXT NOT NULL, -- Slack timestamp of the message reacted to
    user_id TEXT NOT NULL, -- Slack user id of who reacted
    keyword TEXT NOT NULL CHECK (keyword IN ('idea', 'bug', 'skip')), -- what it stands for
    marked_at TEXT NOT NULL, -- UTC, ISO 8601
    PRIMARY KEY (channel_id, ts, user_id, keyword)
)
"""

# Admin-issued, read-only operator tokens (#58). The token itself is never
# stored, only its SHA-256 digest, as for run tokens.
_OPERATOR_TOKENS = """
CREATE TABLE operator_tokens (
    id TEXT PRIMARY KEY, -- random id, named in the audit log
    name TEXT NOT NULL UNIQUE, -- the admin's label for the person, such as alice
    digest TEXT NOT NULL UNIQUE, -- sha256 of the token, hex; the token is never kept
    channels_json TEXT NOT NULL, -- JSON list of the Slack channel ids it may read
    created_at TEXT NOT NULL, -- UTC, ISO 8601
    expires_at TEXT NOT NULL, -- UTC, ISO 8601
    revoked_at TEXT, -- UTC, ISO 8601, once revoked
    last_used_at TEXT -- UTC, ISO 8601, of its last tool call
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
    Migration(
        5,
        (
            "ALTER TABLE proposals ADD COLUMN card_ts TEXT"
            " /* Slack ts of the proposal's card message, so later states edit it (#26) */",
        ),
    ),
    Migration(
        6, (_PENDING_THREADS, "CREATE INDEX pending_threads_due ON pending_threads (due_at)")
    ),
    # Help notices are sent once per person or channel, across restarts (#80).
    Migration(7, (_HELP_NOTICES,)),
    # The store is the card's outbox: a card is due while it shows an older revision (#152).
    Migration(
        8,
        (
            "ALTER TABLE proposals ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            " /* rises with every change a proposal's card shows (#152) */",
            "ALTER TABLE proposals ADD COLUMN card_revision INTEGER"
            " /* the revision the posted card shows; null before it is posted */",
            # A card already posted shows what is stored now. Before #152 nothing posted
            # cards, so only a proposal still waiting for a person gets one after the upgrade.
            "UPDATE proposals SET card_revision = revision"
            " WHERE card_ts IS NOT NULL OR state != 'pending'",
            "CREATE INDEX proposals_card_due ON proposals (created_at)"
            " WHERE card_revision IS NULL OR card_revision < revision",
        ),
    ),
    # The thread is told the issue URL once its proposal is applied (#32).
    Migration(
        9,
        (
            "ALTER TABLE proposals ADD COLUMN thread_told_at TEXT"
            " /* UTC, ISO 8601: when the thread was told the issue URL (#32) */",
            # Nothing told threads before #32: an old applied proposal stays quiet.
            "UPDATE proposals SET thread_told_at = updated_at WHERE state = 'applied'",
            "CREATE INDEX proposals_thread_due ON proposals (created_at)"
            " WHERE state = 'applied' AND thread_told_at IS NULL",
        ),
    ),
    # Edits made from the card: who, when, and which field (#160).
    Migration(
        10,
        (_PROPOSAL_EDITS, "CREATE INDEX proposal_edits_proposal ON proposal_edits (proposal_id)"),
    ),
    # Who reacted with a keyword's reaction, so removing it can take the keyword back (#138).
    Migration(
        11,
        (
            _REACTION_MARKS,
            # A person's skip from before marks were kept stays: no reaction removed
            # later can undo it, as for a skip: in the text (CodeRabbit).
            "INSERT INTO reaction_marks (channel_id, ts, user_id, keyword, marked_at)"
            " SELECT channel_id, ts, 'text', 'skip', linked_at FROM message_refs"
            " WHERE chatter = 1 AND linked_by = 'person'",
        ),
    ),
    Migration(12, (_OPERATOR_TOKENS,)),
    Migration(13, (_IDENTITY_UNLINKS,)),
    # "Same as" (#36): the issue a person said the proposal duplicates, who said so,
    # and who undid it. Pointers and Slack ids only. The store keeps them consistent:
    # all four set together, only on a rejected proposal (SQLite can't add that CHECK).
    Migration(
        14,
        (
            "ALTER TABLE proposals ADD COLUMN duplicate_repo TEXT"
            " /* owner/name of the issue a person marked it the same as */",
            "ALTER TABLE proposals ADD COLUMN duplicate_number INTEGER /* that issue's number */",
            "ALTER TABLE proposals ADD COLUMN duplicate_by TEXT /* Slack user id who marked it */",
            "ALTER TABLE proposals ADD COLUMN duplicate_at TEXT /* UTC, ISO 8601 */",
            "ALTER TABLE proposals ADD COLUMN duplicate_undone_by TEXT"
            " /* Slack user id who undid it, if anyone */",
            "ALTER TABLE proposals ADD COLUMN duplicate_undone_at TEXT /* UTC, ISO 8601 */",
        ),
    ),
    # Verifying a GitHub link with a one-time public code (#195).
    Migration(
        15,
        (
            "ALTER TABLE identity_links ADD COLUMN verified_at TEXT"
            " /* UTC, ISO 8601: when the person proved control of the account */",
            "ALTER TABLE identity_links ADD COLUMN github_id INTEGER"
            " /* GitHub's numeric user id, which survives a rename */",
            _IDENTITY_CODES,
        ),
    ),
    # Follow-ups revise a pending draft (#304). Pointers and Slack ids only.
    Migration(
        16,
        (
            _PROPOSAL_REVISIONS,
            "CREATE INDEX proposal_revisions_proposal ON proposal_revisions (proposal_id)",
            _PROPOSAL_REVISION_AUTHORS,
            "CREATE INDEX proposal_revision_authors_proposal"
            " ON proposal_revision_authors (proposal_id)",
            "ALTER TABLE proposals ADD COLUMN follow_up_due_at TEXT"
            " /* UTC: a revision lost a race; draft the thread again once settled */",
        ),
    ),
    # Admin-loaded links (#305): who made the link, so a card can say so, and a self-add
    # can trust it like a verified one. Existing links were all made by the person.
    Migration(
        17,
        (
            "ALTER TABLE identity_links ADD COLUMN linked_by TEXT NOT NULL DEFAULT 'self'"
            " CHECK (linked_by IN ('self', 'admin'))"
            " /* self: /jotflow link or verify; admin: jotflow identity link */",
        ),
    ),
    # Weekly summaries (#308): one row per channel and week, and the topics put off
    # to a later week. Pointers, Slack ids and UTC times only, never text.
    Migration(
        18,
        (
            """
CREATE TABLE summaries (
    channel_id TEXT NOT NULL, -- the channel the summary is for
    slot TEXT NOT NULL, -- UTC, ISO 8601: when this week's summary was due
    status TEXT NOT NULL -- this week's post: posting, posted, failed or skipped
        CHECK (status IN ('posting', 'posted', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 1, -- tries so far for this slot
    ts TEXT, -- the posted message's ts, once posted
    updated_at TEXT NOT NULL, -- UTC, ISO 8601
    PRIMARY KEY (channel_id, slot)
)
""",
            """
CREATE TABLE summary_deferrals (
    topic_id TEXT NOT NULL, -- the topic put off
    channel_id TEXT NOT NULL, -- its channel
    until_slot TEXT NOT NULL, -- UTC, ISO 8601: the summary it's put off to
    deferred_by TEXT NOT NULL, -- Slack user id who clicked Not this week
    deferred_at TEXT NOT NULL, -- UTC, ISO 8601
    PRIMARY KEY (topic_id, until_slot)
)
""",
            "CREATE INDEX summary_deferrals_slot ON summary_deferrals (channel_id, until_slot)",
        ),
    ),
    # A person may change a card's Projects boards (#309). SQLite can't alter a CHECK,
    # so proposal_edits is rebuilt with 'boards' allowed, keeping every row in order.
    Migration(
        19,
        (
            _PROPOSAL_EDITS_WITH_BOARDS,
            "INSERT INTO proposal_edits_new (proposal_id, edited_by, field, edited_at)"
            " SELECT proposal_id, edited_by, field, edited_at FROM proposal_edits ORDER BY rowid",
            "DROP TABLE proposal_edits",
            "ALTER TABLE proposal_edits_new RENAME TO proposal_edits",
            "CREATE INDEX proposal_edits_proposal ON proposal_edits (proposal_id)",
        ),
    ),
    # The topics each summary offered Not this week for (#315 review), recorded before
    # posting, so a click can only put off a topic its own summary listed.
    Migration(
        20,
        (
            """
CREATE TABLE summary_topics (
    channel_id TEXT NOT NULL, -- the summary's channel
    slot TEXT NOT NULL, -- UTC, ISO 8601: the summary's slot
    topic_id TEXT NOT NULL, -- a topic it listed with Not this week
    PRIMARY KEY (channel_id, slot, topic_id)
)
""",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version
