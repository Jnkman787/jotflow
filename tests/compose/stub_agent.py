"""The stub agent CI's reach check runs in each agent container (#54, #55).

It reads the MCP config the runner wrote, calls ``tools/list`` with the run's
token, and exits 0 only if it sees exactly its kind's tools.
It is passed as ``python -c`` code, so it uses only the standard library.
"""

import json
import sys
import urllib.request

config_path, kind = sys.argv[1], sys.argv[2]
with open(config_path, encoding="utf-8") as handle:
    server = json.load(handle)["mcpServers"]["jotflow"]
meta = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "reach-stub", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
body = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
).encode()
request = urllib.request.Request(
    server["url"],
    data=body,
    method="POST",
    headers={
        **server["headers"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
    },
)
with urllib.request.urlopen(request, timeout=10) as reply:
    names = {tool["name"] for tool in json.loads(reply.read())["result"]["tools"]}
# Exactly the run kind's tools, from the token table in docs/architecture.md.
EXPECTED = {
    "drafting": {
        "get_work",
        "list_topics",
        "get_topic",
        "propose_issue",
        "propose_update",
        "revise_proposal",
        "link_message",
        "ask_channel",
        "point_to_help",
    },
    "execution": {"get_work", "record_result"},
}
if names != EXPECTED[kind]:
    sys.exit(f"{kind} run saw the wrong tools: {sorted(names)}")
