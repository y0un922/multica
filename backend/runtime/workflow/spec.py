"""Versioned, JSON-serializable workflow protocol. No executable expressions."""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .schema import DataSchema, object_schema


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
ReadPath = Annotated[str, Field(pattern=r"^\$\.(inputs|data|system)(\.[A-Za-z_][A-Za-z0-9_]*)+$")]
WritePath = Annotated[str, Field(pattern=r"^\$\.data(\.[A-Za-z_][A-Za-z0-9_]*)+$")]


class Reference(Model):
    type: Literal["ref"] = "ref"
    path: ReadPath


class Constant(Model):
    type: Literal["literal"] = "literal"
    value: JsonValue


Binding = Annotated[Reference | Constant, Field(discriminator="type")]


class Predicate(Model):
    left: Binding
    op: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    right: Binding


class BaseNode(Model):
    id: Identifier
    name: str
    ui_stage_id: str | None = None
    inputs: dict[str, Binding] = Field(default_factory=dict)
    # Output key -> state path. Each executor returns a dictionary.
    outputs: dict[str, WritePath] = Field(default_factory=dict)


class CapabilityNode(BaseNode):
    type: Literal["tool"] = "tool"
    kind: Literal["capability"] = "capability"
    capability: str = Field(min_length=1)


class PiConfig(Model):
    provider: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    timeout: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class AgentNode(BaseNode):
    type: Literal["pi"] = "pi"
    kind: Literal["agent_task"] = "agent_task"
    pi: PiConfig = Field(default_factory=PiConfig)
    goal: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    input_schema: DataSchema | None = None
    output_schema: DataSchema | None = None

    @model_validator(mode="after")
    def object_contracts(self):
        object_schema(self.input_schema, "agent.input_schema")
        object_schema(self.output_schema, "agent.output_schema")
        return self


class DecisionNode(BaseNode):
    type: Literal["decision"] = "decision"
    kind: Literal["decision"] = "decision"
    predicate: Predicate

    @model_validator(mode="after")
    def no_io(self):
        if self.inputs or self.outputs:
            raise ValueError("decision uses predicate bindings, not inputs/outputs")
        return self


class ApprovalNode(BaseNode):
    type: Literal["approval"] = "approval"
    kind: Literal["approval"] = "approval"
    prompt: str = Field(min_length=1)


NodeSpec = Annotated[
    CapabilityNode | AgentNode | DecisionNode | ApprovalNode,
    Field(discriminator="kind"),
]


class EdgeSpec(Model):
    source: Identifier
    target: Identifier | Literal["$end"]
    # decision/approval require exactly one true and one false edge.
    when: bool | None = None


class WorkflowSpec(Model):
    spec_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    id: Identifier
    version: str = Field(min_length=1)
    name: str
    entrypoint: Identifier
    required_inputs: list[str] = Field(default_factory=list)
    input_schema: DataSchema | None = None
    state_schema: DataSchema | None = None
    nodes: list[NodeSpec] = Field(min_length=1)
    edges: list[EdgeSpec]

    @model_validator(mode="before")
    @classmethod
    def normalize_node_types(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
            return value
        mapping = {"pi": "agent_task", "tool": "capability", "decision": "decision", "approval": "approval"}
        nodes = []
        for node in value["nodes"]:
            if not isinstance(node, dict):
                nodes.append(node)
                continue
            node = dict(node)
            if "type" in node:
                kind = mapping.get(node["type"]) if isinstance(node["type"], str) else None
                if kind is None:
                    raise ValueError(f"{node.get('id')}: unknown node type {node['type']!r}")
                if "kind" in node and node["kind"] != kind:
                    raise ValueError(f"{node.get('id')}: type and kind conflict")
                node["kind"] = kind
            elif value.get("spec_version") == "1.2":
                raise ValueError(f"{node.get('id')}: v1.2 requires explicit node type")
            nodes.append(node)
        return {**value, "nodes": nodes}

    @model_validator(mode="after")
    def schema_contracts(self):
        object_schema(self.input_schema, "input_schema")
        object_schema(self.state_schema, "state_schema")
        if self.spec_version in ("1.1", "1.2"):
            if self.input_schema is None or self.state_schema is None:
                raise ValueError(f"v{self.spec_version} requires input_schema and state_schema")
            for node in self.nodes:
                if isinstance(node, AgentNode) and (node.input_schema is None or node.output_schema is None):
                    raise ValueError(f"{node.id}: v{self.spec_version} agent requires input_schema and output_schema")
        if self.input_schema is not None:
            missing = set(self.required_inputs) - self.input_schema.properties.keys()
            if missing:
                raise ValueError(f"required_inputs not declared in input_schema: {sorted(missing)}")
        return self

    @property
    def input_requirements(self) -> set[str]:
        return set(self.required_inputs) | set(self.input_schema.required if self.input_schema else [])

    def to_json_schema(self) -> dict[str, Any]:
        return self.model_json_schema()
