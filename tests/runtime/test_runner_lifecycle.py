import asyncio
import unittest
from pydantic import TypeAdapter
from runtime.execution import RunSnapshot

import json

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_typed_spec
from runtime.execution import (InMemoryRunStore, RunConflictError, WorkflowRunner)
from runtime.workflow import WorkflowSpec


def snapshot_dict(snapshot):
    return TypeAdapter(RunSnapshot).dump_python(snapshot, mode="json")

class RunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.store = InMemoryRunStore()
        self.runner = WorkflowRunner(registry=FakeRegistry(), runtime=self.runtime,
                                     agent=FakeAgent(), store=self.store)

    def create(self, run_id="pending"):
        return self.runner.create_run(load_typed_spec(), {"equipment_id": "TBM"}, run_id=run_id)

    async def test_pending_does_not_execute_tools(self):
        snapshot = self.create()
        self.assertEqual(snapshot.status, "running")
        self.assertEqual(snapshot.state["system"]["status"], "running")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.runner.get_events(snapshot.run_id), [])
        waiting = await self.runner.execute_run(snapshot.run_id)
        self.assertEqual(waiting.status, "waiting_confirmation")
        self.assertEqual(waiting.state["system"]["status"], "waiting_confirmation")
        self.assertLessEqual(snapshot.created_at, waiting.updated_at)
        json.dumps(snapshot_dict(waiting))

    async def test_pending_cannot_resume(self):
        self.create()
        with self.assertRaises(RunConflictError):
            await self.runner.resume_run("pending", confirmation_id="fake", decision="accepted")

    async def test_concurrent_execute_once(self):
        self.create()
        results = await asyncio.gather(self.runner.execute_run("pending"),
                                       self.runner.execute_run("pending"), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, RunConflictError) for r in results), 1)
        self.assertEqual([c[0] for c in self.runtime.calls].count("telemetry.read"), 1)

    async def test_live_snapshot_contains_committed_data(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        class Blocking(FakeRuntime):
            async def invoke(self, tool_name, arguments, context):
                capability_id, args = tool_name, arguments
                if capability_id == "history.query":
                    entered.set()
                    await release.wait()
                return await super().invoke(tool_name, arguments, context)
        self.runner.runtime = Blocking()
        task = asyncio.create_task(self.runner.start_run(
            load_typed_spec(), {"equipment_id": "TBM"}, run_id="live"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            snapshot = self.runner.get_run("live")
            self.assertEqual(snapshot.status, "running")
            self.assertEqual(snapshot.state["data"]["torque"], 4.12)
            self.assertEqual(snapshot.state["system"]["current_node"], "diagnose")
        finally:
            release.set()
            await task

    async def test_store_snapshots_are_detached(self):
        snapshot = self.create()
        snapshot.state["inputs"]["equipment_id"] = "hacked"
        stored = self.store.get("pending")
        self.assertEqual(stored.state["inputs"]["equipment_id"], "TBM")
        stored.state["inputs"].clear()
        self.assertEqual(self.runner.get_run("pending").state["inputs"]["equipment_id"], "TBM")

    async def test_stored_run_cannot_be_overwritten_by_new_runner(self):
        self.create()
        other = WorkflowRunner(registry=FakeRegistry(), runtime=FakeRuntime(),
                               agent=FakeAgent(), store=self.store)
        self.assertEqual(other.get_run("pending").status, "running")
        with self.assertRaises(RunConflictError):
            other.create_run(load_typed_spec(), {"equipment_id": "TBM"}, run_id="pending")
        with self.assertRaisesRegex(RunConflictError, "not loaded"):
            await other.execute_run("pending")

    async def test_old_approval_cannot_consume_next_approval(self):
        raw = load_typed_spec().model_dump()
        raw["nodes"].append({"id": "second_approval", "kind": "approval", "name": "Second",
                             "prompt": "Confirm again"})
        next(e for e in raw["edges"] if e["source"] == "approve" and e["when"])["target"] = "second_approval"
        raw["edges"].extend([
            {"source": "second_approval", "target": "apply", "when": True},
            {"source": "second_approval", "target": "escalate", "when": False},
        ])
        first = await self.runner.start_run(WorkflowSpec.model_validate(raw), {"equipment_id": "TBM"})
        second = await self.runner.resume_run(first.run_id, confirmation_id=first.confirmation.id, decision="accepted")
        self.assertEqual(second.status, "waiting_confirmation")
        self.assertNotEqual(first.confirmation.id, second.confirmation.id)
        replay = await self.runner.resume_run(first.run_id, confirmation_id=first.confirmation.id, decision="accepted")
        self.assertEqual(replay.confirmation.id, second.confirmation.id)
        final = await self.runner.resume_run(first.run_id, confirmation_id=second.confirmation.id, decision="accepted")
        self.assertEqual(final.status, "completed")
        self.assertEqual([c[0] for c in self.runtime.calls].count("control.apply"), 1)

    async def test_runs_have_isolated_checkpoint_and_approvals(self):
        first = await self.runner.start_run(load_typed_spec(), {"equipment_id": "one"})
        second = await self.runner.start_run(load_typed_spec(), {"equipment_id": "two"})
        await self.runner.resume_run(first.run_id, confirmation_id=first.confirmation.id, decision="rejected")
        self.assertEqual(self.runner.get_run(second.run_id).status, "waiting_confirmation")
        self.assertEqual(self.runner.get_run(second.run_id).state["inputs"]["equipment_id"], "two")
        self.assertNotEqual(first.confirmation.id, second.confirmation.id)

    async def test_cancel_after_success_preserves_committed_data(self):
        entered = asyncio.Event()
        class Blocking(FakeRuntime):
            async def invoke(self, tool_name, arguments, context):
                capability_id, args = tool_name, arguments
                if capability_id == "history.query":
                    entered.set()
                    await asyncio.Event().wait()
                return await super().invoke(tool_name, arguments, context)
        self.runner.runtime = Blocking()
        task = asyncio.create_task(self.runner.start_run(
            load_typed_spec(), {"equipment_id": "TBM"}, run_id="cancel"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        snapshot = self.runner.get_run("cancel")
        self.assertEqual(snapshot.status, "failed")
        self.assertEqual(snapshot.state["system"]["status"], "failed")
        self.assertEqual(snapshot.state["data"]["torque"], 4.12)
        self.assertEqual(snapshot.error["type"], "CancelledError")
