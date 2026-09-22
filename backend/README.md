# Multica：WorkflowSpec 与 LangGraph 编译器

Backend A 的第一阶段实现：JSON 工作流协议 → 静态校验 → 原生 LangGraph。
现已补充数据 Schema 校验与统一 Runner。使用说明：[Schema 协议](runtime/workflow/SCHEMA.md)、[Runner 接口](runtime/execution/README.md)。
不包含 REST/SSE、真实设备工具、文件解析或真实 LLM；这些通过接口接入。

## 快速运行

需要 Python 3.11+ 和 uv，在 `multica/backend/` 下执行：

```bash
uv sync
uv run python main.py
uv run python -m examples.runner_demo
uv run python -m unittest discover -s ../tests/runtime -v
```

示例使用确定性的 FakeAgent、FakeRuntime 和内存 checkpoint。`main.py` 为演示自动提交批准，实际产品应由经过身份验证的审批 API 提交，不能自动批准。

示例路径：读取扭矩 4.12 → 判断异常 → Agent 查询历史、生成方案 → interrupt 暂停 → 批准 → 调整到 3.5 → 再读参数 → 比较前后效果 → 记录 closed。拒绝审批或效果未改善记录 needs_attention。正常读数直接结束。

## 目录

```text
multica/
  backend/
    runtime/workflow/
      spec.py          Pydantic 工作流协议
      interfaces.py    Capability/Agent/EventSink 接口
      validator.py     静态分析、安全限制
      compiler.py      LangGraph 编译与执行绑定
    examples/
      advance_anomaly.json
      demo.py          示例适配器，不是生产业务实现
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

暂停由 LangGraph `__interrupt__` / StateSnapshot.next 表达，原始审批请求包含节点、阶段和审批上下文。不要仅依据 RunState.system.status 判断等待审批：暂停时节点尚未提交状态更新。异常会向调用方抛出并输出失败事件，checkpoint 保留最后成功状态；本层不会将失败状态持久化为 failed。现有 Runner 已统一投影 waiting_approval/failed，并提供逐节点业务数据快照；使用 Runner 时应查询它的 RunSnapshot，而非直接解析 checkpoint。

## 与角色 C 的边界

`interfaces.py` 提供当前最小端口：

- Registry.get(id) → CapabilityInfo 或 None；真实 Registry 可返回包含相应字段的适配对象。
- Runtime.invoke(id, args) → ToolResult；能力层仍需负责自身校验。编译器也会按本次编译冻结的 CapabilityInfo 输入/输出 Schema 检查调用边界。
- ToolResult：ok / retryable_error / fatal_error；非 ok 抛异常。本版不自动重试，避免重复写操作。
- AgentExecutor.run(goal, context, capabilities, invoke) → 字典；真实模型、提示词、结构化输出校验和调用预算由 AgentExecutor 实现。声明 Agent output_schema 后，编译器会校验嵌套字段类型及必需字段；v1.1 必须声明该 Schema。
- EventSink：异步接收原始事件，由角色 C 做 Event Projection 与 SSE。当前 sink 异常会向外传播，生产应接入可靠事件队列/存储适配器。

事件包括 run_started、node_started、tool_started、tool_finished、tool_failed、node_finished、node_failed、run_finished、run_failed。审批请求通过 LangGraph interrupt 暴露，不重复发送可能在恢复时重放的 approval_required 事件。事件没有全局 seq；由持久化/投影层分配。

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
