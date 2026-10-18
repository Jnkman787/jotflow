"""Cross-container run check for CI (#54, #55). Not a product command.

CI pipes this into the ``jotflow`` image with ``python -``, on the Compose
networks, with the real ``agent`` and ``agent-exec`` services running a stub
agent. It starts JotFlow's real MCP server and run supervisor with an
in-memory store and no Slack, queues one drafting run and one execution run,
and passes only if both end ``succeeded``.

Each stub agent calls ``tools/list`` with the token from its 0600 MCP config
and exits 0 only if it sees exactly its kind's tools, so success shows:

- JotFlow launched a run in each agent container, routed by kind
- the token reached the agent through its MCP config
- the agent reached JotFlow's MCP port across containers, and was admitted
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

from jotflow.config import Settings
from jotflow.models import IssueProposal, ProposalRecord, ProposalState, RunKind, RunStatus
from jotflow.orchestrator.launch import RunnerClient, RunSupervisor
from jotflow.orchestrator.runs import RunLifecycle
from jotflow.policy import Policy
from jotflow.store import MemoryStore, content_hash
from jotflow.whiteboard.read_tools import read_tools
from jotflow.whiteboard.server import McpServer, build_app
from jotflow.whiteboard.tokens import TokenRegistry
from jotflow.whiteboard.write_tools import write_tools

CHANNEL = "C0PROBE01"
THREAD = "1760781600.000100"
DEADLINE_SECONDS = 120


class NoPoster:
    async def post(self, channel_id: str, thread_ts: str, text: str) -> str:
        print(f"probe: JotFlow posted a notice: {text}", file=sys.stderr)
        return "1.0"


async def main() -> int:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    policy = Policy.model_validate(
        {
            "agent": {"preset": "probe"},
            "channels": {
                CHANNEL: {
                    "repos": [{"repo": "example/probe", "nickname": "probe", "description": "-"}]
                }
            },
        }
    )
    store = MemoryStore()
    registry = TokenRegistry()
    tools = (*read_tools(store, policy), *write_tools(store, policy, NoPoster()))
    app = build_app(
        port=settings.mcp_port,
        extra_hosts=settings.mcp_allowed_hosts,
        tokens=registry,
        tools=tools,
    )
    server = McpServer(app, settings.mcp_host, settings.mcp_port)

    proposal = IssueProposal(repo="example/probe", title="Probe", body="Probe.", confidence=0.9)
    record = await store.add_proposal(
        ProposalRecord(
            id="probe",
            channel_id=CHANNEL,
            thread_ts=THREAD,
            run_id="probe-draft",
            proposal=proposal,
            created_at=datetime.now(UTC),
        )
    )
    await store.transition(
        "probe",
        ProposalState.APPROVED,
        approved_by="U0PROBE",
        expected_hash=content_hash(record.proposal),
    )
    drafting = await store.enqueue_run(RunKind.DRAFTING, CHANNEL, THREAD)
    execution = await store.enqueue_run(RunKind.EXECUTION, CHANNEL, THREAD, proposal_id="probe")

    client = RunnerClient.from_settings(settings)
    supervisor = RunSupervisor(
        RunLifecycle(store, registry), store, client, NoPoster(), max_running=2
    )
    await server.start()
    try:
        loop = asyncio.get_running_loop()
        give_up = loop.time() + DEADLINE_SECONDS
        while loop.time() < give_up:
            await supervisor.tick()
            ended = [await store.get_run(r.id) for r in (drafting, execution)]
            if all(
                r is not None and r.status not in (RunStatus.QUEUED, RunStatus.RUNNING)
                for r in ended
            ):
                break
            await asyncio.sleep(0.5)
    finally:
        await supervisor.close()
        for run in (drafting, execution):
            registry.revoke(run.id)
        await server.stop()
        await client.aclose()

    ok = True
    for run in (drafting, execution):
        ended = await store.get_run(run.id)
        status = ended.status.value if ended else "missing"
        error = ended.error if ended else None
        print(f"probe: {run.kind.value} run ended {status}" + (f" ({error})" if error else ""))
        ok = ok and ended is not None and ended.status is RunStatus.SUCCEEDED
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-probe-not-used")
    os.environ.setdefault("JOTFLOW_SLACK_APP_TOKEN", "xapp-probe-not-used")
    sys.exit(asyncio.run(main()))
