"""Slack attachments (#333): the reader's rules, the packet, the snapshot, the card,
execution's live check, and clearing. Security's five conditions are pinned here."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx2
import pytest

from jotflow.models import AttachedFile, IssueProposal, MessageRef, ProposalRecord, ProposalState
from jotflow.orchestrator import slack_files
from jotflow.orchestrator.slack_files import (
    LOOKS_SECRET,
    NOT_READABLE,
    NOT_TEXT,
    TEXT_OFF,
    TOO_LARGE,
    BoltFileSlack,
    FileFacts,
    TooLarge,
    attachments_section,
    decode,
    facts,
    is_text,
    looks_secret,
    read_text,
    safe_url,
)
from jotflow.policy import parse_policy
from jotflow.policy_writer import render_policy
from jotflow.store import MemoryStore, content_hash
from jotflow.store.base import cleared_draft
from jotflow.whiteboard.tools import ToolError
from tests.test_write_tools import CH, DRAFT, ROOT, Clock, Ids, LiveStore, Poster, issue, link

BASE = f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "Web"
"""
URL = "https://files.slack.com/files-pri/T0ACME-F0DOC1/oven-names.md"
TEXT = "# Oven names\n\nEach oven is named after a river.\n"


def raw_file(file_id: str = "F0DOC1", **extra: Any) -> dict[str, Any]:
    base = {
        "id": file_id,
        "name": "oven-names.md",
        "filetype": "markdown",
        "mimetype": "text/markdown",
        "size": len(TEXT.encode()),
        "url_private": URL.replace("F0DOC1", file_id),
        "channels": [CH],
    }
    return {**base, **extra}


class Files:
    """Slack's files.info and the download, from a table. Records every URL fetched."""

    def __init__(self, texts: dict[str, bytes] | None = None) -> None:
        self.texts = texts if texts is not None else {"F0DOC1": TEXT.encode()}
        self.raw: dict[str, dict[str, Any]] = {k: raw_file(k) for k in self.texts}
        self.fetched: list[str] = []

    async def info(self, file_id: str) -> Mapping[str, Any]:
        if file_id not in self.raw:
            raise LookupError("file_not_found")
        return self.raw[file_id]

    async def download(self, url: str, limit: int) -> bytes:
        self.fetched.append(url)
        file_id = next(k for k in self.raw if self.raw[k]["url_private"] == url)
        data = self.texts[file_id]
        if len(data) > limit:
            raise TooLarge
        return data


def one(**extra: Any) -> FileFacts:
    found = facts(raw_file(**extra))
    assert found is not None
    return found


# The reader's rules (security's conditions 1 and 3)


@pytest.mark.parametrize(
    ("filetype", "mimetype", "text"),
    [
        ("markdown", "text/markdown", True),
        ("text", "text/plain; charset=utf-8", True),
        ("json", "application/json", True),
        ("markdown", "image/png", False),  # named like text, but it isn't
        ("png", "text/plain", False),
        ("binary", "application/octet-stream", False),
    ],
)
def test_both_the_filetype_and_the_mimetype_must_be_text(
    filetype: str, mimetype: str, text: bool
) -> None:
    assert is_text(one(filetype=filetype, mimetype=mimetype)) is text


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        (URL, True),
        ("http://files.slack.com/files-pri/T0ACME-F0DOC1/x.md", False),
        ("https://files.slack.com.evil.example/files-pri/T0ACME-F0DOC1/x.md", False),
        ("https://evil.example/files-pri/T0ACME-F0DOC1/x.md", False),
        ("https://user@files.slack.com/files-pri/T0ACME-F0DOC1/x.md", False),
        ("https://files.slack.com:8443/files-pri/T0ACME-F0DOC1/x.md", False),
        ("https://files.slack.com/files-tmb/T0ACME-F0DOC1/x.md", False),
        (None, False),
    ],
)
def test_only_a_url_private_on_exactly_files_slack_com_is_fetched(
    url: str | None, safe: bool
) -> None:
    assert safe_url(url) is safe


def test_text_is_strict_utf8_without_nul_or_invisible_characters() -> None:
    assert decode(b"\xef\xbb\xbfHello") == "Hello"  # a byte-order mark is dropped
    assert decode(b"bad \xff byte") is None
    assert decode(b"nul\x00here") is None
    assert decode("hidden​mark".encode()) == "hiddenmark"


@pytest.mark.parametrize(
    "text",
    [
        "token: ghp_" + "a" * 36,
        "github_pat_" + "A1_" * 10,
        "SLACK=xoxb-1234567890-abcdefghij",
        "aws AKIAABCDEFGHIJKLMNOP here",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijklmnop",
        "DB_PASSWORD=hunter2",
        "  api_key: s3cr3t",
    ],
)
def test_a_credentials_shape_is_never_offered(text: str) -> None:
    assert looks_secret(text)


def test_plain_prose_about_passwords_isnt_a_secret() -> None:
    assert not looks_secret("Our password policy needs a review.\nTokens expire daily.")


async def test_read_text_gives_a_fixed_reason_for_each_failure() -> None:
    files = Files()
    read = await read_text(one(), files)
    assert read.text == TEXT and read.sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
    assert (await read_text(one(mimetype="image/png"), files)).reason == NOT_TEXT
    assert (await read_text(one(size=40 * 1024), files)).reason == TOO_LARGE
    files.texts["F0DOC1"] = b"x" * (33 * 1024)  # the size Slack gave was wrong
    assert (await read_text(one(), files)).reason == TOO_LARGE
    files.texts["F0DOC1"] = b"\xff\xfe"
    assert (await read_text(one(), files)).reason == NOT_READABLE
    files.texts["F0DOC1"] = b"DB_PASSWORD=hunter2\n"
    assert (await read_text(one(), files)).reason == LOOKS_SECRET
    url_from_text = facts(raw_file(url_private="https://evil.example/x.md"))
    assert url_from_text is not None
    assert (await read_text(url_from_text, files)).text is None
    assert "https://evil.example/x.md" not in files.fetched


async def test_the_download_sends_the_token_only_there_and_never_follows_a_redirect() -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(302, headers={"Location": "https://evil.example/x"})

    class Client:
        token = "xoxb-test"

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(answer), follow_redirects=False)
    reader = BoltFileSlack(Client(), http=http)
    with pytest.raises(RuntimeError, match="302"):
        await reader.download(URL, 1024)
    [request] = seen  # nothing went to the redirect's host
    assert request.headers["Authorization"] == "Bearer xoxb-test"
    with pytest.raises(ValueError, match="not a Slack file URL"):
        await reader.download("https://evil.example/files-pri/T-F0/x", 1024)
    assert len(seen) == 1


async def test_the_download_stops_at_its_byte_cap() -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * 5000)

    class Client:
        token = "xoxb-test"

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(answer))
    with pytest.raises(TooLarge):
        await BoltFileSlack(Client(), http=http).download(URL, 1024)


def test_the_section_fences_past_any_backticks_and_cleans_the_name() -> None:
    section = attachments_section([("plan @team <!here>.md", "Use ````x```` and @everyone")])
    assert section.startswith("## Attachments")
    assert "@team" not in section and "<!here>" not in section
    assert "\n`````\nUse ````x```` and @everyone\n`````" in section
    assert attachments_section([]) == ""


# The setting


def test_the_attachments_setting_round_trips_and_defaults_to_names() -> None:
    base = BASE
    assert parse_policy(base).channels[CH].attachments == "names"
    text = parse_policy(
        base.replace(f"[channels.{CH}]\n", f'[channels.{CH}]\nattachments = "text"\n')
    )
    assert text.channels[CH].attachments == "text"
    assert parse_policy(render_policy(text)) == text
    assert parse_policy(base).attachments_for("C0NOTLISTED") == "off"


def test_check_says_what_text_needs() -> None:
    from jotflow.commands.check import attachment_notes

    policy = parse_policy(
        BASE.replace(f"[channels.{CH}]\n", f'[channels.{CH}]\nattachments = "text"\n')
    )
    [note] = attachment_notes(policy)
    assert "files:read" in note and "every file in every channel" in note


# The packet


class Reader:
    """A thread whose root carries a file."""

    def __init__(self, files: list[dict[str, Any]]) -> None:
        self.files = files

    async def replies(self, channel: str, ts: str, *, cursor: Any, limit: int) -> Any:
        root = {
            "ts": ROOT,
            "text": "Document our oven names.",
            "user": "U0SAM",
            "files": self.files,
        }
        return {"messages": [root], "has_more": False}

    async def user(self, user_id: str) -> Any:
        return {"user": {"profile": {"display_name": "Sam"}}}


async def packet(setting: str, files: list[dict[str, Any]], reader: Files | None = None) -> Any:
    from jotflow.orchestrator.packet import SlackWorkSource

    source = SlackWorkSource(
        Reader(files), MemoryStore(), files=reader or Files(), attachments_for=lambda c: setting
    )
    return await source.thread(CH, ROOT)


async def test_off_shows_nothing_names_shows_facts_text_reads_small_text_files() -> None:
    off = await packet("off", [raw_file()])
    assert off.messages[0].files == []
    names = await packet(
        "names", [raw_file(), raw_file("F0IMG2", filetype="png", mimetype="image/png")]
    )
    doc, image = names.messages[0].files
    assert (doc.name.text, doc.filetype, doc.size, doc.text) == (
        "oven-names.md",
        "markdown",
        len(TEXT),
        None,
    )
    assert doc.not_read == TEXT_OFF and image.not_read is None
    text = await packet("text", [raw_file()])
    [brief] = text.messages[0].files
    assert brief.text is not None and brief.text.text == TEXT
    dumped = text.model_dump_json()
    assert "files.slack.com" not in dumped and "url_private" not in dumped  # never a URL
    assert "sha256" not in dumped  # JotFlow's record only


async def test_a_thread_reads_at_most_64_kib_of_file_text() -> None:
    big = b"a" * (30 * 1024)
    reader = Files({"F0A1": big, "F0A2": big, "F0A3": big})
    files = [raw_file(k, size=len(big)) for k in ("F0A1", "F0A2", "F0A3")]
    thread = await packet("text", files, reader)
    read = [f.text is not None for f in thread.messages[0].files]
    assert read == [True, True, False]
    assert thread.messages[0].files[2].not_read == slack_files.THREAD_FULL


# Proposing


TEXT_POLICY = parse_policy(
    f"""
[channels.{CH}]
attachments = "text"

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app"
labels = ["bug", "enhancement"]
"""
)


async def served(store: LiveStore, files: Files, file_id: str = "F0DOC1") -> None:
    from jotflow.orchestrator.slack_files import SERVED_FILE
    from jotflow.store.base import ToolCallRecord

    await store.record_packet("run-1", [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)])
    sha = hashlib.sha256(files.texts[file_id]).hexdigest()
    await store.record_tool_call(
        ToolCallRecord(
            run_id="run-1",
            tool=SERVED_FILE,
            args_hash=file_id,
            channel_id=CH,
            thread_ts=ROOT,
            result_json=json.dumps({"sha256": sha}),
            called_at=Clock()(),
        )
    )


async def propose(
    files: Files, *, policy: Any = TEXT_POLICY, serve: bool = True, **extra: Any
) -> tuple[Any, LiveStore]:
    clock = Clock()
    store = LiveStore(clock)
    if serve:
        await served(store, files)
    else:
        await store.record_packet("run-1", [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)])
    from jotflow.whiteboard.write_tools import WriteTools

    tools = WriteTools(store, policy, Poster(), clock=clock, new_id=Ids(), files=files)
    topic: Any = await tools.link_message(DRAFT, link())
    args = issue(
        topic.topic_id,
        repo="acme-co/web",
        sources=[{"channel_id": CH, "ts": ROOT}],
        attachments=["F0DOC1"],
        **extra,
    )
    return await tools.propose_issue(DRAFT, args), store


async def test_a_served_file_is_snapshotted_into_the_hashed_draft() -> None:
    result, store = await propose(Files())
    record = await store.get_proposal(result.proposal_id)
    assert record is not None and isinstance(record.proposal, IssueProposal)
    [attached] = record.proposal.attached
    assert (attached.file_id, attached.name, attached.text) == ("F0DOC1", "oven-names.md", TEXT)
    assert attached.sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
    changed = record.proposal.model_copy(
        update={"attached": [attached.model_copy(update={"text": "other"})]}
    )
    assert content_hash(changed) != content_hash(record.proposal)  # approval locks it


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("names", "doesn't allow copying file text"),
        ("not served", "isn't a file this run was given the text of"),
        ("unshared", "is no longer shared in this channel"),
        ("deleted", "is no longer shared in this channel"),
        ("changed", "changed since this run read it"),
        ("secret", "looks like it holds a password"),
    ],
)
async def test_a_file_is_refused_unless_it_is_still_as_served(setup: str, reason: str) -> None:
    files = Files()
    policy: Any = TEXT_POLICY
    serve = setup != "not served"
    if setup == "names":
        from tests.test_write_tools import POLICY

        policy = POLICY
    clock = Clock()
    store = LiveStore(clock)
    if serve:
        await served(store, files)
    else:
        await store.record_packet("run-1", [MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT)])
    if setup == "unshared":
        files.raw["F0DOC1"]["channels"] = ["C0ELSEWHERE"]
    if setup == "deleted":
        del files.raw["F0DOC1"]
    if setup == "changed":
        files.texts["F0DOC1"] = b"# Oven names\n\nRenamed.\n"
    if setup == "secret":
        files.texts["F0DOC1"] = b"DB_PASSWORD=hunter2\n"
    from jotflow.whiteboard.write_tools import WriteTools

    tools = WriteTools(store, policy, Poster(), clock=clock, new_id=Ids(), files=files)
    topic: Any = await tools.link_message(DRAFT, link())
    args = issue(
        topic.topic_id,
        repo="acme-co/web",
        sources=[{"channel_id": CH, "ts": ROOT}],
        attachments=["F0DOC1"],
    )
    with pytest.raises(ToolError, match=reason):
        await tools.propose_issue(DRAFT, args)


async def test_a_slack_link_in_a_file_is_refused_under_issue_links_none() -> None:
    files = Files({"F0DOC1": b"See https://acme.slack.com/archives/C0EXAMPLE1/p1792310400000100\n"})
    policy = TEXT_POLICY.model_copy(update={"issue_links": "none"})
    with pytest.raises(ToolError, match="holds a Slack link"):
        await propose(files, policy=policy)


async def test_the_agent_can_never_supply_the_snapshot_itself() -> None:
    from jotflow.whiteboard.write_tools import ProposeIssueArgs

    snapshot = {"file_id": "F0X1", "name": "x", "size": 1, "sha256": "0" * 64, "text": "x"}
    with pytest.raises(Exception, match="attached is JotFlow's"):
        ProposeIssueArgs.model_validate(
            {
                "repo": "acme-co/web",
                "title": "T",
                "body": "B",
                "confidence": 0.5,
                "attached": [snapshot],
            }
        )


# The card, execution, and clearing


def attached_record(state: ProposalState = ProposalState.PENDING) -> ProposalRecord:
    from tests.test_runs import _proposal_record

    record = _proposal_record("0123456789abcdef")
    attached = AttachedFile(
        file_id="F0DOC1",
        name="oven-names.md",
        size=len(TEXT.encode()),
        sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
        text=TEXT,
    )
    draft = record.proposal.model_copy(update={"attached": [attached]})
    return record.model_copy(update={"proposal": draft, "state": state})


def test_the_card_shows_what_will_be_included_and_warns_for_a_public_repo() -> None:
    from jotflow.orchestrator.cards import CardContext, render_card

    record = attached_record()
    context = CardContext(
        channel=TEXT_POLICY.channel(CH), visibility={record.proposal.repo: "public"}
    )
    text = json.dumps(render_card(record, context).blocks)
    assert "Includes oven-names.md, 48 bytes, from Slack, as a code block in the issue:" in text
    assert "Each oven is named after a river." in text
    assert "So will the attached files' text." in text


def test_clearing_a_draft_clears_its_attachments() -> None:
    cleared = cleared_draft(attached_record().proposal)
    assert isinstance(cleared, IssueProposal) and cleared.attached == []


def test_the_planned_body_puts_the_section_between_body_and_footer() -> None:
    from jotflow.execution.writes import plan

    record = attached_record(ProposalState.APPROVED)
    section = attachments_section([("oven-names.md", TEXT)])
    _, [write] = plan(record, "<!-- jotflow:proposal:x -->", None, section)
    body = write.arguments["body"]
    assert body.index("## Attachments") < body.index("<!-- jotflow:proposal:x -->")
    assert "Each oven is named after a river." in body


async def test_execution_keeps_a_file_as_approved_and_leaves_out_one_that_changed() -> None:
    # Security's condition 2: a deletion or change after approval wins.
    from datetime import UTC, datetime

    from jotflow.models import RunKind
    from jotflow.orchestrator.slack_files import LEFT_OUT
    from jotflow.orchestrator.thread_notice import ThreadNotice
    from jotflow.store.base import AUTO_APPROVER
    from jotflow.whiteboard.read_tools import read_tools
    from tests.test_read_tools import CH as RCH
    from tests.test_read_tools import POLICY as RPOLICY
    from tests.test_read_tools import POST, FakeSource, call, checked_registry, running
    from tests.test_read_tools import Clock as RClock
    from tests.test_thread_notice import FakeThreadSlack

    old = b"Old text.\n"
    files = Files({"F0DOC1": TEXT.encode(), "F0DOC2": b"New text.\n"})
    for raw in files.raw.values():
        raw["channels"] = [RCH]

    def snap(file_id: str, data: bytes) -> AttachedFile:
        return AttachedFile(
            file_id=file_id,
            name=f"{file_id}.md",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            text=data.decode(),
        )

    store = MemoryStore()
    draft = IssueProposal(
        repo="acme-co/web",
        title="Oven names",
        body="Document them.",
        confidence=0.9,
        attached=[snap("F0DOC1", TEXT.encode()), snap("F0DOC2", old)],
    )
    record = ProposalRecord(
        id="p1",
        channel_id=RCH,
        thread_ts=POST,
        run_id="r0",
        proposal=draft,
        created_at=datetime(2026, 10, 29, 8, 0, tzinfo=UTC),
    )
    await store.add_proposal(record)
    await store.transition("p1", ProposalState.APPROVED, approved_by=AUTO_APPROVER)
    specs = checked_registry(read_tools(store, RPOLICY, FakeSource(), clock=RClock(), files=files))
    work: Any = await call(
        specs["get_work"], await running(store, RunKind.EXECUTION, proposal_id="p1")
    )
    body = work.execution.writes[0].arguments["body"]
    assert "Each oven is named after a river." in body
    assert "Old text." not in body and "New text." not in body
    assert [
        json.loads(r)["name"] for r in await store.tool_call_results("attachments:p1", (LEFT_OUT,))
    ] == ["F0DOC2.md"]
    notice = ThreadNotice(store, RPOLICY, FakeThreadSlack())
    line = await notice._left_out(record)
    assert line.startswith("Left out F0DOC2.md: deleted, unshared or changed in Slack")


async def test_get_work_records_only_the_files_it_gave_text_for() -> None:
    from jotflow.orchestrator.packet import SlackWorkSource
    from jotflow.orchestrator.slack_files import SERVED_FILE
    from jotflow.whiteboard.read_tools import read_tools
    from tests.test_read_tools import Clock as RClock
    from tests.test_read_tools import call, checked_registry, running

    store = MemoryStore()
    files = [raw_file(), raw_file("F0IMG2", filetype="png", mimetype="image/png")]
    source = SlackWorkSource(Reader(files), store, files=Files(), attachments_for=lambda c: "text")
    grant = await running(store)
    from tests.test_read_tools import POLICY as RPOLICY

    specs = checked_registry(read_tools(store, RPOLICY, source, clock=RClock()))
    await call(specs["get_work"], grant)
    assert await store.find_tool_call(grant.run_id, SERVED_FILE, "F0DOC1") is not None
    assert await store.find_tool_call(grant.run_id, SERVED_FILE, "F0IMG2") is None
