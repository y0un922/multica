"""Flow-sensitive type checks for the bounded DataSchema dialect."""
from .schema import DataSchema, at_path, compatible, validate_value
from .spec import AgentNode, ApprovalNode, CapabilityNode, Constant, DecisionNode


APPROVAL_OUTPUT = DataSchema.model_validate({
    "type": "object", "properties": {"approved": {"type": "boolean"}},
    "required": ["approved"], "additionalProperties": False,
})
SYSTEM_SCHEMA = DataSchema.model_validate({
    "type": "object", "properties": {
        key: {"type": "string"} for key in
        ("run_id", "workflow_id", "workflow_version", "status")
    }, "required": ["run_id", "workflow_id", "workflow_version", "status"],
    "additionalProperties": False,
})


def check_schema_bindings(spec, registry, order, incoming) -> list[str]:
    issues = []
    strict = spec.spec_version == "1.1"
    nodes = {n.id: n for n in spec.nodes}
    after = {}
    input_schema = spec.input_schema
    if input_schema is not None:
        input_schema = input_schema.model_copy(update={"required": sorted(spec.input_requirements)})

    def source_schemas(binding, env):
        path = binding.path
        parts = path[2:].split(".")
        if parts[0] == "inputs":
            return [at_path(input_schema, parts[1:], guaranteed=True)]
        if parts[0] == "system":
            # current_node may be null; no nullable unions in this dialect yet.
            if path == "$.system.current_node":
                if strict:
                    raise ValueError("current_node is nullable and cannot be bound in v1.1")
                return [None]
            return [at_path(SYSTEM_SCHEMA, parts[1:], guaranteed=True)]
        prefixes = [p for p in env if path == p or path.startswith(p + ".")]
        if prefixes:
            prefix = max(prefixes, key=len)
            suffix = path[len(prefix):].lstrip(".").split(".") if path != prefix else []
            return [at_path(schema, suffix, guaranteed=True) for schema in env[prefix]]
        # In legacy mode a parent may have been modified by a child assignment.
        return [at_path(spec.state_schema, parts[1:])]

    for key in order:
        node = nodes[key]
        parents = incoming[key]
        shared = set.intersection(*(set(after[e.source]) for e in parents)) if parents else set()
        env = {p: [s for e in parents for s in after[e.source][p]] for p in shared}
        input_contract = output_contract = None
        if isinstance(node, CapabilityNode):
            info = registry.get(node.capability)
            input_contract, output_contract = info.input_schema, info.output_schema
        elif isinstance(node, AgentNode):
            input_contract, output_contract = node.input_schema, node.output_schema
        elif isinstance(node, ApprovalNode):
            output_contract = APPROVAL_OUTPUT
        if input_contract is not None:
            missing = set(input_contract.required) - node.inputs.keys()
            if missing:
                issues.append(f"{key}.inputs: missing required capability/agent parameters {sorted(missing)}")

        for name, binding in node.inputs.items():
            try:
                target = at_path(input_contract, [name])
                if isinstance(binding, Constant):
                    validate_value(binding.value, target, f"{key}.inputs.{name}")
                else:
                    sources = source_schemas(binding, env)
                    if strict and any(s is None for s in sources):
                        raise ValueError(f"no declared type for {binding.path}")
                    if not all(compatible(s, target) for s in sources):
                        raise ValueError(f"incompatible type from {binding.path}")
            except ValueError as exc:
                issues.append(f"{key}.inputs.{name}: {exc}")

        if isinstance(node, DecisionNode):
            try:
                operands = [node.predicate.left, node.predicate.right]
                types = []
                for operand in operands:
                    if isinstance(operand, Constant):
                        value = operand.value
                        kind = ("null" if value is None else "boolean" if isinstance(value, bool)
                                else "number" if isinstance(value, (int, float))
                                else "string" if isinstance(value, str)
                                else "array" if isinstance(value, list) else "object")
                        types.append({kind})
                    else:
                        schemas = source_schemas(operand, env)
                        if strict and any(s is None for s in schemas):
                            raise ValueError(f"no declared type for {operand.path}")
                        types.append({s.type for s in schemas if s is not None})
                for left in types[0]:
                    for right in types[1]:
                        both_numeric = {left, right} <= {"integer", "number"}
                        ordered = node.predicate.op not in {"eq", "ne"}
                        if not (both_numeric or left == right) or (ordered and not (both_numeric or left == right == "string")):
                            raise ValueError(f"incompatible predicate types: {left}, {right}")
            except ValueError as exc:
                issues.append(f"{key}.predicate: {exc}")

        writes = {}
        for name, path in node.outputs.items():
            try:
                source = at_path(output_contract, [name], guaranteed=True)
                target = at_path(spec.state_schema, path.split(".")[2:])
                if strict and (source is None or target is None):
                    raise ValueError("output and state paths need declared types")
                if not compatible(source, target):
                    raise ValueError(f"incompatible output type for {path}")
                writes[path] = [source if source is not None else target]
            except ValueError as exc:
                issues.append(f"{key}.outputs.{name}: {exc}")
                writes[path] = [None]
        # Invalidate both overwritten children and parent shapes changed by a child write.
        env = {p: schemas for p, schemas in env.items() if not any(
            p == w or p.startswith(w + ".") or w.startswith(p + ".") for w in writes
        )}
        after[key] = {**env, **writes}
    return issues
