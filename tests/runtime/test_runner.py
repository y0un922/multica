import asyncio
import unittest

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_spec
from runtime.execution import RunConflictError, RunNotFoundError, WorkflowRunner
from runtime.workflow.interfaces import ToolResult


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.runner = WorkflowRunner(registry=FakeRegistry(), runtime=self.runtime, agent=FakeAgent())

    async def start(self, run_id="run-1"):
        return await self.runner.start_run(load_spec(), {"equipment_id": "TBM-01"}, run_id=run_id)

    async def test_pause_resume(self):
        paused = await self.start()
        self.assertEqual(paused.status, "waiting_confirmation")
        self.assertEqual(paused.state["system"]["current_node"], "approve")
        final = await self.runner.resume_run(paused.run_id, confirmation_id=paused.confirmation.id, decision="accepted")
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.state["data"]["task_status"], "closed")
        events = self.runner.get_events(paused.run_id)
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(e["type"] == "confirmation.requested" for e in events), 1)
        self.assertEqual(sum(e["type"] == "run.started" for e in events), 1)
        self.assertEqual(self.runner.get_events(paused.run_id, after_sequence=events[-1]["sequence"]), [])

    async def test_reject(self):
        paused = await self.start()
        final = await self.runner.resume_run(paused.run_id, confirmation_id=paused.confirmation.id, decision="rejected")
        self.assertEqual(final.state["data"]["task_status"], "needs_attention")
        self.assertNotIn("control.apply", [c[0] for c in self.runtime.calls])

    async def test_stale_approval(self):
        await self.start()
        with self.assertRaises(RunConflictError):
            await self.runner.resume_run("run-1", confirmation_id="wrong", decision="accepted")
        self.assertEqual(self.runner.get_run("run-1").status, "waiting_confirmation")

    async def test_concurrent_resume_only_applies_once(self):
        paused = await self.start()
        async def resume():
            return await self.runner.resume_run("run-1", confirmation_id=paused.confirmation.id, decision="accepted")
        results = await asyncio.gather(resume(), resume(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, RunConflictError) for r in results), 0)
        self.assertEqual([c[0] for c in self.runtime.calls].count("control.apply"), 1)

    async def test_duplicate_run(self):
        await self.start()
        with self.assertRaises(RunConflictError):
            await self.start()

    async def test_snapshot_is_detached(self):
        snapshot = await self.start()
        snapshot.confirmation.context["changes"]["torque"] = 99
        snapshot.state["data"]["changes"]["torque"] = 99
        self.assertEqual(self.runner.get_run("run-1").state["data"]["changes"]["torque"], 3.5)
        events = self.runner.get_events("run-1")
        events.clear()
        self.assertTrue(self.runner.get_events("run-1"))

    async def test_failure_is_projected(self):
        class Broken:
            async def invoke(self, *args, **kwargs):
                return ToolResult(status="fatal_error", error={"category": "external_service_error", "code": "TEST_ERROR", "message": "offline", "retryable": False})
        self.runner.runtime = Broken()
        final = await self.start()
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.state["system"]["status"], "failed")
        self.assertIn("offline", final.error["message"])
        self.assertEqual(sum(e["type"] == "run.failed" for e in self.runner.get_events("run-1")), 1)
        with self.assertRaises(RunConflictError):
            await self.runner.resume_run("run-1", confirmation_id="anything", decision="accepted")

    async def test_normal_run(self):
        self.runtime.torque = 3
        final = await self.start()
        self.assertEqual(final.status, "completed")
        self.assertIsNone(final.confirmation)

    async def test_missing_run(self):
        with self.assertRaises(RunNotFoundError):
            self.runner.get_run("missing")

    async def test_invalid_resume_does_not_consume_approval(self):
        paused = await self.start()
        with self.assertRaises(ValueError):
            await self.runner.resume_run("run-1", confirmation_id=paused.confirmation.id, decision="false")
        self.assertEqual(self.runner.get_run("run-1").status, "waiting_confirmation")

    async def test_input_error_does_not_register_run(self):
        with self.assertRaises(ValueError):
            await self.runner.start_run(load_spec(), {}, run_id="bad")
        with self.assertRaises(RunNotFoundError):
            self.runner.get_run("bad")

    async def test_cancellation_recorded(self):
        entered = asyncio.Event()
        class Waiting:
            async def invoke(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
        self.runner.runtime = Waiting()
        task = asyncio.create_task(self.start())
        await entered.wait()
        self.assertEqual(self.runner.get_run("run-1").status, "running")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.runner.get_run("run-1").error["type"], "CancelledError")
