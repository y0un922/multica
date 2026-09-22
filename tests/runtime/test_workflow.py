import unittest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_spec
from runtime.workflow import (WorkflowSpec, WorkflowValidationError, compile_workflow,
                              initial_state, validate_workflow)
from runtime.workflow.interfaces import ToolResult


class ValidationTests(unittest.TestCase):
    def check_invalid(self, mutate, message):
        raw = load_spec().model_dump()
        mutate(raw)
        with self.assertRaisesRegex((WorkflowValidationError, ValidationError), message):
            validate_workflow(WorkflowSpec.model_validate(raw), FakeRegistry())

    def test_json_roundtrip(self):
        spec = load_spec()
        self.assertEqual(spec, WorkflowSpec.model_validate_json(spec.model_dump_json()))
        validate_workflow(spec, FakeRegistry())

    def test_duplicate(self):
        self.check_invalid(lambda d: d["nodes"].append(d["nodes"][0]), "duplicate")

    def test_unknown_edge(self):
        self.check_invalid(lambda d: d["edges"][0].update(target="missing"), "endpoint")

    def test_missing_branch(self):
        self.check_invalid(lambda d: d["edges"].pop(2), "true and false")

    def test_cycle(self):
        self.check_invalid(lambda d: d["edges"][-2].update(target="read_status"), "cycles")

    def test_unknown_capability(self):
        self.check_invalid(lambda d: d["nodes"][0].update(capability="invented.tool"), "unknown capability")

    def test_approval_bypass(self):
        self.check_invalid(lambda d: d["edges"][2].update(target="apply"), "approval")

    def test_agent_write_forbidden(self):
        self.check_invalid(lambda d: d["nodes"][2].update(capabilities=["control.apply"]), "read-only")

    def test_missing_binding(self):
        self.check_invalid(lambda d: d["nodes"][2]["inputs"]["torque"].update(path="$.data.missing"), "producer")

    def test_undeclared_input(self):
        self.check_invalid(lambda d: d.update(required_inputs=[]), "required_inputs")

    def test_reserved_node_name(self):
        self.check_invalid(lambda d: d["nodes"].append({
            "id": "data", "name": "reserved", "kind": "capability",
            "capability": "telemetry.read"}), "reserved")

    def test_overlapping_outputs(self):
        self.check_invalid(lambda d: d["nodes"][0].update(
            outputs={"a": "$.data.result", "b": "$.data.result.child"}), "overlapping")

    def test_unknown_fields(self):
        self.check_invalid(lambda d: d.update(python="exec(...)"), "Extra inputs")

    def test_branch_merge_requires_all_paths(self):
        self.check_invalid(lambda d: d["nodes"][-1]["inputs"].update(
            value={"type": "ref", "path": "$.data.after_torque"}), "producer")

    def test_unreachable(self):
        def mutate(d):
            d["nodes"].append({"id": "unused", "name": "unused", "kind": "capability", "capability": "telemetry.read"})
            d["edges"].append({"source": "unused", "target": "$end"})
        self.check_invalid(mutate, "unreachable")


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.spec = load_spec()
        self.runtime = FakeRuntime()
        self.events = []
        self.config = {"configurable": {"thread_id": "test"}}

    def compile(self, agent=None):
        async def sink(event):
            self.events.append(event)
        return compile_workflow(self.spec, registry=FakeRegistry(), runtime=self.runtime,
                                agent=agent or FakeAgent(), checkpointer=InMemorySaver(), event_sink=sink)

    async def start(self, graph):
        return await graph.ainvoke(initial_state(self.spec, {"equipment_id": "TBM-01"}), self.config)

    async def test_approval_resume_and_effect(self):
        graph = self.compile()
        paused = await self.start(graph)
        self.assertIn("__interrupt__", paused)
        self.assertNotIn("control.apply", [c[0] for c in self.runtime.calls])
        self.assertEqual((await graph.aget_state(self.config)).next, ("approve",))
        result = await graph.ainvoke(Command(resume={"approved": True}), self.config)
        self.assertEqual(result["data"]["task_status"], "closed")
        self.assertEqual(result["data"]["after_torque"], 3.5)
        self.assertEqual(result["system"]["status"], "completed")
        self.assertEqual([c[0] for c in self.runtime.calls].count("control.apply"), 1)
        self.assertEqual(self.events[0]["type"], "run_started")
        self.assertEqual(self.events[-1]["type"], "run_finished")

    async def test_rejected(self):
        graph = self.compile()
        await self.start(graph)
        result = await graph.ainvoke(Command(resume={"approved": False}), self.config)
        self.assertEqual(result["data"]["task_status"], "needs_attention")
        self.assertNotIn("control.apply", [c[0] for c in self.runtime.calls])

    async def test_no_improvement(self):
        class IneffectiveAgent(FakeAgent):
            async def run(self, **kwargs):
                return {"changes": {"torque": 4.5}}
        graph = self.compile(IneffectiveAgent())
        await self.start(graph)
        result = await graph.ainvoke(Command(resume={"approved": True}), self.config)
        self.assertEqual(result["data"]["task_status"], "needs_attention")

    async def test_normal_branch(self):
        self.runtime.torque = 3.0
        result = await self.start(self.compile())
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(len(self.runtime.calls), 1)

    async def test_agent_allowlist(self):
        class BadAgent:
            async def run(self, **kwargs):
                return await kwargs["invoke"]("control.apply", {})
        with self.assertRaises(PermissionError):
            await self.start(self.compile(BadAgent()))
        self.assertEqual(self.events[-1]["type"], "run_failed")

    async def test_tool_error(self):
        class BrokenRuntime:
            async def invoke(self, *args):
                return ToolResult("retryable_error", error="offline")
        self.runtime = BrokenRuntime()
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await self.start(self.compile())
        self.assertIn("tool_failed", [e["type"] for e in self.events])
        self.assertNotIn("run_finished", [e["type"] for e in self.events])

    async def test_missing_output(self):
        class BadAgent:
            async def run(self, **kwargs):
                return {}
        with self.assertRaisesRegex(ValueError, "missing output"):
            await self.start(self.compile(BadAgent()))

    def test_missing_input(self):
        with self.assertRaisesRegex(ValueError, "required inputs"):
            initial_state(self.spec, {})

    def test_requires_checkpointer(self):
        with self.assertRaisesRegex(ValueError, "checkpointer"):
            compile_workflow(self.spec, registry=FakeRegistry(), runtime=self.runtime, agent=FakeAgent())

    async def test_invalid_approval(self):
        graph = self.compile()
        await self.start(graph)
        with self.assertRaisesRegex(ValueError, "approved"):
            await graph.ainvoke(Command(resume={"approved": "false"}), self.config)


if __name__ == "__main__":
    unittest.main()
