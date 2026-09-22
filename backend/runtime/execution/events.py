"""Single-process DEMO event adapter, not a production event store or SSE server.

Inject the same instance into A and the fake B runtime. The lock covers both
sequence allocation and publication, so concurrent producers cannot reorder it.
Production must replace this with B's durable stream adapter.
"""
import asyncio
from copy import deepcopy
from typing import Any, AsyncContextManager, Callable
from uuid import uuid4

from .contracts import AgentEvent, EventBus, EventType
from .state import utc_now


class SequencedEventPublisher:
    """Bridge A's private emission port to B's agreed EventBus.publish method.

    sequence_scope is supplied by the composition root, shared by ALL producers.
    It must reserve a monotonic per-run sequence and hold publication ordering
    until the scope exits. No A-local sequence counter is created here. This
    wiring convention requires agreement with B; see the contract change request.
    """
    def __init__(self, event_bus: EventBus,
                 sequence_scope: Callable[[str], AsyncContextManager[int]]):
        self.event_bus = event_bus
        self.sequence_scope = sequence_scope

    async def emit(self, *, run_id: str, event_type: EventType,
                   payload: dict[str, Any]) -> AgentEvent:
        async with self.sequence_scope(run_id) as sequence:
            event = AgentEvent(id=str(uuid4()), run_id=run_id, sequence=sequence,
                               type=event_type, timestamp=utc_now(), payload=deepcopy(payload))
            await self.event_bus.publish(event.model_copy(deep=True))
            return event


class InMemoryEventBus:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._events: dict[str, list[AgentEvent]] = {}
        self._ids: set[str] = set()

    def _store(self, event: AgentEvent):
        events = self._events.setdefault(event.run_id, [])
        if event.id in self._ids:
            raise ValueError("duplicate event id")
        if events and event.sequence <= events[-1].sequence:
            raise ValueError("event sequence must be strictly increasing")
        events.append(event.model_copy(deep=True))
        self._ids.add(event.id)

    async def publish(self, event: AgentEvent) -> None:
        async with self._lock:
            self._store(event)

    async def emit(self, *, run_id: str, event_type: EventType,
                   payload: dict[str, Any]) -> AgentEvent:
        async with self._lock:
            events = self._events.get(run_id, [])
            event = AgentEvent(
                id=str(uuid4()), run_id=run_id,
                sequence=events[-1].sequence + 1 if events else 1,
                type=event_type, timestamp=utc_now(), payload=deepcopy(payload),
            )
            self._store(event)
            return event.model_copy(deep=True)

    def get_events(self, run_id: str, *, after_sequence: int = 0) -> list[AgentEvent]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        return [e.model_copy(deep=True) for e in self._events.get(run_id, [])
                if e.sequence > after_sequence]
