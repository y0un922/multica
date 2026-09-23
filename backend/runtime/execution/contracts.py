"""A/B v0.1 event and confirmation envelopes; no platform implementation."""
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..workflow.json_types import JsonObject

RunStatus = Literal["running", "waiting_confirmation", "completed", "failed"]
ConfirmationStatus = Literal["pending", "accepted", "rejected"]
ConfirmationDecision = Literal["accepted", "rejected"]
EventType = Literal[
    "run.started", "node.started", "node.completed", "agent.thinking",
    "confirmation.requested", "run.completed", "run.failed",
    "tool.started", "tool.completed", "tool.failed", "task.created", "task.updated",
    "confirmation.resolved",
]


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    run_id: str
    sequence: int = Field(ge=1, strict=True)
    type: EventType
    timestamp: str
    payload: JsonObject


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    run_id: str
    node_id: str | None
    prompt: str
    context: dict[str, Any]
    status: ConfirmationStatus
    created_at: str
    resolved_at: str | None


class EventBus(Protocol):
    """Public interface from the A/B document. B owns persistence and transport."""
    async def publish(self, event: AgentEvent) -> None: ...


class EventPublisher(Protocol):
    """A-private injection port, NOT an addition to the agreed public EventBus.

    A platform adapter must allocate sequence and publish atomically in the same
    stream used by B. Runner never generates a second per-run sequence counter.
    See contracts/CONTRACT_CHANGE_REQUEST_AB_v0.1.md for integration details
    that still require agreement with B.
    """
    async def emit(self, *, run_id: str, event_type: EventType,
                   payload: JsonObject) -> AgentEvent: ...
