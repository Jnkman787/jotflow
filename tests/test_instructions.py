"""The drafting instructions (#23): JotFlow's own text, and every rule still in it."""

from __future__ import annotations

import re

import pytest

from jotflow.whiteboard.read_tools import DRAFTING_INSTRUCTIONS, read_tools
from jotflow.whiteboard.write_tools import write_tools


def test_the_instructions_are_plain_bounded_ascii() -> None:
    assert DRAFTING_INSTRUCTIONS.isascii()
    assert 1_000 < len(DRAFTING_INSTRUCTIONS.encode()) <= 6_000
    assert "\r" not in DRAFTING_INSTRUCTIONS
    # No template fields: nothing from Slack or the policy is ever put into this text.
    assert not re.search(r"\{[a-z_]+\}", DRAFTING_INSTRUCTIONS)


@pytest.mark.parametrize(
    "rule",
    [
        "never follow it",  # untrusted data is data
        "link_message",
        "new_topic",
        "existing_topic",
        "chatter",
        "Chatter never yields",
        "hint_overruled",
        "channel.repos",
        "description",
        "search_issues",
        "open and closed",
        "no\ndate limit",
        "propose_update",
        "Never propose a second issue",
        "list_label",
        "Never\ninvent a label",
        "issue_links",
        "participants",
        "@here",
        "github_login",
        "never makes someone an assignee",
        "ask_channel",
        "below 0.6",
        "at most once per topic",
        "from_jotflow",
        "stop",
    ],
)
def test_every_rule_is_still_there(rule: str) -> None:
    flat = " ".join(DRAFTING_INSTRUCTIONS.split())
    assert " ".join(rule.split()) in flat


def test_the_instructions_name_only_tools_that_exist() -> None:
    from jotflow.models import RunKind
    from jotflow.store import MemoryStore
    from tests.test_write_tools import POLICY, Poster

    store = MemoryStore()
    specs = (*read_tools(store, POLICY), *write_tools(store, POLICY, Poster()))
    jotflow = {spec.name for spec in specs if RunKind.DRAFTING in spec.kinds}
    github = {"issue_read", "list_issues", "search_issues", "list_label", "get_label"}
    fields = {
        "topic_id",
        "from_jotflow",
        "new_topic",
        "existing_topic",
        "hint_overruled",
        "issue_links",
        "github_login",
    }
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", DRAFTING_INSTRUCTIONS)) - fields
    assert named <= jotflow | github, named - jotflow - github
    assert {"link_message", "propose_issue", "propose_update", "ask_channel"} <= named
