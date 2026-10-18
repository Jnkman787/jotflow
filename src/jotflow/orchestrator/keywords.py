"""Intent keywords at the start of a message (D10): the strict prefix rule.

A keyword is the first word of a message followed by a colon, so "Skip button
is broken" is ordinary text and "skip: not an idea" is the ``skip`` keyword.
Case does not matter. This module owns that rule, so the work packet (#20) and
the keyword feature (#56), which adds typo tolerance, reactions, and ``re:``
resolution on top, can never disagree about what counts as a keyword.
"""

from __future__ import annotations

import re
from typing import Literal

Keyword = Literal["idea", "bug", "re", "skip"]
KEYWORDS: tuple[Keyword, ...] = ("idea", "bug", "re", "skip")

# Leading spaces, the word, a colon, then optional spaces. The colon is required.
_PREFIX = re.compile(r"\s*(idea|bug|re|skip):[ \t]*", re.IGNORECASE)


def split_keyword(text: str) -> tuple[Keyword | None, str]:
    """The message's keyword, if it starts with one, and the text without it."""
    match = _PREFIX.match(text)
    if match is None:
        return None, text
    word = match.group(1).lower()
    for keyword in KEYWORDS:
        if keyword == word:
            return keyword, text[match.end() :]
    return None, text  # pragma: no cover - the pattern only matches KEYWORDS
