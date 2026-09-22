# Runner：Backend A 生命周期与 Confirmation

以根目录 `BACKEND_AB_INTERFACE.MD` v0.1 为对齐依据。A 负责图、路由、暂停/恢复和执行决策；B 负责工具、数据、Confirmation API、事件存储与 SSE。这里不实现 HTTP 或身份鉴别。

## 启动与查询

```python
from pydantic import TypeAdapter
from runtime.execution import WorkflowRunner, RunSnapshot

runner = WorkflowRunner(
    registry=registry, runtime=tool_runtime, agent=agent,
    store=run_store, event_publisher=shared_event_publisher,
)
run = await runner.start_run(
    spec, inputs, run_id="unique-run-id",
    project_id="project", user_id="user", trace_id="trace",
)
snapshot = runner.get_run(run.run_id)
response = TypeAdapter(RunSnapshot).dump_python(snapshot, mode="json")
```

`start_run` 执行到完成、失败或确认暂停，不是立即返回的后台调度接口。API 如果需要先返回 ID：

```python
created = runner.create_run(spec, inputs)  # 校验、编译、注册，不调用工具
# 交给应用负责生命周期的调度器，不能每次 HTTP 请求新建 Runner。
result = await runner.execute_run(created.run_id)
```

公共 RunStatus 只有：

```text
running → waiting_confirmation → running → completed
    └──────────────────┴──────────────────→ failed
```

`create_run` 注册后即为 running，表示已接受的活动 Run；是否已经首次调度是 A 的私有标记，不再暴露 pending。`run.started` 在实际执行时发布。重复 `execute_run` 返回冲突。

`RunSnapshot` 包含 run_id、workflow_id/version、status、state、confirmation、error、created_at/updated_at。`confirmation` 是公共 `Confirmation` Pydantic 模型，暂停时非空；完成/失败时为空。不要对含该模型的 dataclass 直接 `json.dumps(asdict(...))`，使用上面的 TypeAdapter 序列化。

- status 与 state.system.status 始终同步。
- 逐节点保存已提交数据；慢工具执行期间可以查询前一节点的输出。
- 所有查询结果是深拷贝。
- completed 只代表图结束，不代表任务已办结或现场异常已消除。

## 人工确认：B 保存，A 恢复

1. A 到达内部 approval 节点，LangGraph 保存 interrupt/checkpoint。
2. Runner 生成独立 Confirmation ID，保存 waiting_confirmation 快照。
3. A 发布 confirmation.requested；请求发布前快照与恢复位置已存在。
4. B 保存 Confirmation，通过 API 接受 accepted/rejected，发布 confirmation.resolved。
5. **A 的事件消费者**接收结果并恢复图，B 不导入/调用 LangGraph。

```python
# 运行在 A 的可信事件消费入口，resolved_event 已由平台验证来源。
final = await runner.handle_event(resolved_event)
```

当前适配约定：请求和结果事件的 payload 是完整 Confirmation 字典。该约定需 B 确认，或在接入适配器中转换。它不是新增的公共必填字段。见 `contracts/CONTRACT_CHANGE_REQUEST_AB_v0.1.md`。

底层 A-owned 恢复方法：

```python
final = await runner.resume_run(
    run_id, confirmation_id=confirmation.id, decision="accepted",  # 或 rejected
)
```

此方法仅用于已认证的确认结果，不是直接暴露给未授权用户的 API。`approved: bool` 已从 Runner 公共接口删除；图内部仍可使用 bool 路由，这不是 B 的接口。

### 并发与幂等

- 每个 Run 用锁串行化执行与恢复。
- 同一 ID、同一决定的重复/并发结果只执行一次；后续投递返回当前快照。
- 冲突决定、错误 Run、错误节点、错误/未知 Confirmation ID 被拒绝。
- 旧确认重投递不能消费后续的新确认。
- Confirmation ID 独立于 LangGraph interrupt ID；平台无需知道图的恢复实现。
- A 不发布 confirmation.resolved，不重复保存 B 的 ToolCall Trace。
- 队列消费者必须在 publish 之外异步投递结果，不能在 EventBus.publish 中同步重入 Runner，否则会等待仍持有的 Run 锁。

拒绝如何路由由工作流决定。新 Demo 的 rejected 分支直接结束 Run，不创建任务；B 的 Confirmation 保留 rejected 记录。没有新增 cancelled RunStatus。

## 公共事件

A 发布 `run.started / node.started / node.completed / agent.thinking / confirmation.requested / run.completed / run.failed`。`agent.thinking` 仅包含任务/行为说明，不包含思维链。

公共模型：

```text
AgentEvent(id, run_id, sequence, type, timestamp, payload)
```

Runner 将编译器内部事件转换为这个模型。终态事件在对应快照保存后发布；确认节点的开始事件不会因 interrupt 重放而重复。

B 发布 tool.*、task.*、confirmation.resolved。所有生产者必须共用事件序号分配器；A 不维护独立全局计数器。

### 接入 EventBus.publish

```python
from runtime.execution import SequencedEventPublisher

publisher = SequencedEventPublisher(event_bus, shared_sequence_scope)
runner = WorkflowRunner(..., event_publisher=publisher)
```

`event_bus` 仍只需公共 `publish(AgentEvent)`。`shared_sequence_scope(run_id)` 是 **A 私有适配器的装配依赖**：异步上下文管理器提供下一个 sequence，并在 publish 完成前维持同一 Run 的发布顺序。所有 A/B 生产者必须使用同一平台机制，不能各自分配计数器。真实实现和持久化事务需与 B 确认，未直接修改公共 EventBus。

默认 `InMemoryEventBus` 只用于单进程 Demo。新 Demo 显式给 A 与假 B 注入同一实例。

```python
# 只读到 A 发布的事件，因此 sequence 可能有间隔。
a_events = runner.get_events(run_id, after_sequence=0)
# 完整 A/B Stream 应从 B 的事件查询 API / SSE 获取。
```

没有旧 `seq/after_seq` 别名。`get_events` 不是持久事件服务。

## 错误和存储

- 工具三态由 B 返回，当前 A 默认 fail，不自动重试副作用操作。
- 工具失败快照保留 type/message/tool_name/status/category/code，路由不解析 message。
- Schema/输入/图错误在创建阶段抛出，不注册 Run。
- 执行错误返回 failed 快照；任务取消记录 failed 后抛出 CancelledError。
- 事件发布不可用时尽量保存 failed 快照；如果失败事件也不能发布，异常向调用方传播，不伪装为已成功投递。
- RunNotFoundError：未找到；RunConflictError：重复 Run、错误恢复/执行状态或未加载图。

`RunStore` 为 A 私有快照存储端口，create/save/get 的实现由平台注入。默认 InMemoryRunStore 为演示实现；同步端口不得执行阻塞的慢数据库 IO。持久化装配需要双方确认。

**当前只保证单进程内运行：**

- 不支持进程重启后的图重建、Confirmation 去重恢复或多 worker 竞争恢复。
- 仅替换 checkpointer 并不能获得重启恢复。
- 不保证外部副作用 exactly-once；工具仍需幂等和结果核对。ToolContext 当前没有幂等键，不擅自添加。
- RunStore/checkpoint/EventBus 之间尚无跨存储事务或 durable outbox。
- 无后台调度器、取消 API、自动 retry/fallback 策略、数据保留策略。

## 运行验证

```bash
cd multica/backend
uv run python -m examples.ab_demo
uv run python -m unittest discover -s ../tests/runtime -v
uv run python -m scripts.export_contracts
```

ab_demo 是真实 A Runner/Compiler + 明确标注的假 B。覆盖 torque_anomaly、normal、accepted/rejected、重复投递和事件排序。示例自动确认仅用于演示，不替代生产人工授权。
