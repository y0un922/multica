"""The public tool boundary carries JSON, never arbitrary Python objects."""
from datetime import datetime
from decimal import Decimal
import unittest

from pydantic import BaseModel, ConfigDict, ValidationError

from runtime.agent.bridge import CapabilityBridge
from runtime.execution.contracts import AgentEvent
from runtime.workflow.json_schema import check_provider_schema
from runtime.workflow.json_types import JSON_OBJECT_ADAPTER
from runtime.workflow.tool_contracts import ToolError, ToolResult, require_ok


def error(details):
    return ToolError(category="validation_error", code="INVALID", message="invalid",
                     retryable=False, details=details)


def event(payload):
    return AgentEvent(id="e", run_id="r", sequence=1, type="tool.completed",
                      timestamp="2026-01-01T00:00:00Z", payload=payload)


class Output(BaseModel):
    model_config = ConfigDict(strict=True)
    count: int


class JsonBoundaryTests(unittest.TestCase):
    def test_success_json_values_and_identity(self):
        for value in [None, False, 0, 1.5, "", [], {}, [1, {"nested": [True, None]}]]:
            with self.subTest(value=value):
                result = ToolResult(status="ok", data=value)
                self.assertIs(require_ok(result, tool_name="test"), result.data)
                self.assertEqual(result.data, value)

    def test_provider_refs_are_inspected_only_at_schema_positions(self):
        check_provider_schema({
            "type": "object",
            "properties": {"$ref": {"type": "string", "default": "$ref"}},
            "$defs": {"$ref": {"type": "string", "enum": ["https://example.com/ref"]}},
        })
        for ref_schema in (
            {"$ref": "https://example.com/schema"},
            {"$dynamicRef": "https://example.com/schema"},
            {"$recursiveRef": "#"},
        ):
            with self.subTest(schema=ref_schema), self.assertRaises(ValueError):
                check_provider_schema(ref_schema)

    def test_strict_output_model_then_json_export(self):
        with self.assertRaises(ValidationError):
            Output.model_validate({"count": "1"})
        output = Output.model_validate({"count": 1})
        with self.assertRaises(ValidationError):
            ToolResult(status="ok", data=output)
        result = ToolResult(status="ok", data=output.model_dump(mode="json"))
        self.assertIsInstance(require_ok(result, tool_name="test"), dict)
        self.assertEqual(Output.model_validate(result.data), output)

    def test_invalid_values_rejected_at_every_boundary(self):
        invalid = [object(), datetime.now(), Decimal("1.2"), b"text", (1,), {1},
                   {1: "non-string key"}, float("nan"), float("inf"), -float("inf"),
                   Output(count=1)]
        cycle = []
        cycle.append(cycle)
        invalid.append(cycle)
        for value in invalid:
            for construct in [lambda v: ToolResult(status="ok", data=v),
                              lambda v: ToolResult(status="ok", metadata={"nested": [v]}),
                              lambda v: error({"nested": [v]}),
                              lambda v: event({"nested": [v]}),
                              lambda v: JSON_OBJECT_ADAPTER.validate_python({"nested": [v]})]:
                with self.subTest(kind=type(value).__name__), self.assertRaises(ValidationError):
                    construct(value)

    def test_dynamic_fields_require_objects(self):
        for value in [None, [], "text", 3]:
            for construct in [lambda v: ToolResult(status="ok", metadata=v), event,
                              JSON_OBJECT_ADAPTER.validate_python]:
                with self.subTest(value=value), self.assertRaises(ValidationError):
                    construct(value)
        for value in [[], "text", 3]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                error(value)
        self.assertIsNone(error(None).details)
        self.assertEqual(error({}).details, {})


class BridgeJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_values_need_not_be_objects(self):
        for value in [None, False, 0, "text", [1, {"ok": True}]]:
            async def invoke(cap, args):
                return value
            bridge = CapabilityBridge(["test"], invoke)
            self.assertEqual(await bridge._execute("test", {}), {"ok": True, "value": value})

    async def test_invalid_tool_output_fails_without_retry(self):
        calls = []
        async def invoke(cap, args):
            calls.append(cap)
            return {"value": float("nan")}
        result = await CapabilityBridge(["test"], invoke)._execute("test", {})
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["test"])
