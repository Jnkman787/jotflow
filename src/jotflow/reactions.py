"""The Slack reactions JotFlow gives meaning to, in one place (#56, #34).

Policy checks the approval reaction against these, so it never also means a
keyword or something JotFlow itself adds. This module imports nothing from
JotFlow, so the policy loader and the keyword parser can both use it.
"""

from __future__ import annotations

from typing import Literal

KeywordName = Literal["idea", "bug", "re", "skip"]

# Reactions that act like a keyword. re: has none: it needs an issue number.
KEYWORD_REACTIONS: dict[str, KeywordName] = {"no_entry_sign": "skip"}

# What JotFlow adds to a message to say a keyword was understood.
CONFIRM = "white_check_mark"

# Reactions JotFlow posts itself; a person's reaction must never be confused with one.
JOTFLOW_REACTIONS: frozenset[str] = frozenset({CONFIRM})

# The approval reaction's default: distinct from the confirmation, and not +1,
# which people add casually.
DEFAULT_APPROVAL = "heavy_check_mark"


def reserved() -> frozenset[str]:
    """Names the approval reaction may not take."""
    return frozenset(KEYWORD_REACTIONS) | JOTFLOW_REACTIONS
