"""Clean topic titles before they are stored.

A topic title is written by one agent run and read back by others, in other
threads and channels, so it is a stored path for prompt injection (#12). Every
title is cleaned on the way in: no Slack mentions or links, no URLs, no markup,
no control or invisible characters, and a short length limit.
"""

from __future__ import annotations

import html
import re
import unicodedata

MAX_TITLE_LENGTH = 80
# Unescape until the text stops changing, so any depth of &amp; nesting is caught;
# anything still escaped after this many passes loses its & as markup below.
MAX_UNESCAPE_PASSES = 8

# Slack's own markup: <@U123>, <#C123|name>, <!here>, <https://x|label>.
_SLACK_TOKEN = re.compile(r"<[^<>]*>")
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S*", re.IGNORECASE)
# Bare domains, which Slack turns into links: any host.tld followed by a path,
# and any host ending in a common top-level domain. "Node.js" survives: .js is
# not a domain Slack links.
_COMMON_TLDS = (
    "com|net|org|io|co|dev|app|ai|info|biz|xyz|me|us|uk|de|fr|eu|ca|au|in|jp|cn|ru|nl|"
    "es|it|se|ch|br|tv|ly|gg|sh|cc|so|to|link|site|online|top|club|example|test|local"
)
_HOST_WITH_PATH = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*", re.IGNORECASE)
_HOST_COMMON_TLD = re.compile(
    rf"\b(?:[a-z0-9-]+\.)+(?:{_COMMON_TLDS})\b(?:[:/?#]\S*)?", re.IGNORECASE
)
_MENTION = re.compile(r"(?<!\w)@\S*")
_MARKUP = re.compile(r"[*_~`<>|\[\]{}\\&]")
_SPACE = re.compile(r"\s+")


def _visible(text: str) -> str:
    """Drop control and format characters, such as zero-width and bidi marks."""
    return "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or unicodedata.category(ch)[0] != "C"
    )


def clean_title(text: str) -> str:
    """Return ``text`` as a safe, short topic title. Raises ValueError if nothing is left."""
    # Slack sends &lt; &gt; &amp; escaped; unescape first, until stable, so
    # "&lt;!channel&gt;" at any depth is caught as a Slack token.
    cleaned = text
    for _ in range(MAX_UNESCAPE_PASSES):
        unescaped = html.unescape(cleaned)
        if unescaped == cleaned:
            break
        cleaned = unescaped
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = _visible(cleaned)
    cleaned = _SLACK_TOKEN.sub(" ", cleaned)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _HOST_WITH_PATH.sub(" ", cleaned)
    cleaned = _HOST_COMMON_TLD.sub(" ", cleaned)
    cleaned = _MENTION.sub(" ", cleaned)
    cleaned = _MARKUP.sub(" ", cleaned)
    cleaned = _SPACE.sub(" ", cleaned).strip(" -:;,.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        # Cut at the last space that fits, unless that would lose more than half.
        head = cleaned[: MAX_TITLE_LENGTH + 1]
        cut = head.rsplit(" ", 1)[0] if " " in head else ""
        if len(cut) < MAX_TITLE_LENGTH // 2:
            cut = cleaned[:MAX_TITLE_LENGTH]
        cleaned = cut.rstrip()
    if not cleaned:
        raise ValueError(
            "a topic title needs some words once links, mentions, and markup are removed"
        )
    return cleaned
