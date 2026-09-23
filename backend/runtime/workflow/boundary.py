"""JSON-only adapters for the A/B process boundary.

The adapters deliberately serialize before crossing the provider boundary and
validate the returned envelope again. They can wrap an in-process B provider or
an HTTP/RPC client without importing B's Pydantic classes.
"""
from typing import Any, Protocol
from uuid import uuid4

from .json_types import JsonObject
from .tool_contracts import ToolContext, ToolResult
from ..execution.contracts import AgentEvent, EventType
from ..execution.state import utc_now


class JsonToolInvoker(Protocol):
    async def invoke(self, tool_name: str, arguments: JsonObject,
                     context: JsonObject) -> Any: ...


class JsonBoundaryToolRuntime:
    """Adapt A's typed port to a provider that accepts JSON dictionaries."""
    def __init__(self, provider: JsonToolInvoker):
        self.provider = provider

    async def invoke(self, tool_name: str, arguments: JsonObject,
                     context: ToolContext) -> ToolResult:
        encoded_context = context.model_dump(mode="json")
        encoded_arguments = ToolResult.model_validate({
            "status": "ok", "data": arguments,
        }).data
        result = await self.provider.invoke(tool_name, encoded_arguments, encoded_context)
        if isinstance(result, BaseException):
            raise result
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        return ToolResult.model_validate(result)


class JsonEventPublisher:
    """Normalize B's authoritative published event at the A boundary."""
    def __init__(self, provider: Any):
        self.provider = provider

    async def emit(self, *, run_id: str, event_type: EventType,
                   payload: JsonObject) -> AgentEvent:
        provisional = AgentEvent(
            id=str(uuid4()), run_id=run_id, sequence=1, type=event_type,
            timestamp=utc_now(), payload=payload,
        ).model_dump(mode="json")
        published = await self.provider.publish(provisional)
        if hasattr(published, "model_dump"):
            published = published.model_dump(mode="json")
        return AgentEvent.model_validate(published)
