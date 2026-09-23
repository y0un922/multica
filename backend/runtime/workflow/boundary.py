"""JSON-only adapters for the A/B process boundary.

The adapters deliberately serialize before crossing the provider boundary and
validate the returned envelope again. They can wrap an in-process B provider or
an HTTP/RPC client without importing B's Pydantic classes.
"""
from typing import Any, Protocol
from uuid import uuid4

from .json_types import JSON_OBJECT_ADAPTER, JsonObject
from .tool_contracts import ToolContext, ToolResult
from ..execution.contracts import AgentEvent, EventType
from ..execution.state import utc_now


class JsonToolInvoker(Protocol):
    async def invoke_tool_json(self, tool_name: str, arguments: JsonObject,
                               context: JsonObject) -> dict[str, Any]: ...


class JsonEventSink(Protocol):
    async def publish_event_json(self, event: JsonObject) -> dict[str, Any]: ...


class JsonBoundaryToolRuntime:
    """Adapt A's typed port to a provider that accepts JSON dictionaries."""
    def __init__(self, provider: JsonToolInvoker):
        self.provider = provider

    async def invoke(self, tool_name: str, arguments: JsonObject,
                     context: ToolContext) -> ToolResult:
        encoded_context = context.model_dump(mode="json")
        encoded_arguments = JSON_OBJECT_ADAPTER.validate_python(arguments)
        result = await self.provider.invoke_tool_json(tool_name, encoded_arguments, encoded_context)
        return ToolResult.model_validate(result)


class JsonEventPublisher:
    """Normalize B's authoritative published event at the A boundary."""
    def __init__(self, provider: JsonEventSink):
        self.provider = provider

    async def emit(self, *, run_id: str, event_type: EventType,
                   payload: JsonObject) -> AgentEvent:
        provisional = AgentEvent(
            id=str(uuid4()), run_id=run_id, sequence=1, type=event_type,
            timestamp=utc_now(), payload=payload,
        ).model_dump(mode="json")
        published = await self.provider.publish_event_json(provisional)
        return AgentEvent.model_validate(published)
