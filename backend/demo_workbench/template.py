"""Node-level demo template and explicit tool/Pi alternatives."""
from examples.demo import FakeRegistry, load_typed_spec, CHANGES_SCHEMA, object_contract
from runtime.workflow.interfaces import CapabilityInfo


class DemoRegistry(FakeRegistry):
    def get(self, capability_id):
        if capability_id == "proposal.generate":
            return CapabilityInfo(
                capability_id,
                input_schema=object_contract({"torque": {"type": "number"}}, ["torque"]),
                output_schema=object_contract({"changes": CHANGES_SCHEMA}, ["changes"]),
            )
        return super().get(capability_id)


def template_draft():
    spec = load_typed_spec().model_dump(mode="json")
    spec["spec_version"] = "1.2"
    options = {}
    for i, node in enumerate(spec["nodes"]):
        node.pop("kind", None)
        if node["id"] == "diagnose":
            pi = node.copy()
            pi["goal"] = "调用 history.query 获取模拟扭矩基线，返回 changes.torque 绝对目标值（不是增量）。使用 submit_result 提交结构化方案。"
            tool = {k: v for k, v in node.items() if k in ("id", "name", "inputs", "outputs", "ui_stage_id")}
            tool.update(type="tool", capability="proposal.generate")
            options[node["id"]] = {"tool": tool, "pi": pi}
            spec["nodes"][i] = tool
        elif node["type"] == "tool":
            info = DemoRegistry().get(node["capability"])
            # Side-effecting actions remain explicit, approved ToolRuntime calls.
            if info.side_effect or info.requires_approval:
                continue
            pi = {k: v for k, v in node.items() if k != "capability"}
            pi.update(type="pi", goal=f"调用 {node['capability']} 完成此节点，使用 submit_result 返回符合 Schema 的结果。",
                      capabilities=[node["capability"]], pi={},
                      input_schema=(info.input_schema.as_json_schema()
                                    if hasattr(info.input_schema, "as_json_schema") else info.input_schema),
                      output_schema=(info.output_schema.as_json_schema()
                                     if hasattr(info.output_schema, "as_json_schema") else info.output_schema))
            options[node["id"]] = {"tool": node.copy(), "pi": pi}
    return {"draft_id": "template-advance-anomaly", "status": "draft", "source": "template", "workflow": spec}, options
