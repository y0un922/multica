"""A/B v0.1 integration demonstration with an explicitly FAKE Backend B.

Tool names/envelopes follow the interface document. Schemas not supplied by that
file are illustrative fixtures, NOT authoritative production Tool Contracts.
Run with: python -m examples.ab_demo
"""
import asyncio
from copy import deepcopy
from uuid import uuid4

from runtime.execution import WorkflowRunner
from runtime.execution.contracts import Confirmation
from runtime.execution.events import InMemoryEventBus
from runtime.execution.state import utc_now
from runtime.workflow import WorkflowSpec
from runtime.workflow.catalog import CatalogRegistry
from runtime.workflow.schema import DataSchema, validate_value
from runtime.workflow.tool_contracts import ToolResult


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string"}
NUMBER = {"type": "number"}
STRINGS = {"type": "array", "items": TEXT}
SAMPLES = {"type": "array", "items": NUMBER}
MACHINE = obj({"machine_id": TEXT})
STATUS = obj({"machine_id": TEXT, "timestamp": TEXT, "torque": NUMBER, "thrust": NUMBER,
              "advance_speed": NUMBER, "chamber_pressure": NUMBER, "vibration": NUMBER})
RISK = {"type": "string", "enum": ["low", "high"]}


def entry(name, category, inputs, outputs):
    return {"name": name, "description": f"Demo fixture: {name}", "category": category,
            "side_effect": category == "action", "input_schema": inputs, "output_schema": outputs}


def demo_catalog():
    return [
        entry("query_tbm_status", "data", MACHINE, STATUS),
        entry("query_sensor_history", "data", MACHINE, obj({"samples": SAMPLES})),
        entry("query_geological_data", "data", MACHINE, obj({"geology": TEXT})),
        entry("get_construction_progress", "data", MACHINE, obj({"ring": {"type": "integer"}})),
        entry("detect_parameter_anomaly", "analysis", obj({"torque": NUMBER, "samples": SAMPLES}),
              obj({"abnormal": {"type": "boolean"}})),
        entry("diagnose_fault", "analysis", obj({"torque": NUMBER, "geology": TEXT}),
              obj({"diagnosis": TEXT, "confidence": NUMBER, "evidence": STRINGS})),
        entry("estimate_risk", "analysis", obj({"torque": NUMBER, "diagnosis": TEXT}),
              obj({"risk_level": RISK, "evidence": STRINGS})),
        entry("create_task", "action", obj({"machine_id": TEXT, "description": TEXT, "risk_level": RISK}),
              obj({"task_id": TEXT, "status": TEXT})),
        entry("update_task", "action", obj({"task_id": TEXT, "status": TEXT}),
              obj({"task_id": TEXT, "status": TEXT})),
    ]


def build_workflow():
    def ref(path):
        return {"type": "ref", "path": path}

    def tool(node_id, name, inputs, outputs):
        return {"id": node_id, "name": name, "kind": "capability", "capability": name,
                "inputs": {key: ref(path) for key, path in inputs.items()},
                "outputs": {key: f"$.data.{value}" for key, value in outputs.items()}}

    nodes = [
        tool("read", "query_tbm_status", {"machine_id": "$.inputs.machine_id"}, {"torque": "torque"}),
        tool("history", "query_sensor_history", {"machine_id": "$.inputs.machine_id"}, {"samples": "samples"}),
        tool("detect", "detect_parameter_anomaly", {"torque": "$.data.torque", "samples": "$.data.samples"},
             {"abnormal": "abnormal"}),
        {"id": "abnormal", "name": "Check anomaly", "kind": "decision", "predicate": {
            "left": ref("$.data.abnormal"), "op": "eq", "right": {"type": "literal", "value": True}}},
        tool("geology", "query_geological_data", {"machine_id": "$.inputs.machine_id"}, {"geology": "geology"}),
        tool("diagnose", "diagnose_fault", {"torque": "$.data.torque", "geology": "$.data.geology"},
             {"diagnosis": "diagnosis", "confidence": "confidence"}),
        tool("risk", "estimate_risk", {"torque": "$.data.torque", "diagnosis": "$.data.diagnosis"},
             {"risk_level": "risk_level"}),
        {"id": "risk_policy", "name": "A confirmation policy", "kind": "decision", "predicate": {
            "left": ref("$.data.risk_level"), "op": "eq", "right": {"type": "literal", "value": "high"}}},
        {"id": "confirm", "name": "Human confirmation", "kind": "approval",
         "prompt": "Create a follow-up task for this high-risk anomaly?",
         "inputs": {"diagnosis": ref("$.data.diagnosis"), "risk_level": ref("$.data.risk_level")}},
        tool("task", "create_task", {"machine_id": "$.inputs.machine_id", "description": "$.data.diagnosis",
                                     "risk_level": "$.data.risk_level"},
             {"task_id": "task_id", "status": "task_status"}),
    ]
    edges = [{"source": a, "target": b} for a, b in [
        ("read", "history"), ("history", "detect"), ("detect", "abnormal"),
        ("geology", "diagnose"), ("diagnose", "risk"), ("risk", "risk_policy"), ("task", "$end")]]
    for source, yes, no in [("abnormal", "geology", "$end"), ("risk_policy", "confirm", "$end"),
                             ("confirm", "task", "$end")]:
        edges.extend([{"source": source, "target": yes, "when": True},
                      {"source": source, "target": no, "when": False}])
    data = obj({"torque": NUMBER, "samples": SAMPLES, "abnormal": {"type": "boolean"},
                "geology": TEXT, "diagnosis": TEXT, "confidence": NUMBER, "risk_level": RISK,
                "task_id": TEXT, "task_status": TEXT})
    # Anomaly-only outputs cannot be required on the normal or rejected path.
    data["required"] = ["torque", "samples", "abnormal"]
    return WorkflowSpec.model_validate({
        "spec_version": "1.1", "id": "torque_anomaly", "version": "1", "name": "Torque anomaly task",
        "entrypoint": "read", "input_schema": MACHINE, "state_schema": data, "nodes": nodes, "edges": edges,
    })


class DemoPlatformEvents(InMemoryEventBus):
    """Fake B: persists confirmations; never imports or resumes LangGraph."""
    def __init__(self):
        super().__init__()
        self.confirmations: dict[str, Confirmation] = {}

    async def emit(self, *, run_id, event_type, payload):
        if event_type == "confirmation.requested":
            confirmation = Confirmation.model_validate(payload)
            self.confirmations[confirmation.id] = confirmation.model_copy(deep=True)
        return await super().emit(run_id=run_id, event_type=event_type, payload=payload)

    async def resolve_confirmation(self, run_id, confirmation_id, decision):
        if decision not in ("accepted", "rejected"):
            raise ValueError("invalid decision")
        confirmation = self.confirmations[confirmation_id]
        if confirmation.run_id != run_id or confirmation.status != "pending":
            raise ValueError("confirmation is not pending for this run")
        resolved = confirmation.model_copy(update={"status": decision, "resolved_at": utc_now()})
        self.confirmations[confirmation_id] = resolved
        return await self.emit(run_id=run_id, event_type="confirmation.resolved",
                               payload=resolved.model_dump(mode="json"))


class DemoToolRuntime:
    """Deterministic B test double, including validated tools and trace events."""
    def __init__(self, events, *, scenario="torque_anomaly"):
        if scenario not in ("normal", "torque_anomaly"):
            raise ValueError("unknown demo scenario")
        self.events = events
        self.scenario = scenario
        self.calls = []
        self.tasks = {}
        self.catalog = {entry["name"]: entry for entry in demo_catalog()}

    async def invoke(self, tool_name, arguments, context):
        self.calls.append((tool_name, deepcopy(arguments), context.model_copy(deep=True)))
        payload = {"tool_name": tool_name, "node_id": context.node_id, "trace_id": context.trace_id}
        await self.events.emit(run_id=context.run_id, event_type="tool.started", payload=payload)
        try:
            contract = self.catalog[tool_name]
            validate_value(arguments, DataSchema.model_validate(contract["input_schema"]), "tool.inputs")
            torque = 4.12 if self.scenario == "torque_anomaly" else 3.0
            if tool_name == "query_tbm_status":
                data = {"machine_id": arguments["machine_id"], "timestamp": utc_now(), "torque": torque,
                        "thrust": 100.0, "advance_speed": 31.0, "chamber_pressure": 2.18, "vibration": 0.1}
            elif tool_name == "query_sensor_history":
                data = {"samples": [3.0, 3.1, 3.0]}
            elif tool_name == "query_geological_data":
                data = {"geology": "clay"}
            elif tool_name == "get_construction_progress":
                data = {"ring": 1229}
            elif tool_name == "detect_parameter_anomaly":
                data = {"abnormal": arguments["torque"] > max(arguments["samples"]) * 1.2}
            elif tool_name == "diagnose_fault":
                data = {"diagnosis": "Increased cutterhead load in clay", "confidence": 0.82,
                        "evidence": ["torque exceeds history baseline"]}
            elif tool_name == "estimate_risk":
                data = {"risk_level": "high" if arguments["torque"] > 4 else "low", "evidence": ["torque"]}
            elif tool_name == "create_task":
                task_id = str(uuid4())
                self.tasks[task_id] = {**deepcopy(arguments), "task_id": task_id, "status": "open"}
                data = {"task_id": task_id, "status": "open"}
                await self.events.emit(run_id=context.run_id, event_type="task.created", payload=data)
            else:  # update_task
                self.tasks[arguments["task_id"]]["status"] = arguments["status"]
                data = dict(arguments)
                await self.events.emit(run_id=context.run_id, event_type="task.updated", payload=data)
            validate_value(data, DataSchema.model_validate(contract["output_schema"]), "tool.outputs")
            result = ToolResult(status="ok", data=data)
        except (KeyError, ValueError) as exc:
            result = ToolResult(status="fatal_error", error={"category": "validation_error", "code": "DEMO_ERROR",
                                "message": str(exc), "retryable": False})
        await self.events.emit(run_id=context.run_id,
                               event_type="tool.completed" if result.status == "ok" else "tool.failed",
                               payload={**payload, "result": result.model_dump(mode="json")})
        return result


def make_demo(*, scenario="torque_anomaly"):
    events = DemoPlatformEvents()
    runtime = DemoToolRuntime(events, scenario=scenario)
    registry = CatalogRegistry(demo_catalog(), confirmation_tools={"create_task"})
    runner = WorkflowRunner(registry=registry, runtime=runtime, event_publisher=events)
    return runner, runtime, events


async def main():
    runner, runtime, events = make_demo()
    paused = await runner.start_run(build_workflow(), {"machine_id": "TBM-01"})
    print(paused.status, paused.confirmation.prompt)
    # Demo only: simulate the user's B API response, then A's queued event consumer.
    event = await events.resolve_confirmation(paused.run_id, paused.confirmation.id, "accepted")
    final = await runner.handle_event(event)
    print(final.status, final.state["data"]["task_id"], "tasks:", len(runtime.tasks))
    for event in events.get_events(final.run_id):
        print(event.sequence, event.type)


if __name__ == "__main__":
    asyncio.run(main())
