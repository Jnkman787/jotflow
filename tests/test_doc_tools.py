"""Reading repo docs while drafting (#209): patterns, the two tools, caps, and the card."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from jotflow.doc_paths import (
    MAX_BYTES_PER_RUN,
    MAX_LISTED,
    MAX_READS_PER_RUN,
    TRUNCATED_MARKER,
    PathRefused,
    allowed,
    check_path,
    check_pattern,
    check_patterns,
    matches,
)
from jotflow.models import MessageRef, RunKind
from jotflow.orchestrator.card_sync import card_context
from jotflow.orchestrator.cards import CardContext, _docs_blocks
from jotflow.orchestrator.launch import DocMissing, RunnerClient, RunnerEndpoint, RunnerError
from jotflow.policy import Policy, PolicyError, parse_policy
from jotflow.policy_writer import render_policy
from jotflow.whiteboard.doc_tools import (
    NO_DOCS,
    PASSAGES,
    READ_TOOL,
    UNAVAILABLE,
    DocReads,
    ListRepoDocsInput,
    ReadRepoDocInput,
    card_lines,
    doc_tools,
    has_long_passage,
    probe_docs,
    sampled_shingles,
)
from jotflow.whiteboard.tokens import Grant
from jotflow.whiteboard.tools import ToolError, ToolSpec
from jotflow.whiteboard.write_tools import WriteTools
from tests.test_write_tools import (
    CH,
    DRAFT,
    POLICY,
    REPLY,
    ROOT,
    Clock,
    Ids,
    LiveStore,
    Poster,
    issue,
    new_topic,
)

PATTERNS = ("README.md", "docs/plans/**/*.md", "docs/adr/*.md")


def with_read_paths(policy: Policy, patterns: tuple[str, ...] = PATTERNS) -> Policy:
    """``policy`` with read_paths on acme-co/web in every channel that lists it."""
    channels = {}
    for channel_id, channel in policy.channels.items():
        repos = tuple(
            r.model_copy(update={"read_paths": patterns}) if r.repo == "acme-co/web" else r
            for r in channel.repos
        )
        channels[channel_id] = channel.model_copy(update={"repos": repos})
    return policy.model_copy(update={"channels": channels})


# Patterns and paths


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("README.md", "README.md", True),
        ("README.md", "readme.md", False),  # case-sensitive, as GitHub's tree reports
        ("docs/adr/*.md", "docs/adr/3.md", True),
        ("docs/adr/*.md", "docs/adr/old/3.md", False),  # * never crosses a slash
        ("docs/plans/**/*.md", "docs/plans/x.md", True),  # ** may be no segments
        ("docs/plans/**/*.md", "docs/plans/a/b/c.md", True),
        ("docs/plans/**/*.md", "docs/plans/.drafts/c.md", False),  # ** skips hidden
        ("docs/*/x.md", "docs/.hidden/x.md", False),  # so does *
        ("docs/?.md", "docs/a.md", True),
        ("docs/?.md", "docs/.md", False),
        ("docs/.notes/x.md", "docs/.notes/x.md", True),  # written out, a dot is fine
        ("**/*.md", "a/b.md", True),
        ("**/*.md", ".github/workflows/x.md", False),
    ],
)
def test_matching(pattern: str, path: str, expected: bool) -> None:
    check_pattern(pattern)
    assert matches(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/docs/x.md",
        "docs/../secrets.md",
        "./docs/x.md",
        "docs//x.md",
        "docs\\x.md",
        "docs/%2e%2e/x.md",
        "docs/x\x00.md",
        "docs/x​.md",
        "docs/**",
        "docs/*",
        "docs/x.py",
        "docs/a**b/x.md",
        ".git/x.md",
        ".GIT/x.md",
        ".github/workflows/ci.md",
        "a/.github/Workflows/x.md",
        ".env.md",
        "x/.envrc.md",
        "d/" * 100 + "x.md",
    ],
)
def test_refused_patterns(pattern: str) -> None:
    with pytest.raises(PathRefused):
        check_pattern(pattern)


def test_a_pattern_list_is_bounded_and_not_empty() -> None:
    with pytest.raises(PathRefused, match="ambiguous"):
        check_patterns(())
    with pytest.raises(PathRefused, match="at most 20"):
        check_patterns(tuple(f"d/{i}.md" for i in range(21)))
    with pytest.raises(PathRefused, match="more than once"):
        check_patterns(("a.md", "a.md"))


@pytest.mark.parametrize(
    "path", ["../x.md", "a/../x.md", "/x.md", "a//x.md", ".git/x.md", "x.sh", "a\\b.md", ""]
)
def test_refused_paths_are_never_allowed(path: str) -> None:
    with pytest.raises(PathRefused):
        check_path(path)
    assert allowed(("**/*.md", "*.sh"), path) is False


def test_a_long_star_run_is_matched_quickly() -> None:
    pattern = "*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*.md"
    check_pattern(pattern)
    assert matches(pattern, "a" * 180 + "b.txt") is False


# The policy


def policy_text(extra: str) -> str:
    return f"""
[channels.{CH}]

[[channels.{CH}.repos]]
repo = "acme-co/web"
nickname = "web"
description = "The web app"
{extra}
"""


def test_read_paths_load_render_and_round_trip() -> None:
    policy = parse_policy(policy_text('read_paths = ["README.md", "docs/adr/*.md"]'))
    assert policy.reads_docs
    assert policy.repo(CH, "web").read_paths == ("README.md", "docs/adr/*.md")
    assert parse_policy(render_policy(policy)) == policy
    assert not parse_policy(policy_text("")).reads_docs


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ('read_paths = ["../x.md"]', "empty, . or .. segments"),
        ("read_paths = []", "empty read_paths list is ambiguous"),
        ('read_paths = [".github/workflows/x.md"]', "may not reach .git"),
    ],
)
def test_bad_read_paths_fail_to_load(line: str, reason: str) -> None:
    with pytest.raises(PolicyError, match=re.escape(reason)):
        parse_policy(policy_text(line))


# The tools


@dataclass
class Listing:
    paths: list[str]
    truncated: bool = False


@dataclass
class Doc:
    path: str
    sha: str
    text: str
    truncated: bool = False


@dataclass
class FakeRunner:
    files: dict[str, str] = field(
        default_factory=lambda: {
            "README.md": "Read me",
            "docs/plans/x.md": "The plan for x",
            "docs/adr/3.md": "ADR three",
            "docs/secret.md": "secret",
            "src/app.md": "source notes",
        }
    )
    lists: list[str] = field(default_factory=list)
    reads: list[tuple[str, str]] = field(default_factory=list)
    error: Exception | None = None

    async def docs_list(self, repo: str) -> Listing:
        if self.error is not None:
            raise self.error
        self.lists.append(repo)
        return Listing(sorted(self.files))

    async def docs_read(self, repo: str, path: str) -> Doc:
        if self.error is not None:
            raise self.error
        await asyncio.sleep(0)  # a real runner call yields, so parallel calls interleave
        self.reads.append((repo, path))
        return Doc(path, "a" * 40, self.files[path])


@dataclass
class Harness:
    store: LiveStore
    runner: FakeRunner
    reads: DocReads
    tools: dict[str, ToolSpec]
    writes: WriteTools

    async def list(self, grant: Grant = DRAFT, repo: str = "web") -> Any:
        return await self.tools["list_repo_docs"].handler(grant, ListRepoDocsInput(repo=repo))

    async def read(self, path: str, grant: Grant = DRAFT, repo: str = "web") -> Any:
        return await self.tools[READ_TOOL].handler(grant, ReadRepoDocInput(repo=repo, path=path))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def h(clock: Clock) -> Harness:
    store = LiveStore(clock)
    runner = FakeRunner()
    reads = DocReads(store)
    policy = with_read_paths(POLICY)
    source: list[FakeRunner | None] = [runner]
    specs = doc_tools(store, policy, lambda: source[0], reads, clock=clock)
    writes = WriteTools(store, policy, Poster(), clock=clock, new_id=Ids(), docs=reads)
    await store.record_packet(
        "run-1",
        [
            MessageRef(channel_id=CH, ts=ROOT, thread_ts=ROOT),
            MessageRef(channel_id=CH, ts=REPLY, thread_ts=ROOT),
        ],
    )
    return Harness(store, runner, reads, {s.name: s for s in specs}, writes)


async def test_only_allowed_paths_are_listed(h: Harness) -> None:
    listed = await h.list()
    assert listed.repo == "acme-co/web"
    assert listed.paths == ["README.md", "docs/adr/3.md", "docs/plans/x.md"]
    assert listed.more == 0


async def test_the_listing_is_capped(h: Harness) -> None:
    h.runner.files = {f"docs/adr/{i:04}.md": "x" for i in range(MAX_LISTED + 7)}
    listed = await h.list()
    assert (len(listed.paths), listed.more) == (MAX_LISTED, 7)


async def test_a_read_returns_untrusted_text_and_is_on_the_ledger(h: Harness) -> None:
    doc = await h.read("docs/plans/x.md")
    assert doc.text.untrusted is True and doc.text.text == "The plan for x"
    assert doc.reads_left == MAX_READS_PER_RUN - 1
    rows = await h.store.tool_call_results("run-1", (READ_TOOL,))
    assert [json.loads(r) for r in rows] == [
        {"repo": "acme-co/web", "path": "docs/plans/x.md", "sha": "a" * 40}
    ]
    assert "The plan" not in rows[0]  # never content
    await h.read("docs/plans/x.md")  # a re-read adds no second row
    assert len(await h.store.tool_call_results("run-1", (READ_TOOL,))) == 1


@pytest.mark.parametrize(
    "path",
    [
        "docs/secret.md",  # in the tree, not allowed
        "src/app.md",
        "docs/plans/missing.md",  # allowed shape, not in the tree
        "docs/plans/../secret.md",
        "./docs/adr/3.md",
        "DOCS/adr/3.md",
        "docs/adr/3.md ",
    ],
)
async def test_a_path_not_listed_exactly_is_refused_before_the_runner(
    h: Harness, path: str
) -> None:
    with pytest.raises(ToolError, match="isn't a doc this run may read"):
        await h.read(path)
    assert h.runner.reads == []


async def test_a_repo_without_read_paths_has_no_docs(h: Harness) -> None:
    with pytest.raises(ToolError, match="has no docs JotFlow may read"):
        await h.list(repo="api")
    with pytest.raises(ToolError, match="has no docs"):
        await h.read("README.md", repo="api")
    assert h.runner.lists == []
    assert NO_DOCS.format(repo="x")


async def test_a_repo_outside_the_channel_is_refused(h: Harness) -> None:
    with pytest.raises(ToolError):
        await h.list(repo="acme-co/other")
    with pytest.raises(ToolError):
        await h.read("README.md", repo="someone/else")
    assert h.runner.lists == []


async def test_only_a_running_drafting_run_may_read(h: Harness) -> None:
    h.store.ended.add("run-1")
    with pytest.raises(ToolError, match="is not running"):
        await h.list()
    question = h.store.register(
        Grant(run_id="run-q", kind=RunKind.QUESTION, channel_id=CH, thread_ts=ROOT)
    )
    with pytest.raises(ToolError, match="is not running"):
        await h.read("README.md", grant=question)
    assert {spec.kinds for spec in h.tools.values()} == {frozenset({RunKind.DRAFTING})}


async def test_reads_are_capped_per_run(h: Harness) -> None:
    for _ in range(MAX_READS_PER_RUN):
        await h.read("README.md")
    with pytest.raises(ToolError, match=f"read its {MAX_READS_PER_RUN} docs"):
        await h.read("docs/adr/3.md")


async def test_the_read_cap_survives_a_restart_through_the_ledger(h: Harness) -> None:
    h.runner.files = {f"docs/adr/{i}.md": "x" for i in range(MAX_READS_PER_RUN + 1)}
    for i in range(MAX_READS_PER_RUN):
        await h.read(f"docs/adr/{i}.md")
    fresh = DocReads(h.store)
    specs = doc_tools(h.store, with_read_paths(POLICY), lambda: h.runner, fresh)
    again = {s.name: s for s in specs}
    with pytest.raises(ToolError, match="read its"):
        await again[READ_TOOL].handler(
            DRAFT, ReadRepoDocInput(repo="web", path=f"docs/adr/{MAX_READS_PER_RUN}.md")
        )


async def test_bytes_are_capped_per_run(h: Harness) -> None:
    big = "x" * (60 * 1024)
    h.runner.files = {f"docs/adr/{i}.md": big for i in range(6)}
    for i in range(4):
        await h.read(f"docs/adr/{i}.md")
    with pytest.raises(ToolError, match=f"{MAX_BYTES_PER_RUN // 1024} KiB"):
        await h.read("docs/adr/5.md")


async def test_a_truncated_file_says_so(h: Harness) -> None:
    async def cut(repo: str, path: str) -> Doc:
        return Doc(path, "a" * 40, "start", truncated=True)

    h.runner.docs_read = cut  # type: ignore[method-assign]
    doc = await h.read("README.md")
    assert doc.truncated is True and doc.text.text == "start" + TRUNCATED_MARKER


async def test_a_runner_failure_is_a_fixed_refusal(h: Harness) -> None:
    h.runner.error = RunnerError("GitHub answered 500 with secret detail")
    with pytest.raises(ToolError) as refused:
        await h.list()
    assert refused.value.reason == UNAVAILABLE


async def test_no_runner_is_a_fixed_refusal(clock: Clock) -> None:
    store = LiveStore(clock)
    specs = doc_tools(store, with_read_paths(POLICY), lambda: None, DocReads(store))
    with pytest.raises(ToolError) as refused:
        await specs[0].handler(DRAFT, ListRepoDocsInput(repo="web"))
    assert refused.value.reason == UNAVAILABLE


def test_no_read_paths_means_no_tools(clock: Clock) -> None:
    store = LiveStore(clock)
    assert doc_tools(store, POLICY, lambda: FakeRunner(), DocReads(store)) == ()


# The copy check

PASSAGE = (
    "The export service streams rows in pages of five hundred, writes each page to a "
    "temporary object, and stitches the parts together only once every page has been "
    "acknowledged by the storage layer, so a retry never duplicates a row in the file."
)


def test_a_long_verbatim_passage_is_found_and_a_short_one_is_not() -> None:
    assert len(PASSAGE) >= 200
    shingles = sampled_shingles("Intro.\n\n" + PASSAGE + "\n\nMore text follows here.")
    assert has_long_passage(shingles, "We should do this: " + PASSAGE)
    # Whitespace and case changes don't hide it.
    assert has_long_passage(shingles, PASSAGE.upper().replace(" ", "\n  "))
    assert not has_long_passage(shingles, PASSAGE[:150])
    assert not has_long_passage(shingles, "An unrelated body about something else entirely.")


@pytest.mark.parametrize("start", range(0, 16))
def test_every_200_character_passage_is_found_whatever_its_offset(start: int) -> None:
    doc = "z" * start + " " + PASSAGE * 2
    shingles = sampled_shingles(doc)
    piece = (PASSAGE * 2)[37 : 37 + 200]
    assert has_long_passage(shingles, piece)


async def test_a_proposal_copying_a_doc_is_flagged_on_the_ledger(h: Harness) -> None:
    h.runner.files["docs/plans/x.md"] = "Plan.\n" + PASSAGE
    await h.read("docs/plans/x.md")
    topic = await new_topic(h.writes)
    result: Any = await h.writes.propose_issue(DRAFT, issue(topic, body="Copy: " + PASSAGE))
    rows = await h.store.tool_call_results("run-1", (PASSAGES,))
    assert [json.loads(r) for r in rows] == [
        {"proposal_id": result.proposal_id, "docs": ["acme-co/web:docs/plans/x.md"]}
    ]


async def test_a_proposal_in_its_own_words_is_not_flagged(h: Harness) -> None:
    h.runner.files["docs/plans/x.md"] = PASSAGE
    await h.read("docs/plans/x.md")
    topic = await new_topic(h.writes)
    await h.writes.propose_issue(DRAFT, issue(topic, body="Pages are stitched once acked."))
    assert await h.store.tool_call_results("run-1", (PASSAGES,)) == []


# The card


async def test_the_card_lists_every_doc_read_and_the_copied_one(h: Harness) -> None:
    h.runner.files["docs/plans/x.md"] = PASSAGE
    await h.read("docs/plans/x.md")
    await h.read("README.md")
    topic = await new_topic(h.writes)
    result: Any = await h.writes.propose_issue(DRAFT, issue(topic, body=PASSAGE))
    record = await h.store.get_proposal(result.proposal_id)
    assert record is not None

    class People:
        async def name(self, user: str) -> str | None:
            return None

    context = await card_context(with_read_paths(POLICY), h.store, People(), record)  # type: ignore[arg-type]
    assert context.docs_read == (
        ("acme-co/web", "docs/plans/x.md"),
        ("acme-co/web", "README.md"),
    )
    assert context.docs_copied == ("acme-co/web:docs/plans/x.md",)
    read, copied = _docs_blocks(context)
    assert read["elements"][0] == {
        "type": "plain_text",
        "text": "Read: web: docs/plans/x.md, README.md",
        "emoji": False,
    }
    assert copied["elements"][0]["text"] == (
        "Contains a long passage copied word for word from web: docs/plans/x.md"
    )
    assert copied["elements"][0]["type"] == "plain_text"  # a path can't ping or link


def test_a_card_never_cuts_the_read_list() -> None:
    channel = POLICY.channel(CH)
    paths = [("acme-co/web", "docs/" + "p" * 390 + f"{i:02}.md") for i in range(20)]
    [block] = _docs_blocks(CardContext(channel=channel, docs_read=tuple(paths)))
    text = "".join(e["text"] for e in block["elements"])
    for _, path in paths:
        assert path in text
    assert all(len(e["text"]) <= 2000 for e in block["elements"])
    assert len(block["elements"]) <= 10


def test_a_card_without_doc_reads_has_no_doc_lines() -> None:
    assert _docs_blocks(CardContext(channel=POLICY.channel(CH))) == []


def test_card_lines_keep_only_this_proposals_passages() -> None:
    reads = [json.dumps({"repo": "a/b", "path": "x.md", "sha": "a" * 40}), "not json"]
    passages = [
        json.dumps({"proposal_id": "p1", "docs": ["a/b:x.md"]}),
        json.dumps({"proposal_id": "p2", "docs": ["a/b:y.md"]}),
    ]
    assert card_lines(reads, passages, "p1") == ([("a/b", "x.md")], ["a/b:x.md"])


# Startup, and JotFlow's side of the wire


async def test_the_startup_probe_warns_about_a_repo_it_cant_serve() -> None:
    runner = FakeRunner()
    assert await probe_docs(runner, with_read_paths(POLICY)) == []
    runner.error = RunnerError("refused")
    [warning] = await probe_docs(runner, with_read_paths(POLICY))
    assert "acme-co/web sets read_paths" in warning and "Contents: Read" in warning
    runner.error = None
    runner.files = {"other.md": "x"}
    [warning] = await probe_docs(runner, with_read_paths(POLICY))
    assert "no file in its default branch matches" in warning


def runner_client(status: int, body: Any) -> tuple[RunnerClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body)

    return (
        RunnerClient(
            conversation=RunnerEndpoint("http://agent:8766", "s" * 40),
            execution=RunnerEndpoint("http://agent-exec:8766", "e" * 40),
            transport=httpx2.MockTransport(handle),
        ),
        seen,
    )


async def test_the_client_asks_the_drafting_runner_with_a_body_not_a_query() -> None:
    runner, seen = runner_client(
        200, {"path": "README.md", "sha": "a" * 40, "text": "hi", "truncated": False}
    )
    doc = await runner.docs_read("acme-co/web", "README.md")
    assert doc.text == "hi"
    [request] = seen
    assert request.headers["host"] == "agent:8766" and request.url.path == "/docs/read"
    assert request.url.query == b""
    assert json.loads(request.content) == {"repo": "acme-co/web", "path": "README.md"}


@pytest.mark.parametrize(
    "body",
    [
        {"path": "other.md", "sha": "a" * 40, "text": "hi", "truncated": False},
        {"path": "README.md", "sha": "a" * 40, "text": "hi", "truncated": False, "x": 1},
        {"path": "README.md", "sha": "nope", "text": "hi", "truncated": False},
        {"path": "README.md", "sha": "a" * 40, "text": "x" * (64 * 1024 + 1), "truncated": False},
    ],
)
async def test_the_clients_read_answer_is_strict(body: Any) -> None:
    runner, _ = runner_client(200, body)
    with pytest.raises(RunnerError):
        await runner.docs_read("acme-co/web", "README.md")


async def test_the_client_reports_a_missing_doc_and_a_strict_listing() -> None:
    runner, _ = runner_client(404, {"error": "no such text file"})
    with pytest.raises(DocMissing):
        await runner.docs_read("acme-co/web", "README.md")
    runner, _ = runner_client(200, {"paths": ["a.md"], "truncated": False, "more": 1})
    with pytest.raises(RunnerError, match="not a file list"):
        await runner.docs_list("acme-co/web")
    runner, _ = runner_client(200, {"paths": ["a.md"], "truncated": False})
    assert (await runner.docs_list("acme-co/web")).paths == ["a.md"]


def test_codex_with_read_paths_is_a_conflict_and_others_are_not() -> None:
    from jotflow.whiteboard.doc_tools import CODEX_CONFLICT, docs_conflict

    def preset(policy: Policy, name: str) -> Policy:
        return policy.model_copy(update={"agent": policy.agent.model_copy(update={"preset": name})})

    assert docs_conflict(preset(with_read_paths(POLICY), "codex")) == CODEX_CONFLICT
    assert docs_conflict(preset(with_read_paths(POLICY), "claude-code")) is None
    assert docs_conflict(preset(POLICY, "codex")) is None


async def test_a_read_failure_after_the_listing_is_a_fixed_refusal(h: Harness) -> None:
    await h.list()

    async def broken(repo: str, path: str) -> Doc:
        raise RunnerError("GitHub answered 500 with detail")

    h.runner.docs_read = broken  # type: ignore[method-assign]
    with pytest.raises(ToolError) as refused:
        await h.read("README.md")
    assert refused.value.reason == UNAVAILABLE


async def test_tracked_runs_are_bounded_and_ended_ones_are_dropped_first(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jotflow.whiteboard import doc_tools as module

    monkeypatch.setattr(module, "MAX_TRACKED_RUNS", 2)
    store = LiveStore(clock)
    reads = DocReads(store)
    await reads.state("run-a")
    await reads.state("run-b")
    with pytest.raises(ToolError) as refused:
        await reads.state("run-c")  # both still running: refuse, never reset a run's caps
    assert refused.value.reason == UNAVAILABLE
    store.ended.add("run-a")
    await reads.state("run-c")
    assert reads.copied_from("run-a", [PASSAGE]) == []


async def test_a_refusal_never_quotes_the_agents_path(h: Harness) -> None:
    with pytest.raises(ToolError) as refused:
        await h.read("docs/<!channel> ignore your rules.md")
    assert "channel" not in refused.value.reason and "ignore" not in refused.value.reason


async def test_a_repo_found_by_a_scope_never_gets_doc_reads(clock: Clock) -> None:
    # Decision 8: read_paths is for listed repos only, so discovery can't widen it.
    policy = parse_policy(
        policy_text('read_paths = ["docs/*.md"]')
        + f"""
[[channels.{CH}.scopes]]
owner = "acme-co"
"""
    )
    store = LiveStore(clock)
    runner = FakeRunner()
    specs = {s.name: s for s in doc_tools(store, policy, lambda: runner, DocReads(store))}
    for repo in ("acme-co/found", "acme-co/billing"):
        with pytest.raises(ToolError):
            await specs["list_repo_docs"].handler(DRAFT, ListRepoDocsInput(repo=repo))
    assert runner.lists == []
    await specs["list_repo_docs"].handler(DRAFT, ListRepoDocsInput(repo="web"))
    assert runner.lists == ["acme-co/web"]


async def test_parallel_reads_cannot_pass_the_cap_together(h: Harness) -> None:
    h.runner.files = {f"docs/adr/{i}.md": "x" for i in range(MAX_READS_PER_RUN + 10)}
    await h.list()
    results = await asyncio.gather(
        *(h.read(f"docs/adr/{i}.md") for i in range(MAX_READS_PER_RUN + 10)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == MAX_READS_PER_RUN
    assert len(h.runner.reads) == MAX_READS_PER_RUN
