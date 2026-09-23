"""Compile a validated sequential/branching DAG into a native LangGraph."""
from copy import deepcopy
from datetime import datetime, timezone
import operator
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .interfaces import (AgentExecutor, AgentTaskContract, ContractAgentExecutor,
                         CapabilityRegistry, CapabilityRuntime, EventSink)
from .spec import (AgentNode, ApprovalNode, CapabilityNode, Constant, DecisionNode,
                   WorkflowSpec)
from .validator import validate_workflow
from .schema import validate_value
from .json_types import JSON_OBJECT_ADAPTER, JsonObject
from .tool_contracts import ToolContext, require_ok


class RunState(TypedDict):
    system: dict[str, Any]
    inputs: dict[str, Any]
    data: dict[str, Any]
    artifacts: dict[str, Any]
    tasks: dict[str, Any]
    route: bool


def initial_state(spec: WorkflowSpec, inputs: dict[str, Any], run_id: str | None = None) -> RunState:
    missing = spec.input_requirements - inputs.keys()
    if missing:
        raise ValueError(f"missing required inputs: {sorted(missing)}")
    validate_value(inputs, spec.input_schema, "inputs")
    return RunState(
        system={"run_id": run_id or str(uuid4()), "workflow_id": spec.id,
                "workflow_version": spec.version, "status": "running", "current_node": None},
        inputs=deepcopy(inputs), data={}, artifacts={}, tasks={}, route=False,
    )


def resolve(binding, state):
    if isinstance(binding, Constant):
        return deepcopy(binding.value)
    value = state
    for part in binding.path[2:].split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"missing binding: {binding.path}")
        value = value[part]
    return deepcopy(value)


def assign(data: dict, path: str, value: Any):
    parts = path.removeprefix("$.data.").split(".")
    target = data
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        target = target[part]
        if not isinstance(target, dict):
            raise ValueError(f"cannot write through non-object: {path}")
    target[parts[-1]] = deepcopy(value)


def compile_workflow(
    spec: WorkflowSpec, *, registry: CapabilityRegistry, runtime: CapabilityRuntime,
    agent: AgentExecutor | None = None, checkpointer=None,
    event_sink: EventSink | None = None,
):
    # Defensive copy: later draft edits must not mutate a compiled version.
    spec = spec.model_copy(deep=True)
    # A running version must not silently change its contracts when Registry changes.
    capability_ids = {cap for node in spec.nodes for cap in
                      ([node.capability] if isinstance(node, CapabilityNode)
                       else node.capabilities if isinstance(node, AgentNode) else [])}
    contracts = {cap: deepcopy(registry.get(cap)) for cap in capability_ids}

    class FrozenRegistry:
        def get(self, capability_id):
            return contracts.get(capability_id)

    registry = FrozenRegistry()
    validate_workflow(spec, registry)
    if any(isinstance(n, ApprovalNode) for n in spec.nodes) and checkpointer is None:
        raise ValueError("approval requires a checkpointer and a thread_id at invocation")
    if any(isinstance(n, AgentNode) for n in spec.nodes) and agent is None:
        raise ValueError("agent_task requires an AgentExecutor")

    async def emit(state, kind, node=None, **details):
        if event_sink:
            await event_sink({
                "type": kind, "run_id": state["system"]["run_id"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "node_id": node.id if node else None,
                "ui_stage_id": (node.ui_stage_id or node.id) if node else None,
                **details,
            })

    def build_node(node):
        async def execute(state: RunState):
            system = {**state["system"], "status": "running", "current_node": node.id}
            # On resume LangGraph re-enters this node. Nothing with side effects
            # occurs before interrupt, and approval_required is not emitted here twice.
            if isinstance(node, ApprovalNode):
                try:
                    args = {key: resolve(binding, state) for key, binding in node.inputs.items()}
                except Exception as exc:
                    await emit(state, "node_failed", node, error=str(exc))
                    await emit(state, "run_failed", node, error=str(exc))
                    raise
                response = interrupt({"type": "approval_required", "node_id": node.id,
                                      "ui_stage_id": node.ui_stage_id or node.id,
                                      "prompt": node.prompt, "context": args})
                if not isinstance(response, dict) or type(response.get("approved")) is not bool:
                    message = "approval response must be {'approved': bool}"
                    await emit(state, "node_failed", node, error=message)
                    await emit(state, "run_failed", node, error=message)
                    raise ValueError(message)
                await emit(state, "node_started", node)
                result = {"approved": response["approved"]}
                route = response["approved"]
            else:
                await emit(state, "node_started", node)
                try:
                    args = {key: resolve(binding, state) for key, binding in node.inputs.items()}

                    async def invoke(capability_id: str, arguments: JsonObject):
                        if isinstance(node, AgentNode) and capability_id not in node.capabilities:
                            raise PermissionError(f"capability not allowed: {capability_id}")
                        arguments = JSON_OBJECT_ADAPTER.validate_python(arguments)
                        info = contracts[capability_id]
                        validate_value(arguments, info.input_schema, f"{node.id}.{capability_id}.inputs")
                        system_context = state["system"]
                        tool_result = await runtime.invoke(
                            tool_name=capability_id,
                            arguments=arguments,
                            context=ToolContext(
                                run_id=system_context["run_id"], node_id=node.id,
                                project_id=system_context.get("project_id"),
                                user_id=system_context.get("user_id"),
                                trace_id=system_context.get("trace_id") or system_context["run_id"],
                            ),
                        )
                        # Fail closed by default. Workflow retry policy belongs to A;
                        # never blindly replay an action after an ambiguous timeout.
                        result = require_ok(tool_result, tool_name=capability_id, node_id=node.id)
                        validate_value(result, info.output_schema, f"{node.id}.{capability_id}.outputs")
                        return result

                    route = False
                    if isinstance(node, CapabilityNode):
                        result = await invoke(node.capability, args)
                    elif isinstance(node, AgentNode):
                        await emit(state, "agent_thinking", node, summary="Executing agent task", goal=node.goal)
                        validate_value(args, node.input_schema, f"{node.id}.inputs")
                        agent_args = dict(goal=node.goal, context=args,
                                          capabilities=list(node.capabilities), invoke=invoke)
                        if isinstance(agent, ContractAgentExecutor):
                            result = await agent.run_with_contract(
                                contract=AgentTaskContract(
                                    run_id=state["system"]["run_id"], node_id=node.id,
                                    output_schema=deepcopy(node.output_schema),
                                    capability_specs={cap: deepcopy(contracts[cap]) for cap in node.capabilities},
                                ), **agent_args)
                        else:
                            result = await agent.run(**agent_args)
                    elif isinstance(node, DecisionNode):
                        ops = {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt,
                               "ge": operator.ge, "lt": operator.lt, "le": operator.le}
                        route = bool(ops[node.predicate.op](resolve(node.predicate.left, state),
                                                           resolve(node.predicate.right, state)))
                        result = {}
                    else:
                        raise TypeError(f"unsupported node: {node.kind}")
                except Exception as exc:
                    await emit(state, "node_failed", node, error=str(exc))
                    await emit(state, "run_failed", node, error=str(exc))
                    raise
            try:
                if not isinstance(result, dict):
                    raise TypeError(f"{node.id}: executor output must be a dictionary")
                if isinstance(node, AgentNode):
                    validate_value(result, node.output_schema, f"{node.id}.outputs")
                data = deepcopy(state["data"])
                for key, path in node.outputs.items():
                    if key not in result:
                        raise ValueError(f"{node.id}: missing output {key}")
                    assign(data, path, result[key])
                validate_value(data, spec.state_schema, f"{node.id}.data", partial=True)
            except Exception as exc:
                await emit(state, "node_failed", node, error=str(exc))
                await emit(state, "run_failed", node, error=str(exc))
                raise
            await emit(state, "node_finished", node)
            return {"system": system, "data": data, "route": route}
        return execute

    graph = StateGraph(RunState)

    async def start(state):
        missing = spec.input_requirements - state["inputs"].keys()
        if missing:
            raise ValueError(f"missing required inputs: {sorted(missing)}")
        validate_value(state["inputs"], spec.input_schema, "inputs")
        validate_value(state["data"], spec.state_schema, "data", partial=True)
        if (state["system"]["workflow_id"], state["system"]["workflow_version"]) != (spec.id, spec.version):
            raise ValueError("run is bound to a different workflow version")
        await emit(state, "run_started")
        return {"system": {**state["system"], "status": "running"}}

    async def finish(state):
        try:
            validate_value(state["data"], spec.state_schema, "data")
        except Exception as exc:
            await emit(state, "run_failed", error=str(exc))
            raise
        await emit(state, "run_finished")
        return {"system": {**state["system"], "status": "completed", "current_node": None}}

    # User IDs cannot start with underscore, so these names cannot collide.
    graph.add_node("_start", start)
    graph.add_node("_finish", finish)
    graph.add_edge(START, "_start")
    graph.add_edge("_start", spec.entrypoint)
    graph.add_edge("_finish", END)
    for node in spec.nodes:
        graph.add_node(node.id, build_node(node))
        edges = [edge for edge in spec.edges if edge.source == node.id]
        target = lambda edge: "_finish" if edge.target == "$end" else edge.target
        if isinstance(node, (DecisionNode, ApprovalNode)):
            graph.add_conditional_edges(node.id, lambda state: state["route"],
                                        {edge.when: target(edge) for edge in edges})
        else:
            graph.add_edge(node.id, target(edges[0]))
    return graph.compile(checkpointer=checkpointer)
