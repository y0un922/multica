# Multica：WorkflowSpec 与 LangGraph 编译器

Backend A 的第一阶段实现：JSON 工作流协议 → 静态校验 → 原生 LangGraph。
现已补充数据 Schema 校验与统一 Runner。使用说明：[Schema 协议](runtime/workflow/SCHEMA.md)、[Runner 接口](runtime/execution/README.md)。
已按 `BACKEND_AB_INTERFACE.MD` v0.1 迁移工具边界、Confirmation 和公共事件。REST/SSE、真实工具、持久平台实现通过接口接入；当前联合示例使用假 Backend B。

## 节点级执行配置（新增 v1.2）

模板可用 `type: pi` 配置独立 Agent，`type: tool` 直接调用能力；`decision/approval` 保留控制流语义。Compiler 和 Runner 支持 `agent_factory(node)`，每个 Pi 节点可配置不同 provider/model/timeout。旧 v1.0/v1.1 kind 模板仍兼容。新的 Demo 不再接受 Run 级 Fake/Pi 覆盖，执行方式取自模板节点。

运行 `uv run python -m demo_workbench.server --enable-pi`，打开 `http://127.0.0.1:8010` 配置各节点。详见 [节点配置、协议和测试](demo_workbench/README.md)。下文的旧示例与边界说明仍适用，Pi 接入细节以 [Agent 说明](runtime/agent/README.md) 为准。

## 快速运行

需要 Python 3.11+ 和 uv，在 `multica/backend/` 下执行：

```bash
uv sync
uv run python main.py
uv run python -m examples.ab_demo
uv run python -m unittest discover -s ../tests/runtime -v
```

主入口使用真实 A Runner/Compiler、假 B ToolRuntime/Catalog、共享内存 EventBus 与 checkpoint。`main.py` 为演示模拟用户接受确认；正式系统必须由经过鉴权的 B Confirmation API 接收决定。

主路径：query_tbm_status → query_sensor_history → detect_parameter_anomaly → query_geological_data → diagnose_fault → estimate_risk → A 判断需要确认 → waiting_confirmation → 用户 accepted → A Resume → create_task → run.completed。正常场景跳过诊断与任务创建；rejected 不创建任务。

新示例 `examples/ab_demo.py` 的完整 Tool Schema 是明确标注的 fixture，尚需替换为 B 的真实 Catalog。旧 examples/demo.py 与 advance_anomaly*.json 保留为编译器回归用例，不代表当前共同验收路径。

## 目录

```text
multica/
  backend/
    runtime/workflow/
      spec.py          Pydantic 工作流协议
      interfaces.py    编译器内部 Capability/Agent/EventSink 端口
      tool_contracts.py A/B ToolContext、ToolResult、ToolError
      catalog.py       公开 Tool Catalog 适配器
      validator.py     静态分析、安全限制
      compiler.py      LangGraph 编译与执行绑定
    examples/
      ab_demo.py       当前 A/B Golden Path（假 B）
      advance_anomaly.json
      demo.py          旧编译器回归适配器
    main.py            后端演示入口
    pyproject.toml
    uv.lock
    README.md
  contracts/
    workflow.schema.json
  tests/runtime/
    test_workflow.py
```

## 协议 v1.0 / v1.1

`WorkflowSpec` 的业务版本 `version` 与格式版本 `spec_version` 分离。

| 字段 | 含义 |
|---|---|
| spec_version | `1.0` 兼容无类型工作流；`1.1` 要求输入/状态/执行器类型契约 |
| id / version / name | 工作流标识、业务版本、展示名称 |
| entrypoint | 唯一入口节点 ID |
| required_inputs | 旧版必需输入名称，与 input_schema.required 取并集 |
| input_schema / state_schema | 工作流输入与业务 data 的 DataSchema，v1.1 必填 |
| nodes | 带 kind 判别字段的节点列表 |
| edges | source / target / when，终点用 `$end` |

普通节点必须有且仅有一条无条件出边，包括指向 `$end` 的结束边。decision 和 approval 必须各有一条 `when: true` 和 `when: false` 出边。此版本仅支持无环、单活跃路径的顺序与分支，不支持隐式并行。

### 四种节点

- `capability`：`capability` 指定能力 ID，通过统一 Runtime 调用。
- `agent_task`：`goal` 和 `capabilities` 定义目标与可用只读能力，交给注入的 AgentExecutor。
- `decision`：通过 `predicate` 比较状态数据，支持 eq/ne/gt/ge/lt/le，不执行 Python 或 eval。该节点不接受 inputs/outputs。
- `approval`：`prompt` 和 inputs 提供待确认内容；interrupt 恢复值必须为 `{"approved": true/false}`，按批准结果路由。可将 approved 输出绑定到 data。

所有节点支持 `id`、`name`、`ui_stage_id`。没有 ui_stage_id 时，原始事件使用节点 ID。

### 显式数据绑定

```json
{
  "id": "read_status",
  "kind": "capability",
  "name": "读取设备状态",
  "capability": "telemetry.read",
  "inputs": {
    "equipment_id": {"type": "ref", "path": "$.inputs.equipment_id"},
    "fields": {"type": "literal", "value": ["torque"]}
  },
  "outputs": {"torque": "$.data.torque"}
}
```

`inputs` 的键是调用参数名；`outputs` 的键是执行器返回字典的字段名，值是写入路径。literal 可承载 JSON 对象或数组，内部不会再次解释引用。

引用仅支持点分隔对象路径，不支持完整 JSONPath、数组索引或通配符。可读 inputs/data/system，只能写 data。路径缺失、穿过非对象或输出字段缺失会报错，不会静默返回 null。编译器不修改上游状态或工具传入的对象。

### RunState

```text
system: run_id / workflow_id / workflow_version / status / current_node
inputs: 运行输入
data: 工作流业务数据
artifacts: 预留产物空间
tasks: 预留任务空间
route: 编译器内部条件路由值，前端不应依赖
```

使用 `initial_state(spec, inputs)` 创建状态。编译时深拷贝 spec；后续草稿变更不影响已经编译的图。编译结果是原生 CompiledStateGraph，不是 Runner 服务。

## 编译和审批恢复

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from runtime.workflow import compile_workflow, initial_state

graph = compile_workflow(
    spec,
    registry=registry,
    runtime=capability_runtime,
    agent=agent_executor,
    checkpointer=InMemorySaver(),
    event_sink=async_event_sink,
)
config = {"configurable": {"thread_id": "unique-run-id"}}
state = await graph.ainvoke(initial_state(spec, inputs), config)

if state.get("__interrupt__"):
    request = state["__interrupt__"][0].value
    # 将 request 交给审批系统；由审批结果驱动后续调用。
    state = await graph.ainvoke(Command(resume={"approved": True}), config)
```

每次独立运行使用不同 thread_id，同一次恢复使用原 thread_id。含 approval 的工作流必须提供 checkpointer。InMemorySaver 仅用于 Demo，进程重启后不能恢复；生产需注入持久化 checkpointer。

暂停由 LangGraph `__interrupt__` / StateSnapshot.next 表达，原始审批请求包含节点、阶段和审批上下文。不要仅依据 RunState.system.status 判断等待审批：暂停时节点尚未提交状态更新。异常会向调用方抛出并输出失败事件，checkpoint 保留最后成功状态；本层不会将失败状态持久化为 failed。现有 Runner 已统一投影 waiting_confirmation/failed，并提供逐节点业务数据快照；使用 Runner 时应查询它的 RunSnapshot，而非直接解析 checkpoint。

## 与角色 C 的边界

`interfaces.py` 提供当前最小端口：

- Registry.get(id) → CapabilityInfo 或 None；真实 Registry 可返回包含相应字段的适配对象。
- ToolRuntime.invoke(tool_name=..., arguments=..., context=ToolContext(...)) → ToolResult；按 `BACKEND_AB_INTERFACE.MD` v0.1 调用，模型定义位于 `runtime/workflow/tool_contracts.py`。`CapabilityRuntime` 仅保留为内部类型别名，不支持旧的两参数调用。能力层仍需负责自身校验，编译器也会按冻结的 Schema 检查调用边界。
- ToolResult：`status / data / error / metadata`，错误使用结构化 ToolError。`require_ok(result, tool_name=..., node_id=...)` 在非 ok 时抛出保留 status/category/code 的 ToolInvocationError。本版默认失败，不自动重试，避免重复写操作。
- AgentExecutor.run(goal, context, capabilities, invoke) → 字典；真实模型、提示词、结构化输出校验和调用预算由 AgentExecutor 实现。声明 Agent output_schema 后，编译器会校验嵌套字段类型及必需字段；v1.1 必须声明该 Schema。
- EventSink：异步接收原始事件，由角色 C 做 Event Projection 与 SSE。当前 sink 异常会向外传播，生产应接入可靠事件队列/存储适配器。

A 侧原始事件包括 run_started、node_started、node_finished、node_failed、run_finished、run_failed。工具调用事件及 ToolCall Trace 由 B 的 ToolRuntime 负责，A 不重复发布。审批请求通过 LangGraph interrupt 暴露，不重复发送可能在恢复时重放的 approval_required 事件。事件没有全局 seq；由持久化/投影层分配。

### A/B v0.1 对齐范围与待办

已迁移工具签名与三态模型、公开 Catalog 适配、公共四态 RunStatus、Confirmation、accepted/rejected、公共 AgentEvent、事件所有权，以及重复确认投递防重。

Runner.start_run/create_run 支持 project_id/user_id/trace_id，调用上下文由 A 传递；缺省 trace_id 使用 run_id。内部 approval 节点仍用 bool 路由，不是 B 的接口。

Runner 的 `get_events(..., after_sequence=N)` 返回 A 发布的公共事件；编译器的 EventSink 仍是 A 私有原始事件端口。所有 A/B 发布者通过共享适配器分配 sequence，不能将 Runner 缓存当完整平台 Stream。

正式接入尚需双方确认序号/持久化机制、Confirmation payload 和结果投递、Run 状态存储，并提供真实 Tool Schema。详情见 [待确认请求](../contracts/CONTRACT_CHANGE_REQUEST_AB_v0.1.md)。当前用假 B 测试通过，不等于真实 B E2E 已完成。

## 校验与安全边界

已支持：

- Pydantic 严格字段校验、节点类型判别、拒绝未知 kind/字段。
- 唯一 ID、保留状态名称、合法边端点、分支完整、无环、入口可达。
- 已注册 Capability 校验、明确的数据生产者检查、分支合流时要求所有路径都有来源。
- 重叠输出路径检查、必需输入声明与运行时缺失检查。
- requires_approval 能力必须直接位于 approval 的 true 分支后，不能从其他路径或入口绕过。
- Agent 只能通过注入的 invoke 调用白名单内能力；side_effect 或 requires_approval 能力不能暴露给 Agent，应使用显式 capability 节点。

限制：

- 新增静态类型兼容检查与运行时 Schema 校验，范围是明确限制的 JSON Schema 子集，不支持任意 JSON Schema。详见 Schema 协议文档。
- AgentExecutor 是可信适配器，白名单不是针对恶意 Python 插件的沙箱。
- 审批是运行图的编排约束，不代替平台身份鉴别、审批授权、审批凭证或设备侧策略。
- LangGraph checkpoint 不保证外部副作用恰好执行一次。崩溃恢复/重放时，工具仍需幂等键与平台层防重；当前协议暂未传递调用幂等上下文。
- 不支持自动重试、超时策略、parallel/subflow/wait/循环、工作流生成器、版本仓库和真实 LLM。这些留给后续迭代。

## 更新 JSON Schema

```bash
uv run python -m scripts.export_contracts
```

JSON Schema 约束结构；能力、图拓扑和审批规则仍需要 `validate_workflow()`。不能仅凭 schema 校验通过就发布执行。
