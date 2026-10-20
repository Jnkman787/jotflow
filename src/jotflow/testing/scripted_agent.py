"""A stand-in agent CLI that plays a fixture's scripted calls over real MCP (#23).

``jotflow replay --agent scripted`` launches it through the real runner, like
any agent, so the whole harness (the runner, the run's private MCP config, the
token, the MCP server, and the tools) is exercised without a model. It reads
the server URL and token from the MCP config the runner wrote, calls
``get_work``, then makes run ``--run`` of the fixture's script.

    python -m jotflow.testing.scripted_agent --thread feature-request --run 0 <mcp.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from jotflow.testing.threads import (
    TOPIC_KEY_PREFIX,
    LinkMessageCall,
    ListRepoDocsCall,
    SavedThread,
    builtin_thread,
    load_thread,
)

EXIT_REFUSED = 3
# Where a failed run's error is left for replay to report.
ERROR_FILE = "scripted-agent-error.txt"


def _thread(name: str) -> SavedThread:
    path = Path(name)
    return load_thread(path) if path.suffix == ".json" else builtin_thread(name)


async def play(thread: SavedThread, index: int, config_path: Path) -> int:
    from jotflow.replay import ReplayError, scripted_call
    from jotflow.whiteboard.server import PINNED_VERSIONS

    server = json.loads(config_path.read_text("utf-8"))["mcpServers"]["jotflow"]
    run = thread.agent[index]
    async with httpx2.AsyncClient(headers=server["headers"], timeout=30) as http:
        transport = streamable_http_client(server["url"], http_client=http)
        async with Client(transport, mode=PINNED_VERSIONS[0]) as client:
            work = await client.call_tool("get_work", {})
            if work.is_error or work.structured_content is None:
                return EXIT_REFUSED
            conversation: dict[str, Any] = work.structured_content["conversation"]
            # Existing topics have fresh ids in the replayed store: find them by title.
            by_title = {t["title"]["text"]: t["id"] for t in conversation["topics"]["topics"]}
            topics = {t.id: by_title.get(t.title) for t in thread.existing_topics}
            # Topics earlier runs started, by the title their script gave them.
            for earlier in thread.agent[:index]:
                for call in earlier.calls:
                    if isinstance(call, LinkMessageCall) and call.topic_key and call.title:
                        topics[TOPIC_KEY_PREFIX + call.topic_key] = by_title.get(call.title)
            participants = [
                m["user_id"]
                for m in conversation["thread"]["messages"]
                if m.get("user_id") and not m.get("from_jotflow")
            ]

            def resolve(topic: str | None) -> str | None:
                if topic is None:
                    return None
                if topics.get(topic) is None:
                    raise ReplayError(f"the script names topic {topic} before it exists")
                return topics[topic]

            for call in run.calls:
                tool, arguments = scripted_call(call, resolve, sorted(set(participants)))
                result = await client.call_tool(tool, arguments)
                expected = getattr(call, "expect_error", None)
                if expected is not None:
                    reason = "".join(getattr(part, "text", "") for part in result.content)
                    if result.is_error and expected in reason:
                        continue  # refused as the script expects (#216)
                    return EXIT_REFUSED
                if result.is_error:
                    return EXIT_REFUSED
                if isinstance(call, ListRepoDocsCall) and call.expect_paths is not None:
                    content = result.structured_content or {}
                    if content.get("paths") != list(call.expect_paths):
                        return EXIT_REFUSED
                if isinstance(call, LinkMessageCall) and call.topic_key:
                    content = result.structured_content or {}
                    topics[TOPIC_KEY_PREFIX + call.topic_key] = content.get("topic_id")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripted-agent")
    parser.add_argument("--thread", required=True, help="a built-in fixture name or a path")
    parser.add_argument("--run", type=int, required=True, help="which scripted run to play")
    parser.add_argument("config", type=Path, help="the MCP config the runner wrote")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(play(_thread(args.thread), args.run, args.config))
    except Exception as exc:
        # The runner discards an agent's output, so leave the error's type and
        # message next to the run directory, where replay reports it (#184).
        note = args.config.parent.parent / ERROR_FILE
        note.write_text(f"run {args.run}: {type(exc).__name__}: {exc}"[:500], encoding="utf-8")
        raise


if __name__ == "__main__":  # pragma: no cover - started by the runner
    sys.exit(main())
