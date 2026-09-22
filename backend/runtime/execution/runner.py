"""A-owned single-process lifecycle service; platform persistence is injected.

Only this service resumes LangGraph. Confirmation API / event transport belong
elsewhere. A transport consumer delivers resolved confirmations to handle_event.
No production restart recovery or exactly-once external effects are claimed.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ..workflow import WorkflowSpec, compile_workflow, initial_state
from ..workflow.interfaces import AgentExecutor, CapabilityRegistry, CapabilityRuntime
from ..workflow.tool_contracts import ToolContext, ToolInvocationError
from .contracts import AgentEvent, Confirmation, ConfirmationDecision, EventPublisher
from .events import InMemoryEventBus
from .state import RunSnapshot, RunStatus, utc_now
from .store import InMemoryRunStore, RunStore


class RunNotFoundError(KeyError):
    pass


class RunConflictError(ValueError):
    pass


@dataclass
class _Run:
    snapshot: RunSnapshot
    graph: Any
    config: dict[str, Any]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started: bool = False  # Scheduling detail, deliberately not a public RunStatus.
    events: list[AgentEvent] = field(default_factory=list)
    interrupt_id: str | None = None  # Never expose LangGraph IDs as platform IDs.
    resolved: dict[str, ConfirmationDecision] = field(default_factory=dict)
    announced_confirmations: set[str] = field(default_factory=set)


class WorkflowRunner:
    def __init__(self, *, registry: CapabilityRegistry, runtime: CapabilityRuntime,
                 agent: AgentExecutor | None = None, checkpointer=None,
                 store: RunStore | None = None, event_publisher: EventPublisher | None = None):
        self.registry = registry
        self.runtime = runtime
        self.agent = agent
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.store = store if store is not None else InMemoryRunStore()
        # Convenience for isolated demos only. Production and integration tests
        # inject B's shared publisher; never give A and B separate allocators.
        self.event_publisher = event_publisher if event_publisher is not None else InMemoryEventBus()
        self._runs: dict[str, _Run] = {}

    def _get(self, run_id: str) -> _Run:
        try:
            return self._runs[run_id]
        except KeyError:
            self.get_run(run_id)
            raise RunConflictError("run graph is not loaded; restart recovery is not supported") from None

    def get_run(self, run_id: str) -> RunSnapshot:
        try:
            return deepcopy(self.store.get(run_id))
        except KeyError:
            raise RunNotFoundError(run_id) from None

    def get_events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        """A-produced events only; query B's event store for the entire A/B stream.

        sequence is assigned by the shared publisher and may have gaps here.
        """
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        return [e.model_dump(mode="json") for e in self._get(run_id).events
                if e.sequence > after_sequence]

    async def _event(self, run: _Run, kind, **payload):
        event = await self.event_publisher.emit(
            run_id=run.snapshot.run_id, event_type=kind, payload=deepcopy(payload))
        # Never renumber events locally; reject a broken integration adapter.
        if event.run_id != run.snapshot.run_id or event.type != kind:
            raise ValueError("event publisher returned a mismatched event")
        if run.events and event.sequence <= run.events[-1].sequence:
            raise ValueError("event publisher returned a non-increasing sequence")
        run.events.append(event.model_copy(deep=True))

    def _publish(self, run: _Run):
        run.snapshot.updated_at = utc_now()
        self.store.save(run.snapshot)

    def _status(self, run: _Run, status: RunStatus):
        run.snapshot.status = status
        run.snapshot.state["system"]["status"] = status
        self._publish(run)

    def create_run(self, spec: WorkflowSpec, inputs: dict[str, Any], *,
                   run_id: str | None = None, project_id: str | None = None,
                   user_id: str | None = None, trace_id: str | None = None) -> RunSnapshot:
        """Register an active Run without dispatching tools. execute_run starts it once.

        There is no public pending state in A/B v0.1; scheduling is private.
        """
        run_id = str(uuid4()) if run_id is None else run_id
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        try:
            self.store.get(run_id)
        except KeyError:
            pass
        else:
            raise RunConflictError(f"run already exists: {run_id}")
        spec = spec.model_copy(deep=True)
        state = initial_state(spec, inputs, run_id)
        context = ToolContext(run_id=run_id, project_id=project_id, user_id=user_id,
                              trace_id=run_id if trace_id is None else trace_id)
        state["system"].update(project_id=context.project_id, user_id=context.user_id,
                                trace_id=context.trace_id)

        async def capture(raw):
            run = self._get(run_id)
            kind = raw["type"]
            # Compiler events stay private; public lifecycle termination is emitted
            # below only AFTER committed snapshot state has been stored.
            mapping = {"run_started": "run.started", "node_started": "node.started",
                       "node_finished": "node.completed", "agent_thinking": "agent.thinking"}
            if kind not in mapping:
                return
            node_id = raw.get("node_id")
            if kind == "node_started":
                if node_id in run.announced_confirmations:
                    return  # interrupt replay must not start the same node twice.
                run.snapshot.state["system"]["current_node"] = node_id
                self._publish(run)
            payload = {k: deepcopy(v) for k, v in raw.items()
                       if k not in ("type", "run_id", "timestamp")}
            await self._event(run, mapping[kind], **payload)

        graph = compile_workflow(spec, registry=self.registry, runtime=self.runtime,
                                 agent=self.agent, checkpointer=self.checkpointer,
                                 event_sink=capture)
        config = {"configurable": {"thread_id": run_id},
                  "recursion_limit": 2 * len(spec.nodes) + 10}
        run = _Run(RunSnapshot(run_id, spec.id, spec.version, "running", deepcopy(state)), graph, config)
        try:
            self.store.create(run.snapshot)
        except KeyError:
            raise RunConflictError(f"run already exists: {run_id}") from None
        self._runs[run_id] = run
        return self.get_run(run_id)

    async def start_run(self, spec: WorkflowSpec, inputs: dict[str, Any], *,
                        run_id: str | None = None, project_id: str | None = None,
                        user_id: str | None = None, trace_id: str | None = None) -> RunSnapshot:
        created = self.create_run(spec, inputs, run_id=run_id, project_id=project_id,
                                  user_id=user_id, trace_id=trace_id)
        return await self.execute_run(created.run_id)

    async def execute_run(self, run_id: str) -> RunSnapshot:
        run = self._get(run_id)
        async with run.lock:
            if run.started or run.snapshot.status != "running":
                raise RunConflictError("run has already been started")
            run.started = True
            await self._execute(run, deepcopy(run.snapshot.state))
        return self.get_run(run_id)

    async def resume_run(self, run_id: str, *, confirmation_id: str,
                         decision: ConfirmationDecision) -> RunSnapshot:
        """A-owned resume entry; call only after an authenticated B resolution.

        Same decision redelivery is idempotent in-process; conflicting decisions
        and IDs cannot consume another pending confirmation. No resolved event is
        emitted here: confirmation.resolved belongs to B.
        """
        if not isinstance(decision, str) or decision not in ("accepted", "rejected"):
            raise ValueError("decision must be accepted or rejected")
        run = self._get(run_id)
        async with run.lock:
            if confirmation_id in run.resolved:
                if run.resolved[confirmation_id] != decision:
                    raise RunConflictError("confirmation already resolved with a different decision")
                return self.get_run(run_id)
            pending = run.snapshot.confirmation
            if run.snapshot.status != "waiting_confirmation" or pending is None:
                raise RunConflictError("run is not waiting for confirmation")
            if pending.id != confirmation_id:
                raise RunConflictError("stale or incorrect confirmation_id")
            if run.interrupt_id is None:
                raise RunConflictError("confirmation has no checkpoint interrupt")
            interrupt_id = run.interrupt_id
            run.resolved[confirmation_id] = decision
            run.snapshot.confirmation = None
            run.interrupt_id = None
            self._status(run, "running")
            await self._execute(run, Command(resume={interrupt_id: {"approved": decision == "accepted"}}))
        return self.get_run(run_id)

    async def handle_event(self, event: AgentEvent) -> RunSnapshot:
        """A-side consumer for trusted B events, not a public unauthenticated API.

        Payload adapter convention: resolved event carries the full Confirmation
        model. B must agree to this convention or translate its payload here.
        Queue delivery outside EventBus.publish: synchronous re-entry deadlocks
        while a workflow is publishing under its run lock.
        """
        if event.type != "confirmation.resolved":
            raise ValueError("expected confirmation.resolved")
        confirmation = Confirmation.model_validate(event.payload)
        if confirmation.run_id != event.run_id:
            raise RunConflictError("confirmation run_id does not match event")
        if confirmation.status not in ("accepted", "rejected") or confirmation.resolved_at is None:
            raise ValueError("confirmation is not resolved")
        run = self._get(event.run_id)
        pending = run.snapshot.confirmation
        if confirmation.id not in run.resolved:
            if pending is None or (pending.id, pending.node_id) != (confirmation.id, confirmation.node_id):
                raise RunConflictError("confirmation does not match pending request")
        return await self.resume_run(event.run_id, confirmation_id=confirmation.id,
                                     decision=confirmation.status)

    async def _execute(self, run: _Run, payload):
        try:
            interrupts = ()
            async for value in run.graph.astream(payload, run.config, stream_mode="values"):
                if "__interrupt__" in value:
                    interrupts = value["__interrupt__"]
                if "system" in value:
                    run.snapshot.state = deepcopy({k: v for k, v in value.items() if k != "__interrupt__"})
                    self._status(run, "running")
            if interrupts:
                if len(interrupts) != 1:
                    raise RuntimeError("only one pending confirmation is supported")
                request = interrupts[0]
                confirmation = Confirmation(
                    id=str(uuid4()), run_id=run.snapshot.run_id, node_id=request.value["node_id"],
                    prompt=request.value["prompt"], context=deepcopy(request.value["context"]),
                    status="pending", created_at=utc_now(), resolved_at=None,
                )
                run.interrupt_id = request.id
                run.snapshot.confirmation = confirmation
                run.snapshot.state["system"]["current_node"] = confirmation.node_id
                # Graph checkpoint and waiting state exist BEFORE publishing the
                # request, so a fast result cannot arrive before suspension.
                self._status(run, "waiting_confirmation")
                run.announced_confirmations.add(confirmation.node_id)
                await self._event(run, "node.started", node_id=confirmation.node_id,
                                  ui_stage_id=request.value.get("ui_stage_id"))
                await self._event(run, "confirmation.requested", **confirmation.model_dump(mode="json"))
            else:
                self._status(run, "completed")
                await self._event(run, "run.completed")
        except asyncio.CancelledError:
            await self._fail(run, "CancelledError", "execution cancelled; external effects may have occurred")
            raise
        except Exception as exc:
            try:
                checkpoint = await run.graph.aget_state(run.config)
                current = run.snapshot.state["system"].get("current_node")
                if checkpoint.values:
                    run.snapshot.state = deepcopy(checkpoint.values)
                    run.snapshot.state["system"]["current_node"] = current
            except Exception:
                pass  # Preserve the original failure if checkpoint reads fail.
            details = {}
            if isinstance(exc, ToolInvocationError):
                details = {"tool_name": exc.tool_name, "status": exc.status.value,
                           "category": exc.category.value if exc.category else "internal_error",
                           "code": exc.code or "MISSING_TOOL_ERROR"}
            await self._fail(run, type(exc).__name__, str(exc), **details)

    async def _fail(self, run, error_type, message, **details):
        run.snapshot.confirmation = None
        run.interrupt_id = None
        run.snapshot.error = {"type": error_type, "message": message, **details}
        self._status(run, "failed")
        await self._event(run, "run.failed", node_id=run.snapshot.state["system"].get("current_node"),
                          error=deepcopy(run.snapshot.error))
