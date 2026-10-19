"""The content hash that approval locks (D7)."""

from __future__ import annotations

import hashlib
import json

from jotflow.models import IssueProposal, UpdateProposal

# Optional fields added after #23; see canonical_json.
LATER_FIELDS = ("hint_overruled",)


def canonical_json(proposal: IssueProposal | UpdateProposal) -> str:
    """The proposal as JSON with sorted keys and no spacing, so equal content hashes equally.

    Fields added after a proposal could be approved are left out while unset, so
    a proposal approved before they existed keeps its hash.
    """
    data = proposal.model_dump(mode="json")
    for name in LATER_FIELDS:
        if data.get(name) is None:
            data.pop(name, None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(proposal: IssueProposal | UpdateProposal) -> str:
    """``sha256:`` and the hex digest of the proposal's canonical JSON."""
    digest = hashlib.sha256(canonical_json(proposal).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
