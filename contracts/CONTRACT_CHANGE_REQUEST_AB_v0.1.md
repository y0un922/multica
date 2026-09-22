# CONTRACT_CHANGE_REQUEST：A/B v0.1 装配细节

**状态：待 Backend B review，未视为双方已批准。**

Requester: Backend A（角色 B）

Current contract:

- `ToolRuntime.invoke(tool_name, arguments, ToolContext) -> ToolResult`
- `EventBus.publish(AgentEvent)`；同一 Run 的 sequence 必须单调递增。
- A 发 confirmation.requested，B 保存/处理确认并发 confirmation.resolved，A 恢复图。
- B 提供 Run 状态记录基础设施、Tool Catalog；未给出完整 Tool Schema。

Problem:

1. AgentEvent.sequence 为必填，但未说明原子分配与多生产者有序发布机制。
2. Confirmation 事件 payload 没有字段约定；发布请求与保存确认的完成语义、结果投递机制未说明。
3. A 定义生命周期而 B 保存状态，但尚无具体写入接口或事务语义。
4. 仅 query_tbm_status 有输出示例，其余工具 Schema 无法从文档推断。
5. 副作用工具重试/幂等与跨进程恢复没有约定。

Required behavior:

- 所有 A/B 事件共用序号来源，不允许 A 自行编号后与 B 合并。
- 请求可见时 Confirmation 已可查询；确认结果须在请求暂停/checkpoint 就绪后可靠投递。
- 确认事件须能关联到 run_id、confirmation_id 和决定；重复投递不得重复调用动作。
- B 无需依赖 LangGraph、私有 Workflow State、Planner 或图拓扑。
- schema 校验失败不能被默默降级为无校验。

Minimal proposed change:

## 1. 不改 EventBus.publish，确认装配策略

A 已提供私有 `SequencedEventPublisher(event_bus, shared_sequence_scope)`。

- scope 是装配依赖，不是新增公共 EventBus 方法。
- `async with shared_sequence_scope(run_id) as sequence` 保持序号分配及发布顺序。
- 所有生产者共用平台的序号/事务机制。B 也可提供等价的 A-private EventPublisher 适配器。
- publish 成功返回表示达到双方约定的持久化边界；生产推荐 durable outbox。

需 B 确认具体分配实现和成功返回语义。单进程测试使用共享 InMemoryEventBus，不作为生产承诺。

## 2. Confirmation 事件 payload 的候选约定

建议 request/resolved 均携带现有完整 Confirmation 模型（不增加该模型字段）：

```json
{
  "id": "confirmation-id",
  "run_id": "run-id",
  "node_id": "confirm",
  "prompt": "Create a task?",
  "context": {},
  "status": "accepted",
  "created_at": "2026-01-01T00:00:00+00:00",
  "resolved_at": "2026-01-01T00:01:00+00:00"
}
```

A 的 `handle_event` 按此候选 payload 工作；如 B 使用更小 payload，需在装配适配器中验证并转换，不能让 Workflow 猜字段。

- B 保存决定、发事件，不直接调用 LangGraph resume。
- A 消费已认证平台事件，再调用自己的 resume_run。
- 禁止 publish 内同步回调并等待 Runner，防止重入运行锁。
- 用户拒绝的默认 Demo 语义：completed，不创建 Task；决定留在 B Confirmation 数据里。

## 3. Run 持久化

A 当前提供私有 `RunStore.create/save/get` 装配端口，B 可适配；不能将其当成双方已经批准的新公共接口。需要确认异步数据库访问和失败语义，慢数据库 IO 不可直接阻塞同步端口。

## 4. Tool Catalog 与 Schema

请 B 提供 `GET /api/tools` 完整真实响应及每项 I/O Schema。

A 的 `CatalogRegistry` 按目录公开字段转换，不 import B 内部服务。当前编译器只支持已声明的 JSON Schema 子集（见 workflow/SCHEMA.md）；不支持的关键词明确失败。需要针对 B 实际 Schema 评估是否扩展，不能静默去掉 $ref/约束。

`examples/ab_demo.py` 的 Tool Schema 仅为 fixture；不视为 B 已接受的正式 Schema。当前固定示例根据自身 fixture 做数据绑定；不声称能适配任意 Schema。

## 5. 幂等与重启恢复（后续确认）

当前不向 ToolContext 擅自添加幂等字段，也不盲重试写工具。需要 B 提供动作幂等/查询补偿约定后再启用恢复重放和动作重试。

Affected modules:

- A：runtime/execution/contracts.py、events.py、runner.py、workflow/catalog.py。
- B：EventBus、Confirmation Service/API、事件结果投递、Run 状态存储、Tool Catalog。
- Frontend：RunStatus、AgentEvent 字段和点号事件名、Confirmation 模型。

Migration required:

- 旧 waiting_approval → waiting_confirmation，删除公共 pending。
- Snapshot.approval → confirmation；approval_id/approved → confirmation_id/decision。
- 旧 seq/after_seq → sequence/after_sequence。
- 旧 run_started 等内部名称转换为 run.started 等公共类型。
- 旧 ToolResult.value/字符串 error → data/结构化 ToolError。
- A 不再发布 tool.*、task.*、confirmation.resolved。

Test impact:

A 已有合同测试（假 B）覆盖异常/正常/拒绝、重复确认、不匹配 ID、错误上下文、工具失败、动作不盲重试、事件顺序和发布故障。

双方确认后仍需用真实 B 的 Catalog、Runtime、EventBus 和 Confirmation API 运行同一条 Golden Path，验证持久化、重连补投和鉴权。**当前没有完成真实 B 的 E2E。**
