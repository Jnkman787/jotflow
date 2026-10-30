"""Adding an applied issue to its approved Projects boards (#309).

The execution agent never touches a board: it has no projects tool. Once a
proposal is applied, JotFlow asks the execution runner's fixed add call for
each board the person approved, as part of telling the thread. A board is added
only if, at that moment:

- it is still in the channel's ``boards`` in jotflow.toml;
- it was in the approved draft, so in the hash a person locked;
- the issue is the one ``record_result`` recorded, in the approved repo;
- and the runner finds the proposal's marker line in that issue's body, and
  the issue's author is the write token's own login, so an execution agent
  can't record another issue in the repo and get it boarded, even one where
  someone pasted the marker. Either failure is a fixed refusal, never retried,
  and named in the notice.

Each attempt is recorded in the tool-call ledger, so a restart neither adds a
board twice nor forgets a failure. A board that keeps failing is named in the
thread notice, by its nickname, for a person to add by hand. GitHub's own board
titles never reach Slack.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from jotflow.board_catalog import BoardCatalog
from jotflow.models import IssueProposal, ProposalRecord, ProposalState
from jotflow.orchestrator.launch import NotThisProposal
from jotflow.policy import Policy, UnknownChannelError
from jotflow.store.base import Store, ToolCallRecord, utc_now

log = logging.getLogger("jotflow.boards")

TOOL = "board_add"
MAX_ATTEMPTS = 3
_ISSUE_URL = re.compile(r"https://github\.com/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>[0-9]+)")


class BoardClient(Protocol):
    """The execution runner's board call. Tests use a fake."""

    async def add_to_board(
        self, repo: str, issue_number: int, board: str, proposal_id: str
    ) -> None: ...


@dataclass
class BoardResults:
    """What happened to a proposal's boards, by the channel's nicknames."""

    added: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # the issue isn't the proposal's
    retry: bool = False  # a board may still succeed on a later try

    def notice(self) -> str:
        """A line for the thread notice, or nothing when there were no boards."""
        parts = []
        if self.added:
            parts.append("Added to " + ", ".join(self.added) + ".")
        if self.failed:
            parts.append("Couldn't add it to " + ", ".join(self.failed) + "; add it by hand.")
        if self.refused:
            parts.append(
                "Didn't add it to "
                + ", ".join(self.refused)
                + ": the recorded issue isn't one JotFlow filed for this proposal."
            )
        return " ".join(parts)


class BoardAdder:
    def __init__(
        self,
        store: Store,
        policy: Policy,
        client: BoardClient,
        *,
        clock: Callable[[], datetime] = utc_now,
        boards: BoardCatalog | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._client = client
        self._clock = clock
        # Listed boards, and found ones only while their listing is fresh (#309 PR 2).
        self._boards = boards if boards is not None else BoardCatalog.static(policy)

    async def add_all(self, record: ProposalRecord) -> BoardResults:
        """Add the applied issue to each approved board not added yet."""
        results = BoardResults()
        draft = record.proposal
        if record.state is not ProposalState.APPLIED or not isinstance(draft, IssueProposal):
            return results
        if not draft.boards:
            return results
        issue = _issue(record)
        try:
            self._policy.channel(record.channel_id)
        except UnknownChannelError:
            return results
        for proposed in draft.boards:
            listed = self._boards.board(record.channel_id, proposed.board)
            name = listed.nickname if listed is not None else proposed.board
            if listed is None and await self._was_added(record, proposed.board):
                results.added.append(name)  # added before it left the list or scope
                continue
            if listed is None and self._boards.awaiting(record.channel_id):
                results.retry = True  # its scope's listing hasn't loaded since the start
                continue
            if listed is None or issue is None:
                # No longer listed or in scope, or no issue recorded: never added.
                results.failed.append(name)
                continue
            outcome = await self._add(record, issue, listed.board)
            if outcome == "added":
                results.added.append(name)
            elif outcome == "failed":
                results.failed.append(name)
            elif outcome == "refused":
                results.refused.append(name)
            else:
                results.retry = True
        return results

    async def _was_added(self, record: ProposalRecord, board: str) -> bool:
        run_id = f"boards:{record.id}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            found = await self._store.find_tool_call(run_id, TOOL, f"{board}#{attempt}")
            if found is None:
                return False
            if json.loads(found).get("added"):
                return True
        return False

    async def _add(self, record: ProposalRecord, issue: tuple[str, int], board: str) -> str:
        """``added``, ``failed`` after the last attempt, ``refused`` when the issue
        isn't the proposal's, or ``again`` to retry later."""
        run_id = f"boards:{record.id}"
        attempts = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            found = await self._store.find_tool_call(run_id, TOOL, f"{board}#{attempt}")
            if found is None:
                break
            attempts = attempt
            recorded = json.loads(found)
            if recorded.get("added"):
                return "added"
            if recorded.get("refused"):
                return "refused"
        if attempts >= MAX_ATTEMPTS:
            return "failed"
        repo, number = issue
        try:
            await self._client.add_to_board(repo, number, board, record.id)
            added, refused = True, False
        except NotThisProposal:
            log.warning(
                "proposal %s: its recorded issue isn't one JotFlow filed for it, so it "
                "isn't added to board %s",
                record.id,
                board,
            )
            added, refused = False, True
        except Exception as exc:
            log.warning(
                "proposal %s: could not add its issue to board %s (%s)",
                record.id,
                board,
                type(exc).__name__,
            )
            added, refused = False, False
        await self._store.record_tool_call(
            ToolCallRecord(
                run_id=run_id,
                tool=TOOL,
                args_hash=f"{board}#{attempts + 1}",
                channel_id=record.channel_id,
                thread_ts=record.thread_ts,
                result_json=json.dumps({"added": added, "refused": refused}),
                called_at=self._clock(),
            )
        )
        if added:
            return "added"
        if refused:
            return "refused"
        return "failed" if attempts + 1 >= MAX_ATTEMPTS else "again"


def _issue(record: ProposalRecord) -> tuple[str, int] | None:
    """The issue ``record_result`` recorded, if it's in the approved repo."""
    url = record.result.issue_url if record.result else None
    match = _ISSUE_URL.fullmatch(url or "")
    if match is None or match["repo"].casefold() != record.proposal.repo.casefold():
        return None
    return record.proposal.repo, int(match["number"])
