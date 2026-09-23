"""Fail closed on unsupported topology and unsafe capability access."""
from .interfaces import CapabilityRegistry
from .typecheck import check_schema_bindings
from .spec import AgentNode, ApprovalNode, CapabilityNode, DecisionNode, Reference, WorkflowSpec


class WorkflowValidationError(ValueError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("Invalid workflow:\n" + "\n".join(issues))


def validate_workflow(spec: WorkflowSpec, registry: CapabilityRegistry) -> None:
    issues: list[str] = []
    nodes = {n.id: n for n in spec.nodes}
    if len(nodes) != len(spec.nodes):
        issues.append("nodes: duplicate node IDs")
    if spec.entrypoint not in nodes:
        issues.append("entrypoint: unknown node")
    outgoing = {key: [] for key in nodes}
    incoming = {key: [] for key in nodes}
    for i, edge in enumerate(spec.edges):
        if edge.source not in nodes or (edge.target != "$end" and edge.target not in nodes):
            issues.append(f"edges[{i}]: unknown endpoint")
            continue
        outgoing[edge.source].append(edge)
        if edge.target != "$end":
            incoming[edge.target].append(edge)
    for node in spec.nodes:
        if node.id in {"system", "inputs", "data", "artifacts", "tasks", "route"}:
            issues.append(f"{node.id}: reserved LangGraph state key")
        edges = outgoing[node.id]
        if isinstance(node, (DecisionNode, ApprovalNode)):
            if len(edges) != 2 or {e.when for e in edges} != {True, False}:
                issues.append(f"{node.id}: branching node needs true and false edges")
        elif len(edges) != 1 or edges[0].when is not None:
            issues.append(f"{node.id}: needs exactly one unconditional edge (use $end to finish)")
        paths = list(node.outputs.values())
        for i, path in enumerate(paths):
            if any(path == other or path.startswith(other + ".") or other.startswith(path + ".")
                   for other in paths[i + 1:]):
                issues.append(f"{node.id}.outputs: overlapping write paths")
        caps = ([node.capability] if isinstance(node, CapabilityNode)
                else node.capabilities if isinstance(node, AgentNode) else [])
        for cap in caps:
            info = registry.get(cap)
            if info is None:
                issues.append(f"{node.id}: unknown capability {cap}")
            elif spec.spec_version in ("1.1", "1.2") and (info.input_schema is None or info.output_schema is None):
                issues.append(f"{node.id}: v1.1 capability {cap} requires input_schema and output_schema")
            elif isinstance(node, AgentNode) and (info.side_effect or info.requires_approval):
                issues.append(f"{node.id}: agent tools must be read-only; use an explicit capability node for {cap}")
            elif info.requires_approval:
                parents = incoming[node.id]
                if node.id == spec.entrypoint or not parents or any(
                    not isinstance(nodes[e.source], ApprovalNode) or e.when is not True
                    for e in parents
                ):
                    issues.append(f"{node.id}: {cap} must directly follow approval's true branch")
    if issues:
        raise WorkflowValidationError(issues)

    # Topological order detects all cycles, including disconnected cycles.
    degrees = {key: len(value) for key, value in incoming.items()}
    ready = [key for key, degree in degrees.items() if degree == 0]
    order = []
    while ready:
        key = ready.pop()
        order.append(key)
        for edge in outgoing[key]:
            if edge.target != "$end":
                degrees[edge.target] -= 1
                if degrees[edge.target] == 0:
                    ready.append(edge.target)
    if len(order) != len(nodes):
        raise WorkflowValidationError(["edges: cycles are not supported in v1"])

    reachable: set[str] = set()
    pending = [spec.entrypoint]
    while pending:
        key = pending.pop()
        if key in reachable:
            continue
        reachable.add(key)
        pending.extend(e.target for e in outgoing[key] if e.target != "$end")
    for key in nodes.keys() - reachable:
        issues.append(f"{key}: unreachable node")

    # Definite assignment: data must be produced on EVERY incoming path.
    available_after: dict[str, set[str]] = {}
    for key in order:
        parents = incoming[key]
        available = (set.intersection(*(available_after[e.source] for e in parents))
                     if parents else set())
        node = nodes[key]
        bindings = list(node.inputs.values())
        if isinstance(node, DecisionNode):
            bindings += [node.predicate.left, node.predicate.right]
        for binding in bindings:
            if not isinstance(binding, Reference):
                continue
            path = binding.path
            if path.startswith("$.data.") and not any(
                path == known or path.startswith(known + ".") for known in available
            ):
                issues.append(f"{key}: no guaranteed producer for {path}")
            if path.startswith("$.inputs.") and path.split(".")[2] not in spec.input_requirements:
                issues.append(f"{key}: input {path} must be declared in required_inputs")
        writes = set(node.outputs.values())
        # Replacing a parent object invalidates prior knowledge of its children.
        retained = {path for path in available
                    if not any(path.startswith(write + ".") for write in writes)}
        available_after[key] = retained | writes
    issues.extend(check_schema_bindings(spec, registry, order, incoming))
    if issues:
        raise WorkflowValidationError(issues)
