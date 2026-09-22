"""Regression coverage for BACKEND_AB_INTERFACE.MD's public tool boundary."""
import unittest

from pydantic import ValidationError

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_spec
from runtime.execution.runner import WorkflowRunner
from runtime.workflow import compile_workflow, initial_state
from runtime.workflow.tool_contracts import (
    ErrorCategory, ToolContext, ToolInvocationError, ToolResult, ToolStatus, require_ok,
)


def failure(status):
    return ToolResult(status=status, error={
        "category": "timeout", "code": "TOOL_TIMEOUT", "message": "temporary outage",
        "retryable": status == ToolStatus.RETRYABLE_ERROR,
    })


class ToolContractTests(unittest.TestCase):
    def test_success_preserves_all_data_types(self):
        for data in [None, False, 0, [], {}, {"torque": 4.12}]:
            with self.subTest(data=data):
                self.assertEqual(require_ok(ToolResult(status="ok", data=data),
                                            tool_name="query"), data)

    def test_failure_keeps_machine_readable_fields(self):
        for status in [ToolStatus.RETRYABLE_ERROR, ToolStatus.FATAL_ERROR]:
            with self.subTest(status=status), self.assertRaises(ToolInvocationError) as cm:
                require_ok(failure(status), tool_name="query_tbm_status", node_id="read")
            exc = cm.exception
            self.assertEqual(exc.tool_name, "query_tbm_status")
            self.assertEqual(exc.node_id, "read")
            self.assertEqual(exc.status, status)
            self.assertEqual(exc.category, ErrorCategory.TIMEOUT)
            self.assertEqual(exc.code, "TOOL_TIMEOUT")

    def test_unknown_fields_and_legacy_results_rejected(self):
        for kwargs in [{"status": "ok", "value": {}},
                       {"status": "fatal_error", "error": "offline"},
                       {"status": "unknown"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                ToolResult(**kwargs)
        with self.assertRaises(ValidationError):
            ToolContext(run_id="run", trace_id="trace", workflow_state={})

    def test_metadata_is_not_shared(self):
        first, second = ToolResult(status="ok"), ToolResult(status="ok")
        first.metadata["latency"] = 10
        self.assertEqual(second.metadata, {})


class InvocationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_and_keyword_invocation(self):
        calls = []

        class Runtime(FakeRuntime):
            async def invoke(self, *, tool_name, arguments, context):
                calls.append((tool_name, arguments, context))
                return await super().invoke(tool_name, arguments, context)

        spec = load_spec()
        # A normal path completes without an approval interrupt.
        from langgraph.checkpoint.memory import InMemorySaver
        events = []

        async def sink(event):
            events.append(event)

        graph = compile_workflow(spec, registry=FakeRegistry(), runtime=Runtime(torque=3),
                                 agent=FakeAgent(), checkpointer=InMemorySaver(), event_sink=sink)
        state = initial_state(spec, {"equipment_id": "TBM-01"}, "run-context")
        state["system"].update(project_id="project", user_id="user", trace_id="trace")
        result = await graph.ainvoke(state, {"configurable": {"thread_id": "run-context"}})
        self.assertEqual(result["system"]["status"], "completed")
        self.assertTrue(calls)
        for _, _, context in calls:
            self.assertEqual(context.run_id, "run-context")
            self.assertEqual(context.trace_id, "trace")
            self.assertEqual(context.project_id, "project")
            self.assertEqual(context.user_id, "user")
            self.assertIsNotNone(context.node_id)
        self.assertFalse(any(e["type"].startswith("tool") for e in events))

    async def test_failure_is_not_silenced_or_automatically_retried(self):
        for status in [ToolStatus.RETRYABLE_ERROR, ToolStatus.FATAL_ERROR]:
            with self.subTest(status=status):
                calls = []

                class Runtime:
                    async def invoke(self, *, tool_name, arguments, context):
                        calls.append(tool_name)
                        return failure(status)

                runner = WorkflowRunner(registry=FakeRegistry(), runtime=Runtime(), agent=FakeAgent())
                snapshot = await runner.start_run(load_spec(), {"equipment_id": "TBM-01"})
                self.assertEqual(snapshot.status, "failed")
                self.assertEqual(len(calls), 1)
                self.assertIn("TOOL_TIMEOUT", snapshot.error["message"])
                self.assertEqual(snapshot.state["data"], {})
                self.assertFalse(any(e["type"].startswith("tool")
                                     for e in runner.get_events(snapshot.run_id)))
