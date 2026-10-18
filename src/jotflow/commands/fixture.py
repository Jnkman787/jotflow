"""`jotflow execute-fixture`: queue an execution run for a hand-approved proposal (#55).

This is for a person checking an install end to end before approval in Slack
exists (M4). It reads a small JSON file describing one issue, checks it against
``jotflow.toml`` exactly as ``propose_issue`` would, stores it as approved, and
queues one execution run. A running ``jotflow run`` with agent runs on picks the
run up within seconds, and the agent writes a real GitHub issue.

Because it causes a real write, it needs ``--confirm`` and never runs in CI.
It prints ids only, never the proposal's content.

``--reconciling`` stores the proposal as if an earlier run had been
interrupted, so the run first looks for the proposal's hidden marker, its id.
With the ``id`` of a proposal that already produced an issue, in a fresh
database, this checks that a second run does not duplicate the issue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from jotflow.commands.run import SettingsError, load_settings
from jotflow.models import IssueProposal, ProposalRecord, ProposalState, RunKind
from jotflow.policy import Policy, PolicyError, PolicyViolation, UnknownChannelError, load_policy
from jotflow.store import SqliteStore, StoreError, content_hash

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_STORE = 4
MAX_FIXTURE_BYTES = 64 * 1024

ProposalId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
SlackTs = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,12}\.[0-9]{1,8}$")]
UserId = Annotated[str, StringConstraints(pattern=r"^[UW][A-Z0-9]{2,20}$")]


class Refusal(Exception):
    """The command will not run. The message says why."""


class Fixture(BaseModel):
    """One hand-approved issue."""

    model_config = ConfigDict(extra="forbid")

    id: ProposalId | None = Field(
        default=None, description="Reuse a proposal id, which is also the issue's hidden marker"
    )
    channel_id: str
    thread_ts: SlackTs = Field(description="A message in that channel; failure notices go there")
    repo: str
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=60_000)
    labels: list[str] = Field(default_factory=list)
    approved_by: UserId = Field(description="The Slack user id that stands as the approver")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", nargs="?", help="The fixture JSON file, or - for standard input")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required: the run writes a real issue with the agent's GitHub token",
    )
    parser.add_argument(
        "--reconciling",
        action="store_true",
        help="Store it as interrupted, so the run looks for its hidden marker first",
    )


def read_fixture(path: str) -> Fixture:
    raw = sys.stdin.buffer.read(MAX_FIXTURE_BYTES + 1) if path == "-" else _read(Path(path))
    if len(raw) > MAX_FIXTURE_BYTES:
        raise Refusal(f"the fixture is larger than {MAX_FIXTURE_BYTES} bytes")
    try:
        return Fixture.model_validate_json(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "(file)" for e in exc.errors()})
        raise Refusal(f"the fixture is invalid: {', '.join(fields)}") from None


def _read(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_FIXTURE_BYTES + 1)
    except OSError as exc:
        raise Refusal(f"could not read {path} ({type(exc).__name__})") from None


def checked_proposal(fixture: Fixture, policy: Policy) -> IssueProposal:
    """The proposal, with the repo and labels checked as ``propose_issue`` does."""
    try:
        repo = policy.repo(fixture.channel_id, fixture.repo)
        labels = repo.check_labels(fixture.labels)
    except UnknownChannelError:
        raise Refusal(f"channel {fixture.channel_id} is not in jotflow.toml") from None
    except PolicyViolation as refused:
        raise Refusal(str(refused)) from None
    return IssueProposal(
        repo=repo.repo, title=fixture.title, body=fixture.body, labels=labels, confidence=1.0
    )


async def queue(
    database_path: str, fixture: Fixture, proposal: IssueProposal, *, reconciling: bool
) -> tuple[str, str]:
    """Store the proposal as approved (or reconciling) and queue its run."""
    store = await SqliteStore.open(database_path)
    try:
        proposal_id = fixture.id or secrets.token_hex(8)
        if await store.get_proposal(proposal_id) is not None:
            raise Refusal(f"proposal {proposal_id} already exists in this database")
        record = await store.add_proposal(
            ProposalRecord(
                id=proposal_id,
                channel_id=fixture.channel_id,
                thread_ts=fixture.thread_ts,
                run_id="fixture",
                proposal=proposal,
                created_at=datetime.now(UTC),
            )
        )
        await store.transition(
            proposal_id,
            ProposalState.APPROVED,
            approved_by=fixture.approved_by,
            expected_hash=content_hash(record.proposal),
        )
        if reconciling:
            await store.transition(proposal_id, ProposalState.EXECUTING)
            await store.transition(proposal_id, ProposalState.RECONCILING)
        run = await store.enqueue_run(
            RunKind.EXECUTION, fixture.channel_id, fixture.thread_ts, proposal_id=proposal_id
        )
        return proposal_id, run.id
    finally:
        await store.close()


def run(args: argparse.Namespace) -> int:
    try:
        if os.environ.get("CI", "").strip().lower() not in {"", "0", "false", "no"}:
            raise Refusal("execute-fixture never runs in CI; it writes a real GitHub issue")
        if not args.confirm:
            raise Refusal(
                "the run writes a real issue with the agent's GitHub token; "
                "add --confirm to go ahead"
            )
        if args.path is None:
            raise Refusal("give the fixture file, or - to read it from standard input")
        settings = load_settings()
        policy = load_policy(settings.config_path)
        fixture = read_fixture(args.path)
        proposal = checked_proposal(fixture, policy)
        proposal_id, run_id = asyncio.run(
            queue(settings.database_path, fixture, proposal, reconciling=args.reconciling)
        )
    except Refusal as exc:
        print(f"jotflow execute-fixture: refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (SettingsError, PolicyError) as exc:
        print(f"jotflow execute-fixture: configuration problem in {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (StoreError, OSError, sqlite3.Error) as exc:
        print(
            f"jotflow execute-fixture: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_STORE
    state = "reconciling" if args.reconciling else "approved"
    print(f"proposal {proposal_id} is {state}; execution run {run_id} is queued")
    print(json.dumps({"proposal_id": proposal_id, "run_id": run_id}))
    return EXIT_OK
