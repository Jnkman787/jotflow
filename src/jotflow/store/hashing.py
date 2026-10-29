"""The content hash that approval locks (D7)."""

from __future__ import annotations

import hashlib
import json

from jotflow.models import IssueProposal, UpdateProposal

# Optional fields added after #23; see canonical_json.
LATER_FIELDS = ("hint_overruled", "related", "boards", "assignee_reasons")
# The same, inside an update's ``changes``.
LATER_CHANGE_FIELDS = ("assignee_reasons",)
# Advisory fields that are never written to GitHub, so never part of what approval locks.
UNHASHED_FIELDS = ("repo_visibility",)


def canonical_json(proposal: IssueProposal | UpdateProposal, *, hashed: bool = False) -> str:
    """The proposal as JSON with sorted keys and no spacing, so equal content hashes equally.

    Fields added after a proposal could be approved are left out while unset, so
    a proposal approved before they existed keeps its hash. For the hash
    (``hashed=True``) the agent's ``repo_visibility`` (#122) is left out too: it
    only adds a warning to the card, so it never changes the lock. The store
    keeps it.
    """
    data = proposal.model_dump(mode="json")
    for name in LATER_FIELDS:
        if data.get(name) in (None, []):
            data.pop(name, None)
    changes = data.get("changes")
    if isinstance(changes, dict):
        for name in LATER_CHANGE_FIELDS:
            if changes.get(name) in (None, []):
                changes.pop(name, None)
    # A related issue's likely_duplicate came later too (#337): left out while false.
    for related in data.get("related") or []:
        if related.get("likely_duplicate") is False:
            related.pop("likely_duplicate")
    if hashed:
        for name in UNHASHED_FIELDS:
            data.pop(name, None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(proposal: IssueProposal | UpdateProposal) -> str:
    """``sha256:`` and the hex digest of the proposal's canonical JSON, as hashed."""
    digest = hashlib.sha256(canonical_json(proposal, hashed=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
