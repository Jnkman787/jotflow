"""The fixed tool registry.

Every tool the server offers is a :class:`ToolSpec` in :data:`TOOLS`, written
in code. Names, descriptions, and input schemas never come from Slack, the
database, or any other runtime content, and the list cannot change while the
server runs, so an agent never sees a tool it was not built with (no tool
poisoning, no ``list_changed``). Input schemas derive from Pydantic models,
the same ones the rest of JotFlow validates with.

Each tool names the run kinds that may call it, per the token table in
docs/architecture.md. A run lists and calls only its own kind's tools, and a
tool outside its kind looks exactly like a tool that does not exist.

The registry is empty until the read tools (#14) and write tools (#15) land.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from jotflow.models import RunKind
from jotflow.whiteboard.tokens import Grant

# The MCP specification's tool-name rule.
TOOL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")

Handler = Callable[[Grant, Any], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    # Run kinds that may call this tool; see the token table in architecture.md.
    kinds: frozenset[RunKind] = frozenset()
    # The result's model, advertised as the tool's outputSchema when set.
    output_model: type[BaseModel] | None = None

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any] | None:
        if self.output_model is None:
            return None
        # Serialization mode: the schema of what model_dump(mode="json") sends.
        return self.output_model.model_json_schema(mode="serialization")


TOOLS: tuple[ToolSpec, ...] = ()


# The ledger row get_work writes for every drafting run it serves, saying whether there's
# anything to draft (#373). Every drafting write needs it to say there is: an allow-list,
# so a run that never called get_work, or was served a thread under JotFlow's own post,
# writes nothing.
PACKET_SERVED = "packet_served"
NOTHING_TO_DRAFT_REFUSAL = "this run's thread has nothing to draft, so nothing is written"
CALL_GET_WORK_FIRST = "call get_work first: a drafting run writes only after its packet"


class ToolError(Exception):
    """A refusal the model should see and act on, such as a cap or a missing topic.

    The server returns it as a tool result with ``isError`` set and the reason
    as its text, not as a protocol error. Reasons are written in code and never
    quote Slack text or the agent's arguments.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RegistryError(Exception):
    """The tool registry is malformed. A programming error, caught at startup."""


def checked_registry(tools: Sequence[ToolSpec]) -> dict[str, ToolSpec]:
    """Validate ``tools`` and index them by name."""
    by_name: dict[str, ToolSpec] = {}
    for tool in tools:
        if not TOOL_NAME.fullmatch(tool.name):
            raise RegistryError(f"tool name {tool.name!r} breaks the MCP naming rule")
        if tool.name in by_name:
            raise RegistryError(f"tool {tool.name!r} is registered twice")
        if not tool.description.strip():
            raise RegistryError(f"tool {tool.name!r} has no description")
        if not tool.kinds:
            raise RegistryError(f"tool {tool.name!r} names no run kind that may call it")
        by_name[tool.name] = tool
    return by_name
