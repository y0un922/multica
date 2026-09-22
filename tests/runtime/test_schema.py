import unittest
from dataclasses import replace

from pydantic import ValidationError

from examples.demo import (FakeAgent, FakeRegistry, FakeRuntime, load_spec,
                           load_typed_spec, object_contract)
from runtime.execution import RunNotFoundError, WorkflowRunner
from runtime.workflow import (DataSchema, SchemaValueError, WorkflowSpec,
                              WorkflowValidationError, compile_workflow, initial_state,
                              validate_workflow)
from runtime.workflow.interfaces import CapabilityInfo, ToolResult
from runtime.workflow.schema import compatible, validate_value


class SchemaTests(unittest.TestCase):
    def test_bounded_dialect_rejects_unknown_keywords(self):
        for schema in ({"type": "string", "$ref": "https://untrusted/schema"},
                       {"type": "string", "format": "email"},
                       {"type": "array"},
                       {"type": "number", "required": []},
                       object_contract({}, ["missing"]),
                       {"type": "integer", "enum": [True]},
                       {"type": "string", "enum": []}):
            with self.subTest(schema=schema), self.assertRaises((ValidationError, ValueError)):
                DataSchema.model_validate(schema)

    def test_roundtrip(self):
        spec = load_typed_spec()
        self.assertEqual(spec, WorkflowSpec.model_validate_json(spec.model_dump_json()))
        validate_workflow(spec, FakeRegistry())

    def test_no_coercion(self):
        schema = DataSchema(type="number")
        for value in (True, "3.5", None):
            with self.subTest(value=value), self.assertRaises(SchemaValueError):
                validate_value(value, schema, "value")
        validate_value(3, schema, "value")

    def test_partial_state_defers_required_only(self):
        schema = DataSchema.model_validate(object_contract({"x": {"type": "number"}}, ["x"]))
        validate_value({}, schema, "data", partial=True)
        with self.assertRaises(SchemaValueError):
            validate_value({}, schema, "data")
        with self.assertRaises(SchemaValueError):
            validate_value({"x": "wrong"}, schema, "data", partial=True)
        with self.assertRaises(SchemaValueError):
            validate_value({"extra": 1}, schema, "data", partial=True)

    def test_array_nested_error_location(self):
        schema = DataSchema.model_validate({"type": "array", "items": object_contract(
            {"score": {"type": "number"}}, ["score"])})
        with self.assertRaisesRegex(SchemaValueError, r"outputs\[0\].score"):
            validate_value([{"score": "bad"}], schema, "outputs")

    def test_enum_and_integer_assignability(self):
        schema = DataSchema.model_validate
        self.assertTrue(compatible(schema({"type": "integer"}), schema({"type": "number"})))
        self.assertFalse(compatible(schema({"type": "number"}), schema({"type": "integer"})))
        self.assertTrue(compatible(schema({"type": "string", "enum": ["ok"]}),
                                   schema({"type": "string", "enum": ["ok", "bad"]})))
        self.assertFalse(compatible(schema({"type": "string"}), schema({"type": "string", "enum": ["ok"]})))
        self.assertFalse(compatible(schema({"type": "array", "items": {"type": "number"}}),
                                    schema({"type": "array", "items": {"type": "string"}})))

    def test_open_object_is_not_assignable_to_closed(self):
        source = DataSchema.model_validate({"type": "object"})
        target = DataSchema.model_validate(object_contract({}))
        self.assertFalse(compatible(source, target))

    def test_required_object_field_assignability(self):
        source = DataSchema.model_validate(object_contract({"x": {"type": "number"}}))
        target = DataSchema.model_validate(object_contract({"x": {"type": "number"}}, ["x"]))
        self.assertFalse(compatible(source, target))


class StaticSchemaTests(unittest.TestCase):
    def invalid(self, mutate, message, registry=None):
        raw = load_typed_spec().model_dump()
        mutate(raw)
        with self.assertRaisesRegex((WorkflowValidationError, ValidationError), message):
            validate_workflow(WorkflowSpec.model_validate(raw), registry or FakeRegistry())

    def test_v11_requires_schemas(self):
        self.invalid(lambda d: d.pop("input_schema"), "v1.1 requires")
        self.invalid(lambda d: d["nodes"][2].pop("output_schema"), "agent requires")

    def test_schema_roots_must_be_objects(self):
        self.invalid(lambda d: d.update(input_schema={"type": "string"}), "object type")

    def test_legacy_remains_supported(self):
        validate_workflow(load_spec(), FakeRegistry())

    def test_strict_registry_contracts_required(self):
        class Untyped(FakeRegistry):
            def get(self, capability_id):
                info = super().get(capability_id)
                return replace(info, input_schema=None, output_schema=None)
        self.invalid(lambda d: None, "requires input_schema", Untyped())

    def test_literal_tool_argument(self):
        self.invalid(lambda d: d["nodes"][0]["inputs"].update(
            equipment_id={"type": "literal", "value": 7}), "not of type 'string'")

    def test_input_to_capability_type_mismatch(self):
        self.invalid(lambda d: d["input_schema"]["properties"].update(
            equipment_id={"type": "integer"}), "incompatible type")

    def test_agent_context_type_mismatch(self):
        self.invalid(lambda d: d["nodes"][2]["input_schema"]["properties"].update(
            torque={"type": "string"}), "incompatible type")

    def test_capability_output_to_state_mismatch(self):
        self.invalid(lambda d: d["state_schema"]["properties"].update(
            torque={"type": "string"}), "incompatible output type")

    def test_agent_output_to_state_mismatch(self):
        self.invalid(lambda d: d["nodes"][2]["output_schema"]["properties"].update(
            changes={"type": "string"}), "incompatible output type")

    def test_unknown_output_field(self):
        self.invalid(lambda d: d["nodes"][0].update(outputs={"invented": "$.data.torque"}), "undeclared field")

    def test_optional_output_field(self):
        self.invalid(lambda d: d["nodes"][2]["output_schema"].update(required=[]), "optional")

    def test_undeclared_state_destination(self):
        self.invalid(lambda d: d["nodes"][0].update(outputs={"torque": "$.data.unknown"}), "undeclared field")

    def test_missing_capability_argument(self):
        self.invalid(lambda d: d["nodes"][4].update(inputs={}), "missing required")

    def test_extra_capability_argument(self):
        self.invalid(lambda d: d["nodes"][0]["inputs"].update(
            bogus={"type": "literal", "value": 1}), "undeclared field")

    def test_optional_nested_input_is_not_guaranteed(self):
        def mutate(d):
            d["input_schema"] = object_contract({"equipment": object_contract(
                {"id": {"type": "string"}})}, ["equipment"])
            d["nodes"][0]["inputs"]["equipment_id"]["path"] = "$.inputs.equipment.id"
        self.invalid(mutate, "optional")

    def test_predicate_mismatch(self):
        self.invalid(lambda d: d["nodes"][1]["predicate"]["right"].update(value="four"), "predicate types")

    def test_unknown_nested_source_field(self):
        self.invalid(lambda d: d["nodes"][3]["inputs"]["changes"].update(
            path="$.data.changes.missing"), "undeclared field")

    def test_branch_types_checked_without_state_schema(self):
        # Legacy graphs may omit state_schema, but typed producers must still be checked.
        raw = {
            "id": "branches", "name": "branches", "version": "1", "entrypoint": "choose",
            "nodes": [
                {"id": "choose", "name": "choose", "kind": "decision", "predicate": {
                    "left": {"type": "literal", "value": 1}, "op": "eq",
                    "right": {"type": "literal", "value": 1}}},
                {"id": "numeric", "name": "numeric", "kind": "capability", "capability": "telemetry.read",
                 "outputs": {"torque": "$.data.value"}},
                {"id": "text", "name": "text", "kind": "capability", "capability": "task.record",
                 "inputs": {"status": {"type": "literal", "value": "ok"}}, "outputs": {"status": "$.data.value"}},
                {"id": "consume", "name": "consume", "kind": "capability", "capability": "task.record",
                 "inputs": {"status": {"type": "ref", "path": "$.data.value"}}},
            ],
            "edges": [
                {"source": "choose", "target": "numeric", "when": True},
                {"source": "choose", "target": "text", "when": False},
                {"source": "numeric", "target": "consume"},
                {"source": "text", "target": "consume"},
                {"source": "consume", "target": "$end"},
            ],
        }
        with self.assertRaisesRegex(WorkflowValidationError, "incompatible type"):
            validate_workflow(WorkflowSpec.model_validate(raw), FakeRegistry())


class RuntimeSchemaTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, runtime=None, agent=None):
        return WorkflowRunner(registry=FakeRegistry(), runtime=runtime or FakeRuntime(), agent=agent or FakeAgent())

    async def test_typed_golden_path(self):
        runner = self.runner()
        paused = await runner.start_run(load_typed_spec(), {"equipment_id": "TBM-01"})
        self.assertEqual(paused.status, "waiting_confirmation")
        final = await runner.resume_run(paused.run_id, confirmation_id=paused.confirmation.id, decision="accepted")
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.state["data"]["task_status"], "closed")

    async def test_input_error_before_registration(self):
        runner = self.runner()
        for value in (7, True, None):
            with self.assertRaises(SchemaValueError):
                await runner.start_run(load_typed_spec(), {"equipment_id": value}, run_id="bad")
        with self.assertRaises(RunNotFoundError):
            runner.get_run("bad")

    async def test_extra_input_rejected(self):
        with self.assertRaises(SchemaValueError):
            initial_state(load_typed_spec(), {"equipment_id": "TBM", "extra": 1})

    async def test_tool_output_validated_before_state_write(self):
        class Bad(FakeRuntime):
            async def invoke(self, *args, **kwargs):
                return ToolResult(status="ok", data={"torque": "too high"})
        runner = self.runner(runtime=Bad())
        failed = await runner.start_run(load_typed_spec(), {"equipment_id": "TBM"})
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["type"], "SchemaValueError")
        self.assertEqual(failed.state["data"], {})
        self.assertIn("run.failed", [e["type"] for e in runner.get_events(failed.run_id)])

    async def test_agent_output_validated(self):
        class BadAgent:
            async def run(self, **kwargs):
                return {"changes": {"torque": "bad"}}
        runner = self.runner(agent=BadAgent())
        failed = await runner.start_run(load_typed_spec(), {"equipment_id": "TBM"})
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["type"], "SchemaValueError")
        self.assertIn("diagnose.outputs.changes.torque", failed.error["message"])
        self.assertNotIn("changes", failed.state["data"])

    async def test_agent_tool_input_validated_before_invocation(self):
        class BadAgent:
            async def run(self, **kwargs):
                return await kwargs["invoke"]("history.query", {"extra": "invalid"})
        runtime = FakeRuntime()
        runner = self.runner(runtime=runtime, agent=BadAgent())
        failed = await runner.start_run(load_typed_spec(), {"equipment_id": "TBM"})
        self.assertEqual(failed.status, "failed")
        self.assertNotIn("history.query", [c[0] for c in runtime.calls])

    async def test_final_required_state_is_checked(self):
        raw = load_typed_spec().model_dump()
        raw["state_schema"]["required"].append("applied")
        runner = self.runner(runtime=FakeRuntime(torque=3))
        failed = await runner.start_run(WorkflowSpec.model_validate(raw), {"equipment_id": "TBM"})
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error["type"], "SchemaValueError")
        self.assertNotIn("run.completed", [e["type"] for e in runner.get_events(failed.run_id)])

    async def test_direct_graph_input_bypass_still_validated(self):
        # Direct LangGraph usage must not bypass initial_state's validation.
        from langgraph.checkpoint.memory import InMemorySaver
        spec = load_typed_spec()
        graph = compile_workflow(spec, registry=FakeRegistry(), runtime=FakeRuntime(),
                                 agent=FakeAgent(), checkpointer=InMemorySaver())
        state = initial_state(spec, {"equipment_id": "TBM"})
        state["inputs"]["equipment_id"] = 7
        with self.assertRaises(SchemaValueError):
            await graph.ainvoke(state, {"configurable": {"thread_id": "bypass"}})

    async def test_capability_contract_snapshot_is_stable(self):
        class Registry(FakeRegistry):
            override = None
            def get(self, capability_id):
                return self.override or super().get(capability_id)
        registry = Registry()
        runner = WorkflowRunner(registry=registry, runtime=FakeRuntime(3), agent=FakeAgent())
        pending = runner.create_run(load_typed_spec(), {"equipment_id": "TBM"})
        registry.override = CapabilityInfo("telemetry.read", input_schema=object_contract({}))
        final = await runner.execute_run(pending.run_id)
        self.assertEqual(final.status, "completed")
