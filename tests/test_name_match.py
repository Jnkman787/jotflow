"""Who a message names in plain words, for named_in_text (#331)."""

from __future__ import annotations

import pytest

from jotflow.name_match import Names, matchable, named, only

PEOPLE = {
    "U0SAM": Names(display="Sam", real="Samuel Lee"),
    "U0WILL": Names(display="Will", real="William Ng"),
    "U0ROBIN": Names(display="Robin Park", real=None),
    "U0KIM": Names(display=None, real="Kim Ito"),
}


@pytest.mark.parametrize(
    ("text", "who"),
    [
        ("Assign to Sam please", ("U0SAM", "Sam")),
        ("Sam can take it", ("U0SAM", "Sam")),
        ("Samuel should own it", ("U0SAM", "Samuel")),  # the real name's first word
        ("give it to Robin Park", ("U0ROBIN", "Robin Park")),  # a full display name
        ("give it to Robin", None),  # part of a display name isn't enough
        ("Kim owns exports", ("U0KIM", "Kim")),
        ("we will fix it", None),  # case-sensitive: "will" isn't Will
        ("Will can fix it", None),  # "Will" is on the stoplist
        ("assign to sam", None),  # case-sensitive
        ("Samantha will look", None),  # whole words only
        ("ask <@U0SAM> about it", None),  # a mention is named_owner's job
        ("@Sam take it", None),
        ("Sam and Kim can split it", None),  # two people: refused
    ],
)
def test_only_one_exact_name_counts(text: str, who: tuple[str, str] | None) -> None:
    assert only(text, PEOPLE) == who


@pytest.mark.parametrize("form", ["me", "It", "Al", "Bo", "May", "Grant", "WILL", "x1"])
def test_short_or_common_names_are_never_matchable(form: str) -> None:
    assert not matchable(form)


def test_a_person_with_only_unmatchable_names_needs_a_mention() -> None:
    people = {"U0MAY": Names(display="May", real="May Chen")}
    assert named("Assign to May", people) == {}
    assert matchable("Chen") and named("Chen", {"U0C": Names(real="Chen Li")}) == {"U0C": "Chen"}
