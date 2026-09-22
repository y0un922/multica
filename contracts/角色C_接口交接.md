# 角色 C 接口交接（当前实现）

**分工：**角色 B 提供工作流协议、编译器和 Runner；角色 C 实现能力层、模拟世界、REST、事件投影与 SSE。AgentExecutor 由角色 B 提供，C 不需要实现 Agent。

## 1. C 需要提供的接口

定义：`backend/runtime/workflow/interfaces.py`。实现同名方法即可，无需继承。

```python
from runtime.workflow.interfaces import CapabilityInfo, ToolResult

# Registry：同步查询；不存在返回 None
registry.get(capability_id: str) -> CapabilityInfo | None

# Runtime：异步调用，负责输入/输出校验与真实或模拟执行
await runtime.invoke(capability_id: str, args: dict) -> ToolResult
```

```python
CapabilityInfo(id="control.apply", side_effect=True, requires_approval=True)
ToolResult(status="ok", value={"applied": True})  # 成功必须返回字典
ToolResult(status="retryable_error", error="暂时不可用")
ToolResult(status="fatal_error", error="参数非法")
```

- 当前没有自动重试，非 `ok` 会导致运行失败。
- `requires_approval=True` 的能力必须紧接审批通过分支；Agent 仅能使用只读能力。
- 当前 invoke **没有 run_id/调用幂等上下文**，真实写操作接入前需双方补齐；不能假定 checkpoint 保证写操作只执行一次。
- Registry.search/Resolver 暂未实现，不是本轮接入前提。

## 2. C 调用 Runner

定义：`backend/runtime/execution/runner.py`。**服务生命周期内复用一个 Runner，不能每次请求新建。**

```python
from dataclasses import asdict
from runtime.execution import WorkflowRunner
from runtime.workflow import WorkflowSpec

runner = WorkflowRunner(registry=registry, runtime=runtime, agent=agent_executor)
spec = WorkflowSpec.model_validate(workflow_json)

run = await runner.start_run(spec, inputs, run_id="唯一运行ID")
snapshot = runner.get_run(run.run_id)
events = runner.get_events(run.run_id, after_seq=0)

# 仅在 snapshot.status == "paused" 时，由已授权的人工审批命令触发
run = await runner.resume_run(
    run.run_id, approval_id=snapshot.approval["id"], approved=True,
)
response = asdict(run)  # dataclass → API 响应字典
```

**start_run/resume_run 会等待本段执行结束、失败或暂停，不是立即返回的后台启动接口。** 若 API 需要立即响应，由 C 管理后台任务及其异常；当前没有任务队列。

返回 `RunSnapshot`：

| 字段 | 内容 |
|---|---|
| run_id / workflow_id / workflow_version | 运行与版本标识 |
| status | running / paused / completed / failed |
| state | system / inputs / data / artifacts / tasks / route（route 为内部字段） |
| approval | 暂停时包含 id、node_id、ui_stage_id、prompt、context；否则 None |
| error | 失败时包含 type、message；否则 None |

运行期间 `state.data` 不是逐节点实时快照；以事件展示执行进度。`completed` 只表示图执行结束，不等于业务已闭环；示例看 `state.data.task_status`。

建议 HTTP 映射（**路由尚未实现**）：

| 情况 | 建议处理 |
|---|---|
| RunNotFoundError | 404 |
| RunConflictError：重复 ID、过期审批、非暂停状态恢复 | 409 |
| 协议/校验/输入 ValueError | 422；WorkflowValidationError.issues 可供展示 |
| 工具/图执行失败 | 返回 status=failed 的 RunSnapshot，不作为接口内部异常 |

## 3. 原始事件 → C 投影为 RunEvent / SSE

`get_events(run_id, after_seq=N)` 返回 `seq > N` 的原始事件列表。seq 从 1 开始，**仅在单个 Run 内递增**。

- 公共字段：`seq / type / run_id / timestamp`。
- 可选字段：`node_id / ui_stage_id / capability / error`，不能假定每种事件都有。
- 类型：`run_started / run_finished / run_failed`、`node_started / node_finished / node_failed`、`tool_started / tool_finished / tool_failed`、`approval_required / approval_resolved`。
- `approval_required` 的审批 ID 字段叫 **id**；`approval_resolved` 中叫 **approval_id**。
- C 负责业务文案、字段映射、敏感信息过滤、SSE 与事件持久化。当前没有 state_changed/WorldState 推送，需由模拟世界与投影层补齐。

## 4. 联调入口与限制

```bash
cd multica/backend
uv sync
uv run python -m examples.runner_demo
uv run python -m unittest discover -s ../tests/runtime -v
```

- 示例能力参数/返回值：`backend/examples/demo.py`；流程：`backend/examples/advance_anomaly.json`。
- 示例能力：telemetry.read、history.query、control.apply、task.record。
- 工作流结构协议：`contracts/workflow.schema.json`；静态图与能力校验仍由 Runner 编译阶段执行。
- 当前仅单进程内存运行，无重启恢复、多 worker 协调或持久事件存储；仅换持久化 checkpointer 不足以解决这些问题。
- 审批身份鉴别、权限与审计由 C 实现；Demo 自动批准不能直接用于正式 API。
