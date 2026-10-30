"""Who a message names in plain words, as in "Assign to Sam" (#331).

A person is named when their full Slack display name, or the first word of their
real name, appears in the message as whole words, spelled exactly as Slack has
it, case included. Names come from Slack's ``users.info``, never from the agent.

Slack names are chosen by their owners and are often ordinary words, so (security,
#331):

- **Case-sensitive:** "Will" names a Will; "we will fix it" names nobody.
- **At least 3 letters,** and never a word on :data:`STOPLIST`, such as "May" or
  "Can". Such a person needs an @-mention.
- **Unique:** the caller refuses unless exactly one candidate matches, so a planted
  or unlucky name can cause a refusal, never a wrong assignee.

Mentions are stripped first, since an @-mention is ``named_owner``'s job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# A ledger row get_work writes, never a tool: the packet's people, by Slack id only.
PACKET_PEOPLE = "packet_people"
MIN_LETTERS = 3
# English function and pronoun words, and others often used as names. Compared without
# case, so "Will", "WILL" and "will" are all unmatchable.
STOPLIST = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "so",
        "as",
        "at",
        "by",
        "for",
        "in",
        "of",
        "on",
        "to",
        "up",
        "via",
        "with",
        "from",
        "into",
        "onto",
        "me",
        "my",
        "mine",
        "you",
        "your",
        "yours",
        "he",
        "him",
        "his",
        "she",
        "her",
        "hers",
        "it",
        "its",
        "we",
        "us",
        "our",
        "ours",
        "they",
        "them",
        "their",
        "theirs",
        "i",
        "this",
        "that",
        "these",
        "those",
        "who",
        "whom",
        "whose",
        "what",
        "which",
        "there",
        "here",
        "all",
        "any",
        "one",
        "ones",
        "some",
        "none",
        "each",
        "every",
        "both",
        "either",
        "neither",
        "other",
        "another",
        "can",
        "may",
        "will",
        "shall",
        "must",
        "might",
        "could",
        "would",
        "should",
        "do",
        "does",
        "did",
        "done",
        "be",
        "is",
        "are",
        "was",
        "were",
        "been",
        "being",
        "have",
        "has",
        "had",
        "not",
        "no",
        "yes",
        "ok",
        "okay",
        "hi",
        "hey",
        "thanks",
        "please",
        "just",
        "also",
        "only",
        "even",
        "still",
        "well",
        "now",
        "then",
        "soon",
        "later",
        "today",
        "new",
        "old",
        "good",
        "best",
        "art",
        "bill",
        "grant",
        "mark",
        "rose",
        "sky",
        "ray",
        "hope",
        "faith",
        "joy",
        "june",
        "april",
        "august",
    ]
)
_MENTION = re.compile(r"<[@!#][^>]*>")
_LETTERS = re.compile(r"[^\W\d_]")


@dataclass(frozen=True)
class Names:
    """One person's names in Slack."""

    display: str | None = None
    real: str | None = None


def _forms(names: Names) -> list[str]:
    """The ways a message may name them: the full display name, the real name's first word."""
    forms: list[str] = []
    if names.display and names.display.strip():
        forms.append(" ".join(names.display.split()))
    if names.real and names.real.split():
        forms.append(names.real.split()[0])
    return [form for form in dict.fromkeys(forms) if matchable(form)]


def matchable(form: str) -> bool:
    """Whether a name can be matched by text at all."""
    letters = len(_LETTERS.findall(form))
    return letters >= MIN_LETTERS and form.casefold() not in STOPLIST


def _appears(form: str, text: str) -> bool:
    words = r"\s+".join(re.escape(word) for word in form.split())
    return re.search(rf"(?<![\w@]){words}(?!\w)", text) is not None


def named(text: str, people: Mapping[str, Names]) -> dict[str, str]:
    """Each person ``text`` names, by Slack id, with the name it matched."""
    plain = _MENTION.sub(" ", text)
    found: dict[str, str] = {}
    for user_id, names in people.items():
        for form in _forms(names):
            if _appears(form, plain):
                found[user_id] = form
                break
    return found


def only(text: str, people: Mapping[str, Names]) -> tuple[str, str] | None:
    """The one person ``text`` names, and the name, or None if it names none or several."""
    found = named(text, people)
    if len(found) != 1:
        return None
    ((user_id, form),) = found.items()
    return user_id, form


def candidates(*groups: Iterable[str]) -> list[str]:
    """The people a message can name: the thread's and the owner hints, each once."""
    return list(dict.fromkeys(user for group in groups for user in group))
