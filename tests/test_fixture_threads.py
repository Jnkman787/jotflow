"""The built-in fixture threads: loader validation, ingest, and fictional data only."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter
from jotflow.testing.threads import (
    MessageEvent,
    ProposeIssueCall,
    ReactionEvent,
    SavedThread,
    ThreadError,
    builtin_paths,
    builtin_thread,
    load_thread,
    parse_thread,
    scripted_outcome,
    skipped,
)

PATHS = list(builtin_paths())
REQUIRED = {
    "feature-request",
    "bug-report",
    "chatter",
    "same-topic-two-threads",
    "existing-topic",
    "keywords",
    "mentions",
    "mention-without-ownership",
}


def ids(paths: list[Path]) -> list[str]:
    return [p.stem for p in paths]


def test_every_required_fixture_ships() -> None:
    assert set(ids(PATHS)) >= REQUIRED


@pytest.mark.parametrize("path", PATHS, ids=ids(PATHS))
def test_fixture_loads_and_names_itself(path: Path) -> None:
    thread = load_thread(path)
    assert thread.name == path.stem
    assert thread.expected.summary
    assert path.read_text(encoding="utf-8").endswith("\n")


def _run_ingest(thread: SavedThread) -> tuple[list[str], dict[str, str]]:
    bot = thread.workspace.bot
    events = EventFilter(thread.policy.channels, BotIdentity(bot.user_id, bot.bot_id))
    accepted: list[str] = []
    ignored: dict[str, str] = {}
    for event in thread.events:
        payload = event.model_dump(exclude_none=True)
        decision = events.classify(payload)
        key = f"{event.item.ts}:{event.reaction}" if isinstance(event, ReactionEvent) else event.ts
        if isinstance(decision, Accepted):
            accepted.append(key)
        else:
            ignored[key] = decision.reason
    return accepted, ignored


@pytest.mark.parametrize("path", PATHS, ids=ids(PATHS))
def test_ingest_matches_the_expected_outcome(path: Path) -> None:
    thread = load_thread(path)
    accepted, ignored = _run_ingest(thread)
    assert accepted == list(thread.expected.accepted)
    assert ignored == thread.expected.ignored


@pytest.mark.parametrize("path", PATHS, ids=ids(PATHS))
def test_script_produces_the_expected_topics_and_proposals(path: Path) -> None:
    thread = load_thread(path)
    outcome = scripted_outcome(thread)
    attached = {ts for ts, word in thread.expected.keywords.items() if word == "re"}
    want = {t.topic: sorted(set(t.messages) - attached) for t in thread.expected.topics}
    assert {k: sorted(v) for k, v in outcome.topics.items()} == {k: v for k, v in want.items() if v}
    assert sorted(outcome.chatter) == sorted(thread.expected.chatter)
    assert outcome.proposals == list(thread.expected.proposals)
    assert outcome.asks == list(thread.expected.asks)


def _assignees(thread: SavedThread) -> list[str]:
    return [
        login
        for run in thread.agent
        for call in run.calls
        if isinstance(call, ProposeIssueCall)
        for login in call.arguments.assignees
    ]


def test_a_mention_alone_never_assigns() -> None:
    # The rule on #23: mentions mark people as involved, never as assignees.
    thread = builtin_thread("mention-without-ownership")
    assert "<@U0BOB>" in thread.messages[thread.agent[0].thread_ts].text
    assert _assignees(thread) == []
    assert list(thread.expected.asks) == [thread.agent[0].thread_ts]


def test_assignment_needs_someone_to_say_so() -> None:
    # Bob says "assign it to me" and is assigned; Alice is only mentioned.
    thread = builtin_thread("mentions")
    assert _assignees(thread) == ["acme-bob"]
    assert "assign it to me" in " ".join(m.text for m in thread.messages.values())


@pytest.mark.parametrize("path", PATHS, ids=ids(PATHS))
def test_keywords_match_the_message_text(path: Path) -> None:
    thread = load_thread(path)
    for ts, message in thread.messages.items():
        first = message.text.split(" ", 1)[0]
        word = first[:-1].lower() if first.endswith(":") else None
        expected = thread.expected.keywords.get(ts)
        if word in ("idea", "bug", "re", "skip"):
            assert expected == word, f"{ts} starts with {first} but expected says {expected}"
        else:
            assert expected is None, f"{ts} has no keyword but expected says {expected}"


@pytest.mark.parametrize("path", PATHS, ids=ids(PATHS))
def test_skipped_messages_reach_no_agent_run(path: Path) -> None:
    thread = load_thread(path)
    drafted = {run.thread_ts for run in thread.agent}
    linked = {
        call.arguments.message.ts
        for run in thread.agent
        for call in run.calls
        if call.tool == "link_message"
    }
    for ts in skipped(thread):
        assert ts not in drafted
        assert ts not in linked


def test_builtin_thread_by_name() -> None:
    assert builtin_thread("chatter").name == "chatter"
    with pytest.raises(ThreadError, match="no built-in thread named 'nope'; choose one of: "):
        builtin_thread("nope")


# Fictional data only. Allowlists, not deny-lists, so no real organisation is
# named in the repository even as a forbidden word.

# Slack ids are a type letter and a digit, then more characters. Fictional ids
# follow one shape: the letter, 0, capital letters, and an optional digit.
_SLACK_ID = re.compile(r"\b[BCDEGTUW][0-9][A-Z0-9]{3,}\b")
_FICTIONAL_ID = re.compile(r"[A-Z]0[A-Z]+[0-9]?")
_URL = re.compile(r"https?://([^/\s>|]+)(/[^\s>|]*)?")
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_TOKEN = re.compile(r"xox[abposre]-|xapp-|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_|-----BEGIN")
_ALLOWED_HOSTS = {"example.com", "example.org", "acme-co.example"}
FICTIONAL_OWNER = "acme-co"
FICTIONAL_TEAM = "Acme Co"


def fictional_problems(text: str) -> list[str]:
    """Everything in ``text`` that looks like real data rather than a fictional stand-in."""
    problems: list[str] = []
    for found in _SLACK_ID.findall(text):
        if not _FICTIONAL_ID.fullmatch(found):
            problems.append(f"real-looking Slack id {found}")
    for host, path in _URL.findall(text):
        if host == "github.com":
            if not (path or "/").startswith(f"/{FICTIONAL_OWNER}/"):
                problems.append(f"GitHub URL outside {FICTIONAL_OWNER}: {host}{path}")
        elif host not in _ALLOWED_HOSTS:
            problems.append(f"URL host {host} is not a fictional domain")
    for domain in _EMAIL.findall(text):
        if domain not in _ALLOWED_HOSTS:
            problems.append(f"email domain {domain} is not a fictional domain")
    if _TOKEN.search(text):
        problems.append("token-shaped string")
    return problems


def _repos_and_logins(data: Any) -> tuple[list[str], list[str]]:
    repos: list[str] = []
    logins: list[str] = []

    def walk(node: Any, key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str):
            if key == "repo":
                repos.append(node)
            elif key in ("github", "assignees", "add_assignees", "remove_assignees"):
                logins.append(node)

    walk(data)
    return repos, logins


@pytest.mark.parametrize("path", PATHS, ids=ids(PATHS))
def test_fixture_holds_only_fictional_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert fictional_problems(text) == []
    data = json.loads(text)
    assert data["workspace"]["team"] == FICTIONAL_TEAM
    repos, logins = _repos_and_logins(data)
    assert repos, "a fixture should name at least one repo"
    assert all(r.split("/", 1)[0] == FICTIONAL_OWNER for r in repos), repos
    assert all(login.startswith(f"{FICTIONAL_OWNER.split('-')[0]}-") for login in logins), logins


@pytest.mark.parametrize(
    ("sample", "problem"),
    [
        ("from U04ABCD123", "real-looking Slack id U04ABCD123"),
        ("in C07QX2LMNOP", "real-looking Slack id C07QX2LMNOP"),
        ("see https://github.com/someone-else/repo", "GitHub URL outside acme-co"),
        ("open https://intranet.corp.test/x", "URL host intranet.corp.test"),
        ("mail me at ops@corp.test", "email domain corp.test"),
        ("token xoxb-1234", "token-shaped string"),
    ],
)
def test_the_guard_catches_real_looking_data(sample: str, problem: str) -> None:
    assert any(problem in p for p in fictional_problems(sample))


def test_the_guard_accepts_fictional_data() -> None:
    sample = "U0ALICE in C0EXAMPLE1, https://github.com/acme-co/web/issues/42, a@example.com"
    assert fictional_problems(sample) == []


# Loader validation: each inconsistency is rejected with the field at fault.


def _base() -> dict[str, Any]:
    return json.loads((PATHS[0].parent / "feature-request.json").read_text(encoding="utf-8"))


def _problem(data: dict[str, Any]) -> str:
    with pytest.raises(ThreadError) as exc:
        parse_thread(json.dumps(data), source="case.json")
    return str(exc.value)


def test_unknown_field_is_rejected() -> None:
    data = _base()
    data["events"][0]["blocks"] = []
    assert "events.0.message.blocks: Extra inputs are not permitted" in _problem(data)


def test_wrong_format_is_rejected() -> None:
    data = _base()
    data["format"] = "jotflow-thread/2"
    assert "format" in _problem(data)


def test_reply_to_an_unknown_message() -> None:
    data = _base()
    data["events"][1]["thread_ts"] = "1.000000"
    assert "is not an earlier top-level message in the same channel" in _problem(data)


def test_two_different_messages_with_one_ts() -> None:
    data = _base()
    data["events"].append({**data["events"][0], "text": "different"})
    assert "is used by two different messages" in _problem(data)


def test_unlisted_user_and_mention() -> None:
    data = _base()
    data["events"][0]["user"] = "U0NOBODY"
    data["events"][1]["text"] = "<@U0GHOST> look"
    problem = _problem(data)
    assert "user U0NOBODY is not listed in users" in problem
    assert "mentions U0GHOST, who is not listed in users" in problem


def test_reaction_to_a_later_message() -> None:
    data = _base()
    data["events"].insert(
        0,
        {
            "type": "reaction_added",
            "user": "U0BOB",
            "reaction": "eyes",
            "item": {"type": "message", "channel": "C0EXAMPLE1", "ts": data["events"][0]["ts"]},
        },
    )
    assert "not an earlier message" in _problem(data)


def test_run_for_a_reply_is_rejected() -> None:
    data = _base()
    data["agent"][0]["thread_ts"] = data["events"][1]["ts"]
    assert "is not a top-level message in C0EXAMPLE1" in _problem(data)


def test_topic_key_rules() -> None:
    data = copy.deepcopy(_base())
    calls = data["agent"][0]["calls"]
    del calls[0]["topic_key"]
    problem = _problem(data)
    assert "a new_topic link needs a topic_key" in problem
    assert "topic key:csv-export is neither existing nor declared" in problem

    data = _base()
    data["agent"][0]["calls"][1]["topic_key"] = "extra"
    assert "only a new_topic link may set topic_key" in _problem(data)


def test_link_to_an_unknown_message() -> None:
    data = _base()
    data["agent"][0]["calls"][1]["arguments"]["message"]["ts"] = "9.000000"
    assert "message 9.000000 is not in events" in _problem(data)


def test_proposal_outside_policy() -> None:
    data = _base()
    proposal = data["agent"][0]["calls"][2]["arguments"]
    proposal["repo"] = "acme-co/secret"
    assert "repo 'acme-co/secret' is not one of channel C0EXAMPLE1's repos" in _problem(data)

    data = _base()
    data["agent"][0]["calls"][2]["arguments"]["labels"] = ["wontfix"]
    assert "labels not in acme-co/web's allowlist" in _problem(data)


def test_proposal_needs_a_named_topic_and_known_sources() -> None:
    data = _base()
    proposal = data["agent"][0]["calls"][2]["arguments"]
    del proposal["topic_id"]
    proposal["sources"].append({"channel_id": "C0EXAMPLE1", "ts": "9.000000"})
    problem = _problem(data)
    assert "name the topic with topic_id key:<name> or an existing id" in problem
    assert "source 9.000000 is not in events" in problem


def test_proposal_with_an_unknown_topic_or_assignee() -> None:
    data = _base()
    proposal = data["agent"][0]["calls"][2]["arguments"]
    proposal["topic_id"] = "topic-missing"
    proposal["assignees"] = ["someone"]
    problem = _problem(data)
    assert "topic topic-missing is neither existing nor declared" in problem
    assert "assignee someone is not a linked GitHub login in users" in problem


def test_expected_must_name_real_messages() -> None:
    data = _base()
    data["expected"]["chatter"] = ["9.000000"]
    data["expected"]["topics"][0]["messages"].append("8.000000")
    problem = _problem(data)
    assert "expected: 9.000000 is not a message in events" in problem
    assert "expected.topics: 8.000000 is not a message in events" in problem


def test_expected_must_place_each_message_once() -> None:
    data = _base()
    data["expected"]["chatter"] = [data["events"][0]["ts"]]
    problem = _problem(data)
    assert "must be in exactly one topic, chatter, or skipped thread; found 2" in problem
    assert "expected.chatter: lists" in problem


def test_expected_topics_and_proposals_must_match_the_script() -> None:
    data = _base()
    data["expected"]["topics"][0]["topic"] = "key:other"
    problem = _problem(data)
    assert "expected.topics: key:other lists" in problem
    assert "the script creates key:csv-export, which is not listed" in problem

    data = _base()
    data["expected"]["proposals"] = []
    assert "expected.proposals: does not match the script's propose calls" in _problem(data)

    data = _base()
    data["expected"]["asks"] = [data["events"][0]["ts"]]
    assert "expected.asks: lists" in _problem(data)


def test_invalid_json_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ThreadError, match=r"case\.json: not valid JSON"):
        parse_thread("{", source="case.json")
    with pytest.raises(ThreadError, match="cannot read file"):
        load_thread(tmp_path / "missing.json")


def test_message_helpers() -> None:
    top = MessageEvent(type="message", channel="C0EXAMPLE1", ts="1.000100")
    reply = MessageEvent(type="message", channel="C0EXAMPLE1", ts="2.000100", thread_ts="1.000100")
    own_root = MessageEvent(
        type="message", channel="C0EXAMPLE1", ts="1.000100", thread_ts="1.000100"
    )
    assert (top.is_reply, top.root) == (False, "1.000100")
    assert (reply.is_reply, reply.root) == (True, "1.000100")
    assert (own_root.is_reply, own_root.root) == (False, "1.000100")
