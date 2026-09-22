"""Ports implemented by Capability & Platform and Agent Runtime owners."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from .schema import DataSchema, object_schema
from .tool_contracts import (ToolContext, ToolError, ToolResult, ToolStatus,
                             ErrorCategory, ToolRuntime, require_ok)


@dataclass(frozen=True)
class CapabilityInfo:
    id: str
    requires_approval: bool = False
    side_effect: bool = False
    input_schema: DataSchema | None = None
    output_schema: DataSchema | None = None

    def __post_init__(self):
        for name in ("input_schema", "output_schema"):
            schema = getattr(self, name)
            if schema is not None:
                schema = DataSchema.model_validate(schema)
                object_schema(schema, f"{self.id}.{name}")
                object.__setattr__(self, name, schema)


class CapabilityRegistry(Protocol):
    def get(self, capability_id: str) -> CapabilityInfo | None: ...


# Internal compatibility name; the public invocation contract is ToolRuntime.
CapabilityRuntime = ToolRuntime


class AgentExecutor(Protocol):
    async def run(
        self, *, goal: str, context: dict[str, Any],
        capabilities: list[str], invoke: Callable[..., Awaitable[dict[str, Any]]],
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
        capabilities: list[str], invoke: Callable[..., Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]: ...


EventSink = Callable[[dict[str, Any]], Awaitable[None]]
