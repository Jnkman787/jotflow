"""Text an agent writes that a person then reads on a Slack card (#94, #23).

Labels and an overruled hint's reason reach an approver's eyes. Control
characters, line breaks, bidirectional overrides, and invisible format
characters could make that text read differently from what it is, so both are
checked here with one rule.
"""

from __future__ import annotations

import unicodedata

# Bidirectional overrides and isolates, which can make text on a card read
# differently from what it is.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_ZWJ = "\u200d"
_EMOJI_VARIATION = "\ufe0f"


def _is_emoji_part(char: str, *, before_joiner: bool) -> bool:
    # Emoji are symbols (So). Before a joiner there may also be a skin-tone
    # modifier (Sk) or the emoji variation selector, as in a flag or 👩🏽‍💻.
    if unicodedata.category(char) == "So":
        return True
    return before_joiner and (char == _EMOJI_VARIATION or unicodedata.category(char) == "Sk")


def _invisible_format_char(label: str) -> bool:
    """Whether ``label`` has a format character (Cf) that could hide text.

    Zero-width spaces and the like make "bug" and "b\u200bug" look identical on a
    card. The zero-width joiner is allowed only between two emoji parts, where
    multi-part emoji need it.
    """
    for index, char in enumerate(label):
        if unicodedata.category(char) != "Cf":
            continue
        between_emoji = (
            0 < index < len(label) - 1
            and _is_emoji_part(label[index - 1], before_joiner=True)
            and _is_emoji_part(label[index + 1], before_joiner=False)
        )
        if char == _ZWJ and between_emoji:
            continue
        return True
    return False


def unsafe_text_problem(text: str) -> str | None:
    """Why ``text`` is unsafe to show as one line on a card, or None if it is safe."""
    if text != text.strip():
        return "must not start or end with a space"
    for char in text:
        if unicodedata.category(char) in {"Cc", "Zl", "Zp"} or char in _BIDI_CONTROLS:
            return "must not contain control characters or line breaks"
    if _invisible_format_char(text):
        return "must not contain invisible characters such as zero-width spaces"
    return None
