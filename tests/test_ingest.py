import logging
from typing import Any

import pytest

from jotflow.orchestrator.ingest import (
    Accepted,
    BotIdentity,
    EventFilter,
    Ignored,
    log_decision,
)

CHANNEL = "C0TEST1"
BOT = BotIdentity(user_id="U0BOT", bot_id="B0BOT")
SECRET_TEXT = "the launch code is swordfish"


def message(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": CHANNEL,
        "user": "U0ALICE",
        "text": SECRET_TEXT,
        "ts": "1700000000.000100",
    }
    event.update(overrides)
    return event


def reaction(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "reaction_added",
        "user": "U0ALICE",
        "reaction": "bug",
        "item": {"type": "message", "channel": CHANNEL, "ts": "1700000000.000100"},
    }
    event.update(overrides)
    return event


@pytest.fixture
def events() -> EventFilter:
    return EventFilter([CHANNEL], BOT)


def test_message_in_configured_channel_is_accepted(events: EventFilter) -> None:
    decision = events.classify(message(thread_ts="1699999999.000001"), "Ev1")
    assert isinstance(decision, Accepted)
    assert decision.kind == "message"
    assert decision.ref.channel_id == CHANNEL
    assert decision.ref.ts == "1700000000.000100"
    assert decision.ref.thread_ts == "1699999999.000001"


@pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share", "me_message"])
def test_new_message_subtypes_are_accepted(events: EventFilter, subtype: str) -> None:
    assert isinstance(events.classify(message(subtype=subtype)), Accepted)


def test_unconfigured_channel_is_ignored(events: EventFilter) -> None:
    assert events.classify(message(channel="C0OTHER")) == Ignored("unconfigured channel", "C0OTHER")


def test_own_message_by_user_id_is_ignored(events: EventFilter) -> None:
    assert events.classify(message(user="U0BOT")) == Ignored("own message", CHANNEL)


def test_own_message_by_bot_id_is_ignored(events: EventFilter) -> None:
    event = message(subtype=None, bot_id="B0BOT")
    del event["user"]
    assert events.classify(event) == Ignored("own message", CHANNEL)


def test_own_thread_broadcast_is_ignored(events: EventFilter) -> None:
    assert events.classify(message(subtype="thread_broadcast", user="U0BOT")) == Ignored(
        "own message", CHANNEL
    )


def test_other_bots_are_treated_like_people(events: EventFilter) -> None:
    assert isinstance(events.classify(message(bot_id="B0OTHER")), Accepted)


def test_edit_is_ignored(events: EventFilter) -> None:
    edit = message(subtype="message_changed", message={"text": SECRET_TEXT, "user": "U0ALICE"})
    assert events.classify(edit) == Ignored("edit", CHANNEL)


def test_delete_is_ignored(events: EventFilter) -> None:
    assert events.classify(message(subtype="message_deleted")) == Ignored("delete", CHANNEL)


@pytest.mark.parametrize("subtype", ["channel_join", "channel_topic", "bot_message"])
def test_other_subtypes_are_ignored(events: EventFilter, subtype: str) -> None:
    decision = events.classify(message(subtype=subtype))
    assert isinstance(decision, Ignored)
    assert subtype in decision.reason


def test_malformed_message_is_ignored(events: EventFilter) -> None:
    assert isinstance(events.classify(message(ts=None)), Ignored)
    assert isinstance(events.classify(message(channel=42)), Ignored)


def test_duplicate_event_id_is_dropped(events: EventFilter) -> None:
    assert isinstance(events.classify(message(), "Ev1"), Accepted)
    assert events.classify(message(), "Ev1") == Ignored("duplicate delivery", CHANNEL)


def test_same_message_under_a_new_event_id_is_dropped(events: EventFilter) -> None:
    assert isinstance(events.classify(message(), "Ev1"), Accepted)
    assert events.classify(message(), "Ev2") == Ignored("duplicate delivery", CHANNEL)


def test_distinct_messages_are_not_duplicates(events: EventFilter) -> None:
    assert isinstance(events.classify(message(ts="1.1")), Accepted)
    assert isinstance(events.classify(message(ts="1.2")), Accepted)


def test_ignored_events_do_not_count_as_seen(events: EventFilter) -> None:
    assert isinstance(events.classify(message(channel="C0OTHER"), "Ev1"), Ignored)
    assert isinstance(events.classify(message(), "Ev2"), Accepted)


def test_duplicate_memory_is_bounded() -> None:
    events = EventFilter([CHANNEL], BOT, memory=2)
    for ts in ("1.1", "1.2", "1.3"):
        assert isinstance(events.classify(message(ts=ts)), Accepted)
    # The oldest key was forgotten; the newest is still remembered.
    assert isinstance(events.classify(message(ts="1.1")), Accepted)
    assert isinstance(events.classify(message(ts="1.3")), Ignored)


def test_reaction_is_accepted(events: EventFilter) -> None:
    decision = events.classify(reaction(), "Ev1")
    assert isinstance(decision, Accepted)
    assert decision.kind == "reaction"
    assert decision.reaction == "bug"
    assert decision.ref.ts == "1700000000.000100"


def test_own_reaction_is_ignored(events: EventFilter) -> None:
    assert events.classify(reaction(user="U0BOT")) == Ignored("own reaction", CHANNEL)


def test_reaction_in_unconfigured_channel_is_ignored(events: EventFilter) -> None:
    event = reaction(item={"type": "message", "channel": "C0OTHER", "ts": "1.1"})
    assert events.classify(event) == Ignored("unconfigured channel", "C0OTHER")


def test_reaction_on_a_file_is_ignored(events: EventFilter) -> None:
    assert isinstance(events.classify(reaction(item={"type": "file", "file": "F1"})), Ignored)


def test_duplicate_reaction_is_dropped(events: EventFilter) -> None:
    assert isinstance(events.classify(reaction(), "Ev1"), Accepted)
    assert isinstance(events.classify(reaction(), "Ev2"), Ignored)
    assert isinstance(events.classify(reaction(reaction="bulb"), "Ev3"), Accepted)


def test_unknown_event_type_is_ignored(events: EventFilter) -> None:
    assert isinstance(events.classify({"type": "member_joined_channel"}), Ignored)


def test_accepted_message_logs_one_line_without_text(
    events: EventFilter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="jotflow")
    log_decision(events.classify(message(), "Ev1"))
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert CHANNEL in record.getMessage()
    assert "1700000000.000100" in record.getMessage()
    assert SECRET_TEXT not in caplog.text


def test_ignored_event_logs_at_debug(events: EventFilter, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="jotflow")
    log_decision(events.classify(message(channel="C0OTHER")))
    [record] = caplog.records
    assert record.levelno == logging.DEBUG
    assert "unconfigured channel" in record.getMessage()
    assert SECRET_TEXT not in caplog.text


# --- reaction_removed (#138) ------------------------------------------------------------------


def test_a_removed_reaction_is_accepted_and_re_adding_it_is_not_a_duplicate() -> None:
    from jotflow.orchestrator.ingest import Accepted, BotIdentity, EventFilter

    events = EventFilter(["C0IDEAS01"], BotIdentity("U0BOT", "B0BOT"))
    item = {"type": "message", "channel": "C0IDEAS01", "ts": "1.1"}
    added = {"type": "reaction_added", "user": "U0ALEX", "reaction": "bulb", "item": item}
    gone = {**added, "type": "reaction_removed"}
    first = events.classify(added)
    assert isinstance(first, Accepted) and first.kind == "reaction"
    assert not isinstance(events.classify(added), Accepted)  # a redelivery
    removal = events.classify(gone)
    assert isinstance(removal, Accepted) and removal.kind == "reaction_removed"
    again = events.classify(added)
    assert isinstance(again, Accepted) and again.kind == "reaction"
    own = {**gone, "user": "U0BOT"}
    assert not isinstance(events.classify(own), Accepted)
