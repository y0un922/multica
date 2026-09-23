"""Ports implemented by Capability & Platform and Agent Runtime owners."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .json_types import JsonValue
from .schema import DataSchema, object_schema
from .tool_contracts import (ToolContext, ToolError, ToolResult, ToolStatus,
                             ErrorCategory, ToolRuntime, require_ok)


@dataclass(frozen=True)
class CapabilityInfo:
    id: str
    requires_approval: bool = False
    side_effect: bool = False
    # These are the provider's complete JSON Schemas.  Do not coerce them to
    # A's historical DataSchema subset: B is authoritative and may return
    # $defs/$ref/anyOf/title/constraints.
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class CapabilityRegistry(Protocol):
    def get(self, capability_id: str) -> CapabilityInfo | None: ...


# Internal compatibility name; the public invocation contract is ToolRuntime.
CapabilityRuntime = ToolRuntime


class AgentExecutor(Protocol):
    async def run(
        self, *, goal: str, context: dict[str, Any],
        capabilities: list[str], invoke: Callable[..., Awaitable[JsonValue]],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentTaskContract:
    """Optional richer agent input; invocation authority remains in invoke."""
    run_id: str
    node_id: str
    output_schema: DataSchema | None
    capability_specs: dict[str, CapabilityInfo]


@runtime_checkable
class ContractAgentExecutor(Protocol):
    async def run_with_contract(
        self, *, contract: AgentTaskContract, goal: str, context: dict[str, Any],
        capabilities: list[str], invoke: Callable[..., Awaitable[JsonValue]],
    ) -> dict[str, Any]: ...


EventSink = Callable[[dict[str, Any]], Awaitable[None]]
