"""Deterministic fakes for compiler integration; not production tools or an LLM."""
import asyncio
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from runtime.workflow import WorkflowSpec, compile_workflow, initial_state
from runtime.workflow.interfaces import CapabilityInfo, ToolResult


def object_contract(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


CHANGES_SCHEMA = object_contract({"torque": {"type": "number"}}, ["torque"])


class FakeRegistry:
    def get(self, capability_id):
        return {
            "telemetry.read": CapabilityInfo(
                "telemetry.read",
                input_schema=object_contract({"equipment_id": {"type": "string"}}),
                output_schema=object_contract({"torque": {"type": "number"}}, ["torque"])),
            "history.query": CapabilityInfo(
                "history.query", input_schema=object_contract({}),
                output_schema=object_contract({"baseline": {"type": "number"}}, ["baseline"])),
            "control.apply": CapabilityInfo(
                "control.apply", requires_approval=True, side_effect=True,
                input_schema=object_contract({"changes": CHANGES_SCHEMA}, ["changes"]),
                output_schema=object_contract({"applied": {"type": "boolean"}}, ["applied"])),
            "task.record": CapabilityInfo(
                "task.record", side_effect=True,
                input_schema=object_contract({"status": {"type": "string"}}, ["status"]),
                output_schema=object_contract({"status": {"type": "string"}}, ["status"])),
        }.get(capability_id)


class FakeRuntime:
    def __init__(self, torque=4.12):
        self.torque = torque
        self.calls = []

    async def invoke(self, capability_id, args):
        self.calls.append((capability_id, args))
        if capability_id == "telemetry.read":
            return ToolResult("ok", {"torque": self.torque})
        if capability_id == "history.query":
            return ToolResult("ok", {"baseline": 3.5})
        if capability_id == "control.apply":
            self.torque = args["changes"]["torque"]
            return ToolResult("ok", {"applied": True})
        if capability_id == "task.record":
            return ToolResult("ok", {"status": args["status"]})
        return ToolResult("fatal_error", error="unknown capability")


class FakeAgent:
    async def run(self, *, goal, context, capabilities, invoke):
        history = await invoke("history.query", {})
        return {"changes": {"torque": history["baseline"]}}


def load_typed_spec():
    return WorkflowSpec.model_validate_json(
        Path(__file__).with_name("advance_anomaly_typed.json").read_text(encoding="utf-8")
    )


def load_spec():
    return WorkflowSpec.model_validate_json(
        Path(__file__).with_name("advance_anomaly.json").read_text(encoding="utf-8")
    )


async def demo():
    spec = load_spec()
    async def print_event(event):
        print(event["type"], event["node_id"] or "", event.get("capability", ""))
    graph = compile_workflow(spec, registry=FakeRegistry(), runtime=FakeRuntime(),
                             agent=FakeAgent(), checkpointer=InMemorySaver(), event_sink=print_event)
    config = {"configurable": {"thread_id": "demo-run-001"}}
    paused = await graph.ainvoke(initial_state(spec, {"equipment_id": "TBM-01"}), config)
    print("Approval request:", paused["__interrupt__"][0].value)
    result = await graph.ainvoke(Command(resume={"approved": True}), config)
    print("Final state:", result["data"])


if __name__ == "__main__":
    asyncio.run(demo())
