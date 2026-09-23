# Backend B / 角色 C 接口交接

对齐依据：`BACKEND_AB_INTERFACE.MD` v0.1。角色 B 是 Backend A（当前实现），角色 C 是 Backend B（平台）。

## 1. Tool Runtime：使用公共协议，不导入实现

```python
from runtime.workflow.tool_contracts import ToolContext, ToolResult

async def invoke(tool_name: str, arguments: dict, context: ToolContext) -> ToolResult:
    ...

ToolResult(status="ok", data={"task_id": "task-1", "status": "open"})
ToolResult(status="retryable_error", error={
    "category": "timeout", "code": "TOOL_TIMEOUT",
    "message": "Service timed out", "retryable": True,
})
```

B 负责严格 I/O 校验、工具执行、错误分类、tool.* 和 task.* 事件、ToolCall Trace。A 不解析 error.message，不直接调用具体工具/业务服务/Repository。

当前 A 采用 fail 策略，不自动 retry；后续重试/fallback 也只由 A 决定。ToolContext 没有幂等字段，写工具暂不盲重试。

公共模型目前在 A 仓库中镜像；共享包路径/分发需双方确认，不能各自悄悄更改模型。

## 2. Tool Catalog

```python
from runtime.workflow.catalog import CatalogRegistry

# catalog_json 为 GET /api/tools 的已加载响应；网络/鉴权由装配层负责。
registry = CatalogRegistry(catalog_json, confirmation_tools={"create_task"})
```

CatalogEntry 字段：name/description/category/side_effect/input_schema/output_schema。confirmation_tools 是 A 的策略，不向 B Catalog 虚构 requires_approval 字段。

不支持的 Schema 关键词会明确报错，详见 `backend/runtime/workflow/SCHEMA.md`。需要用 B 的真实 Schema 验证兼容性；Demo fixture 不是正式工具定义。

## 3. Runner

```python
from pydantic import TypeAdapter
from runtime.execution import RunSnapshot, WorkflowRunner

runner = WorkflowRunner(
    registry=registry, runtime=tool_runtime,
    event_publisher=shared_event_publisher, store=run_store,
)
run = await runner.start_run(spec, inputs, project_id="project", user_id="user")
response = TypeAdapter(RunSnapshot).dump_python(run, mode="json")
```

复用一个 Runner；start_run 会等待执行到结束、失败或确认暂停。需要异步调度时：先 `create_run`，再由应用任务调度器调用 `execute_run`。新注册 Run 为 running，首次调度标记不暴露为 pending。

RunStatus：running / waiting_confirmation / completed / failed。

RunSnapshot：run_id、workflow_id/version、status、state、confirmation、error、created_at/updated_at。

state.data 是逐节点已提交的数据快照。completed 不是现场异常已消除：新 Demo 的目标是创建 open 任务，而不是设备控制闭环。

## 4. EventBus 与 Confirmation

公共 AgentEvent：id/run_id/sequence/type/timestamp/payload。

A 发布：run.started、node.started、node.completed、agent.thinking、confirmation.requested、run.completed、run.failed。

B 发布：tool.started/completed/failed、task.created/updated、confirmation.resolved。

A 私有适配器 `SequencedEventPublisher` 最终调用公共 `EventBus.publish(AgentEvent)`。B 提供或装配共享序号作用域，使所有生产者使用同一顺序。**不要让 A/B 各自编号。**

B 保存 Confirmation 并暴露确认 API；决定处理后发布 confirmation.resolved，**A 的消费者**调用：

```python
await runner.handle_event(resolved_event)
```

B 不导入 LangGraph，不操作内部 interrupt ID。当前候选 payload 是完整 Confirmation 模型；详见下方待确认文档。重复相同决定在单进程内幂等，不重复创建任务。

`runner.get_events(run_id, after_sequence=N)` 只查询 A 发布的事件；sequence 可能有间隔。完整 Stream/SSE 应由 B 查询/提供。

## 5. 需要双方确认的部分

请 review [CONTRACT_CHANGE_REQUEST_AB_v0.1.md](CONTRACT_CHANGE_REQUEST_AB_v0.1.md)：

- 序号分配和发布事务；
- Confirmation payload、保存成功语义和结果投递；
- Run 状态存储装配；
- 真实 Tool Catalog Schema；
- 动作幂等与重启恢复。

这些并未因为 A 有适配器和测试就被视为共同批准。

## 6. 验证入口

```bash
cd multica/backend
uv sync
uv run python main.py
uv run python -m unittest discover -s ../tests/runtime -v
uv run python -m scripts.export_contracts
```

- 新 Demo：`backend/examples/ab_demo.py`；主入口 main.py 已切换到它。
- 单测：`tests/runtime/test_ab_integration.py`。
- 旧 examples/demo.py 与 advance_anomaly*.json 保留为编译器回归用例，不是当前 A/B Golden Path。
- 示例运行真实 A 编译器/Runner，B 是明确标注的测试替身；未完成真实平台联调。
- 单进程内存模式不支持重启恢复、多 worker、跨存储事务和 exactly-once 外部写入。
- Demo 自动接受确认只是测试脚本行为，生产必须经过 B 的用户鉴权、授权和审计。

详细行为与迁移参数见 [Runner 说明](../backend/runtime/execution/README.md)。

## 公共 Tool JSON 边界（已确认）

- `ToolResult.data: JsonValue`：B 先使用该 Tool 的严格 Output Model 校验，再导出 JSON 值。A 不依赖 Pydantic 模型实例。
- `ToolError.details`、`ToolResult.metadata`、`ToolCall.arguments`、`AgentEvent.payload` 均为 `dict[str, JsonValue]`。`details` 缺省为 `{}`，不接受显式 `null`。
- JSON 值仅允许 null、boolean、有限 number、string、数组和字符串键对象；递归拒绝任意 Python 对象、非字符串键、循环引用及 NaN/Infinity。不在公共边界隐式序列化模型、日期或 Decimal。
- `require_ok()` 成功时原样返回 `result.data`，不解析 Output Model；失败处理及 A 的 retry/fallback/fail 决策权不变。
- A 当前没有 `ToolCall` Trace 模型（由 B 管理），但已在实际 `invoke()` 前校验 arguments；B 的 Trace 模型需采用相同约束。
- Agent 工具桥接支持任意合法 JSON 结果，包括 null、数组和标量。现有 WorkflowSpec 的节点命名输出绑定仍要求对象结果，这是节点 DSL 的约束，不是公共 ToolResult 的约束。
- Schema 导出描述 JSON 类型；NaN/Infinity 本就不是合法 JSON，Python 入口另外执行有限数校验。公共模型应通过正常校验构造，不能使用 `model_construct` 或事后原地写入非法对象绕过边界。
