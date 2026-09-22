"""Contract integration tests: real A runner/compiler, explicitly fake B platform."""
import asyncio
import unittest
from contextlib import asynccontextmanager

from pydantic import TypeAdapter, ValidationError

from examples.ab_demo import build_workflow, demo_catalog, make_demo
from runtime.execution import RunConflictError, RunSnapshot
from runtime.execution.contracts import AgentEvent, Confirmation
from runtime.execution.events import InMemoryEventBus, SequencedEventPublisher
from runtime.workflow.catalog import CatalogRegistry
from runtime.workflow.tool_contracts import ToolResult


class ABIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, scenario="torque_anomaly"):
        runner, runtime, events = make_demo(scenario=scenario)
        run = await runner.start_run(build_workflow(), {"machine_id": "TBM-01"},
                                     project_id="project", user_id="user", trace_id="trace")
        return runner, runtime, events, run

    async def test_golden_demo_path(self):
        runner, runtime, events, paused = await self.start()
        self.assertEqual(paused.status, "waiting_confirmation")
        self.assertEqual(paused.state["system"]["status"], "waiting_confirmation")
        self.assertEqual(runtime.tasks, {})
        self.assertEqual([name for name, _, _ in runtime.calls], [
            "query_tbm_status", "query_sensor_history", "detect_parameter_anomaly",
            "query_geological_data", "diagnose_fault", "estimate_risk",
        ])
        self.assertEqual(events.confirmations[paused.confirmation.id].status, "pending")
        resolved = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "accepted")
        # B resolution does NOT resume LangGraph; the A consumer does so.
        self.assertEqual(runner.get_run(paused.run_id).status, "waiting_confirmation")
        final = await runner.handle_event(resolved)
        self.assertEqual(final.status, "completed")
        self.assertIsNone(final.confirmation)
        self.assertEqual(len(runtime.tasks), 1)
        task = runtime.tasks[final.state["data"]["task_id"]]
        self.assertEqual(task["status"], "open")  # Run completion != task closure.
        for _, _, context in runtime.calls:
            self.assertEqual((context.run_id, context.project_id, context.user_id, context.trace_id),
                             (paused.run_id, "project", "user", "trace"))
        stream = events.get_events(final.run_id)
        kinds = [e.type for e in stream]
        self.assertLess(kinds.index("confirmation.requested"), kinds.index("confirmation.resolved"))
        self.assertLess(kinds.index("confirmation.resolved"), kinds.index("task.created"))
        self.assertEqual(kinds[-1], "run.completed")
        self.assertEqual(kinds.count("run.started"), 1)
        self.assertEqual(kinds.count("run.completed"), 1)
        self.assertEqual(kinds.count("tool.started"), 7)
        self.assertEqual(kinds.count("tool.completed"), 7)
        self.assertEqual(sum(e.type == "node.started" and e.payload["node_id"] == "confirm"
                             for e in stream), 1)
        self.assertEqual([e.sequence for e in stream], list(range(1, len(stream) + 1)))
        self.assertEqual(len({e.id for e in stream}), len(stream))
        # Runner's A-only view preserves B-allocated sequence gaps, not its own seq.
        a_events = runner.get_events(final.run_id)
        self.assertTrue(all(not e["type"].startswith(("tool.", "task.")) for e in a_events))
        self.assertNotIn("confirmation.resolved", [e["type"] for e in a_events])
        self.assertNotEqual([e["sequence"] for e in a_events], list(range(1, len(a_events) + 1)))
        self.assertEqual(runner.get_events(final.run_id, after_sequence=stream[-1].sequence), [])
        for event in a_events:
            AgentEvent.model_validate(event)
        encoded = TypeAdapter(RunSnapshot).dump_python(final, mode="json")
        self.assertEqual(encoded["status"], "completed")

    async def test_normal_does_not_diagnose_confirm_or_create_task(self):
        runner, runtime, events, final = await self.start("normal")
        self.assertEqual(final.status, "completed")
        self.assertFalse(final.state["data"]["abnormal"])
        self.assertEqual([c[0] for c in runtime.calls], [
            "query_tbm_status", "query_sensor_history", "detect_parameter_anomaly"])
        self.assertEqual(runtime.tasks, {})
        self.assertEqual(events.confirmations, {})
        self.assertNotIn("confirmation.requested", [e.type for e in events.get_events(final.run_id)])

    async def test_rejected_ends_without_action(self):
        runner, runtime, events, paused = await self.start()
        event = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "rejected")
        final = await runner.handle_event(event)
        self.assertEqual(final.status, "completed")
        self.assertNotIn("create_task", [c[0] for c in runtime.calls])
        self.assertEqual(runtime.tasks, {})
        self.assertEqual(events.confirmations[paused.confirmation.id].status, "rejected")

    async def test_duplicate_delivery_is_idempotent(self):
        runner, runtime, events, paused = await self.start()
        event = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "accepted")
        results = await asyncio.gather(runner.handle_event(event), runner.handle_event(event))
        self.assertTrue(all(result.status == "completed" for result in results))
        self.assertEqual([c[0] for c in runtime.calls].count("create_task"), 1)
        self.assertEqual(len(runtime.tasks), 1)
        self.assertEqual(sum(e.type == "run.completed" for e in events.get_events(paused.run_id)), 1)
        with self.assertRaises(RunConflictError):
            await runner.resume_run(paused.run_id, confirmation_id=paused.confirmation.id, decision="rejected")

    async def test_invalid_confirmation_does_not_consume_checkpoint(self):
        runner, runtime, events, paused = await self.start()
        resolved = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "accepted")
        for field, value in [("run_id", "other"), ("id", "other"), ("node_id", "other"),
                             ("status", "pending"), ("resolved_at", None)]:
            invalid = resolved.model_copy(deep=True)
            invalid.payload[field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, RunConflictError)):
                await runner.handle_event(invalid)
            self.assertEqual(runner.get_run(paused.run_id).status, "waiting_confirmation")
            self.assertEqual(runtime.tasks, {})
        await runner.handle_event(resolved)
        self.assertEqual(len(runtime.tasks), 1)

    async def test_cross_run_confirmation_rejected(self):
        runner, runtime, events, first = await self.start()
        second = await runner.start_run(build_workflow(), {"machine_id": "TBM-02"})
        event = await events.resolve_confirmation(first.run_id, first.confirmation.id, "accepted")
        event.run_id = second.run_id
        event.payload["run_id"] = second.run_id
        with self.assertRaises(RunConflictError):
            await runner.handle_event(event)
        self.assertEqual(runner.get_run(first.run_id).status, "waiting_confirmation")
        self.assertEqual(runner.get_run(second.run_id).status, "waiting_confirmation")
        self.assertEqual(runtime.tasks, {})

    async def test_request_publication_observes_waiting_snapshot(self):
        runner, runtime, events = make_demo()
        original = events.emit
        observed = []

        async def emit(**kwargs):
            if kwargs["event_type"] == "confirmation.requested":
                snapshot = runner.get_run(kwargs["run_id"])
                observed.append(snapshot.status)
                self.assertEqual(snapshot.confirmation.id, kwargs["payload"]["id"])
                self.assertEqual(runtime.tasks, {})
            if kwargs["event_type"] == "run.completed":
                self.assertEqual(runner.get_run(kwargs["run_id"]).status, "completed")
            return await original(**kwargs)

        events.emit = emit
        paused = await runner.start_run(build_workflow(), {"machine_id": "TBM-01"})
        self.assertEqual(observed, ["waiting_confirmation"])
        resolved = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "accepted")
        await runner.handle_event(resolved)

    async def test_fast_result_is_queued_until_request_publication_finishes(self):
        runner, runtime, events = make_demo()
        original = events.emit
        consumers = []

        async def emit(**kwargs):
            event = await original(**kwargs)
            if kwargs["event_type"] == "confirmation.requested":
                resolved = await events.resolve_confirmation(
                    kwargs["run_id"], kwargs["payload"]["id"], "accepted")
                consumers.append(asyncio.create_task(runner.handle_event(resolved)))
                # Run lock is still held by start_run: consumer must wait, not
                # execute an action before request publication has returned.
                await asyncio.sleep(0)
                self.assertEqual(runtime.tasks, {})
            return event

        events.emit = emit
        await runner.start_run(build_workflow(), {"machine_id": "TBM-01"})
        final = await asyncio.wait_for(consumers[0], timeout=5)
        self.assertEqual(final.status, "completed")
        self.assertEqual(len(runtime.tasks), 1)
        stream = events.get_events(final.run_id)
        self.assertEqual([event.sequence for event in stream], list(range(1, len(stream) + 1)))

    async def test_action_failure_not_retried_on_event_redelivery(self):
        runner, runtime, events, paused = await self.start()
        original = runtime.invoke
        actions = []

        async def invoke(tool_name, arguments, context):
            if tool_name == "create_task":
                actions.append(tool_name)
                return ToolResult(status="retryable_error", error={
                    "category": "timeout", "code": "TOOL_TIMEOUT", "message": "ambiguous result", "retryable": True})
            return await original(tool_name, arguments, context)

        runtime.invoke = invoke
        resolved = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "accepted")
        failed = await runner.handle_event(resolved)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["code"], "TOOL_TIMEOUT")
        self.assertEqual(failed.error["category"], "timeout")
        self.assertEqual((await runner.handle_event(resolved)).status, "failed")
        self.assertEqual(actions, ["create_task"])
        self.assertEqual(sum(e.type == "run.failed" for e in events.get_events(paused.run_id)), 1)

    async def test_event_outage_fails_closed(self):
        runner, runtime, events = make_demo()

        async def broken(**kwargs):
            raise OSError("event store unavailable")

        events.emit = broken
        with self.assertRaises(OSError):
            await runner.start_run(build_workflow(), {"machine_id": "TBM-01"}, run_id="outage")
        self.assertEqual(runner.get_run("outage").status, "failed")
        self.assertEqual(runtime.calls, [])


class EventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_calls_public_event_bus_with_shared_sequence_scope(self):
        bus = InMemoryEventBus()
        lock = asyncio.Lock()
        next_sequence = {}

        @asynccontextmanager
        async def platform_scope(run_id):
            async with lock:
                next_sequence[run_id] = next_sequence.get(run_id, 0) + 1
                yield next_sequence[run_id]

        # Two adapters, one shared B scope/store. No A-specific counter.
        a = SequencedEventPublisher(bus, platform_scope)
        b = SequencedEventPublisher(bus, platform_scope)
        await asyncio.gather(*(publisher.emit(run_id="shared", event_type=kind, payload={})
                               for publisher, kind in [(a, "node.started"), (b, "tool.started")] * 10))
        stream = bus.get_events("shared")
        self.assertEqual([event.sequence for event in stream], list(range(1, 21)))
        self.assertEqual(sum(event.type == "tool.started" for event in stream), 10)

    async def test_concurrent_producers_share_ordered_sequence(self):
        events = InMemoryEventBus()
        await asyncio.gather(*(events.emit(run_id="one", event_type="node.started" if i % 2 else "tool.started",
                                           payload={"index": i}) for i in range(100)))
        first = events.get_events("one")
        self.assertEqual([e.sequence for e in first], list(range(1, 101)))
        first[0].payload["index"] = "mutated"
        self.assertNotEqual(events.get_events("one")[0].payload["index"], "mutated")
        other = await events.emit(run_id="two", event_type="run.started", payload={})
        self.assertEqual(other.sequence, 1)
        with self.assertRaises(ValueError):
            await events.publish(other)
        with self.assertRaises(ValidationError):
            AgentEvent(id="id", run_id="run", sequence=0, type="run.started", timestamp="now", payload={})


class CatalogTests(unittest.TestCase):
    def test_catalog_mapping_and_a_confirmation_policy(self):
        registry = CatalogRegistry(demo_catalog(), confirmation_tools={"create_task"})
        self.assertTrue(registry.get("create_task").requires_approval)
        self.assertTrue(registry.get("create_task").side_effect)
        self.assertFalse(registry.get("query_tbm_status").side_effect)
        self.assertIsNone(registry.get("invented_tool"))
        self.assertEqual(len(registry.search("query_tbm_status")), 1)

    def test_catalog_rejects_duplicate_unknown_policy_and_unsupported_schema(self):
        catalog = demo_catalog()
        with self.assertRaises(ValueError):
            CatalogRegistry(catalog + [catalog[0]])
        with self.assertRaises(ValueError):
            CatalogRegistry(catalog, confirmation_tools={"invented"})
        catalog[0]["input_schema"] = {"$ref": "https://untrusted.example/schema"}
        with self.assertRaises(ValueError):
            CatalogRegistry(catalog)
