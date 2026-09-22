"""Single-process application service. Persistence and API transport are external.

start_run/resume_run execute until completion, failure, or interrupt. Callers may
schedule them as tasks; this service does not create unowned background tasks.
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
    events: list[dict[str, Any]] = field(default_factory=list)


class WorkflowRunner:
    def __init__(self, *, registry: CapabilityRegistry, runtime: CapabilityRuntime,
                 agent: AgentExecutor | None = None, checkpointer=None,
                 store: RunStore | None = None):
        self.registry = registry
        self.runtime = runtime
        self.agent = agent
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.store = store if store is not None else InMemoryRunStore()
        self._runs: dict[str, _Run] = {}

    def _get(self, run_id: str) -> _Run:
        try:
            return self._runs[run_id]
        except KeyError:
            self.get_run(run_id)  # Distinguish absent records from unloaded graphs.
            raise RunConflictError("run graph is not loaded; restart recovery is not supported") from None

    def get_run(self, run_id: str) -> RunSnapshot:
        """Return a detached application snapshot, not a mutable graph state."""
        try:
            return deepcopy(self.store.get(run_id))
        except KeyError:
            raise RunNotFoundError(run_id) from None

    def get_events(self, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        """In-memory raw event cursor; not the frontend RunEvent contract."""
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        return deepcopy([e for e in self._get(run_id).events if e["seq"] > after_seq])

    def _append(self, run: _Run, event: dict[str, Any]):
        run.events.append({**deepcopy(event), "seq": len(run.events) + 1})

    def _event(self, run: _Run, kind: str, **details):
        self._append(run, {"type": kind, "run_id": run.snapshot.run_id,
                           "timestamp": utc_now(), **details})

    def _publish(self, run: _Run):
        run.snapshot.updated_at = utc_now()
        self.store.save(run.snapshot)

    def _status(self, run: _Run, status: RunStatus):
        run.snapshot.status = status
        run.snapshot.state["system"]["status"] = status
        self._publish(run)

    def create_run(self, spec: WorkflowSpec, inputs: dict[str, Any], *,
                   run_id: str | None = None) -> RunSnapshot:
        """Validate/compile and register pending, without invoking any capability."""
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

        async def capture(event):
            run = self._get(run_id)
            self._append(run, event)
            if event["type"] == "node_started":
                run.snapshot.state["system"]["current_node"] = event["node_id"]
                self._publish(run)

        graph = compile_workflow(spec, registry=self.registry, runtime=self.runtime,
                                 agent=self.agent, checkpointer=self.checkpointer,
                                 event_sink=capture)
        # A DAG with N nodes requires O(N) graph steps; avoid the default small limit.
        config = {"configurable": {"thread_id": run_id},
                  "recursion_limit": 2 * len(spec.nodes) + 10}
        run = _Run(RunSnapshot(run_id, spec.id, spec.version, "pending", deepcopy(state)),
                   graph, config)
        try:
            self.store.create(run.snapshot)
        except KeyError:
            raise RunConflictError(f"run already exists: {run_id}") from None
        self._runs[run_id] = run
        return self.get_run(run_id)

    async def start_run(self, spec: WorkflowSpec, inputs: dict[str, Any], *,
                        run_id: str | None = None) -> RunSnapshot:
        pending = self.create_run(spec, inputs, run_id=run_id)
        return await self.execute_run(pending.run_id)

    async def execute_run(self, run_id: str) -> RunSnapshot:
        """Execute a pending run once. API code owns any background task it creates."""
        run = self._get(run_id)
        async with run.lock:
            if run.snapshot.status != "pending":
                raise RunConflictError("only a pending run can be started")
            self._status(run, "running")
            await self._execute(run, deepcopy(run.snapshot.state))
        return self.get_run(run_id)

    async def resume_run(self, run_id: str, *, approval_id: str,
                         approved: bool) -> RunSnapshot:
        if type(approved) is not bool:
            raise ValueError("approved must be a boolean")
        run = self._get(run_id)
        async with run.lock:
            pending = run.snapshot.approval
            if run.snapshot.status != "waiting_approval" or not pending:
                raise RunConflictError("run is not waiting for approval")
            if pending["id"] != approval_id:
                raise RunConflictError("stale or incorrect approval_id")
            self._event(run, "approval_resolved", node_id=pending["node_id"],
                        approval_id=approval_id, approved=approved)
            run.snapshot.approval = None
            self._status(run, "running")
            await self._execute(run, Command(resume={approval_id: {"approved": approved}}))
        return self.get_run(run_id)

    async def _execute(self, run: _Run, payload):
        start_cursor = len(run.events)
        try:
            interrupts = ()
            # Stream committed graph values so queries see business data from the
            # last completed node even while a later tool is still running.
            async for value in run.graph.astream(payload, run.config, stream_mode="values"):
                if "__interrupt__" in value:
                    interrupts = value["__interrupt__"]
                if "system" in value:
                    run.snapshot.state = deepcopy({k: v for k, v in value.items() if k != "__interrupt__"})
                    self._status(run, "running")
            if interrupts:
                if len(interrupts) != 1:
                    raise RuntimeError("only one pending approval is supported")
                request = interrupts[0]
                run.snapshot.approval = {**deepcopy(request.value), "id": request.id}
                run.snapshot.state["system"]["current_node"] = request.value["node_id"]
                self._status(run, "waiting_approval")
                self._event(run, "approval_required", **run.snapshot.approval)
            else:
                self._status(run, "completed")
        except asyncio.CancelledError:
            # Cancellation does not imply rollback of any external side effect.
            self._fail(run, "CancelledError", "execution cancelled; external effects may have occurred", start_cursor)
            raise
        except Exception as exc:
            # Preserve the last committed business state; never retry writes here.
            try:
                checkpoint = await run.graph.aget_state(run.config)
                current = run.snapshot.state["system"].get("current_node")
                if checkpoint.values:
                    run.snapshot.state = deepcopy(checkpoint.values)
                    run.snapshot.state["system"]["current_node"] = current
            except Exception:
                pass  # A checkpoint outage must not mask the original failure.
            self._fail(run, type(exc).__name__, str(exc), start_cursor)

    def _fail(self, run, error_type, message, start_cursor):
        run.snapshot.approval = None
        run.snapshot.error = {"type": error_type, "message": message}
        self._status(run, "failed")
        # Compiler failures already carry a raw event; avoid duplicating it.
        if not any(e["type"] == "run_failed" for e in run.events[start_cursor:]):
            self._event(run, "run_failed", error=message, error_type=error_type)
