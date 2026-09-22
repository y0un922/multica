# Runner 运行管理层

角色 B 的应用服务；角色 C 的 API/命令层调用此接口，不直接操作 LangGraph 图。这里不实现 HTTP、SSE、审批鉴权或业务事件投影。

## 使用

```python
from runtime.execution import WorkflowRunner

runner = WorkflowRunner(registry=registry, runtime=capability_runtime, agent=agent)

# 执行到完成、失败或等待审批，不是立即返回的后台调度接口。
run = await runner.start_run(spec, inputs, run_id="globally-unique-id")

# 查询是同步接口（沿用已有接口）；执行/恢复是异步接口。
snapshot = runner.get_run(run.run_id)
events = runner.get_events(run.run_id, after_seq=0)

if snapshot.status == "waiting_approval":
    run = await runner.resume_run(
        run.run_id,
        approval_id=snapshot.approval["id"],
        approved=True,
    )
```

若 API 需要先返回 run_id，再由其管理后台任务：

```python
pending = runner.create_run(spec, inputs)  # 同步编译、校验、注册，不调用工具
# 将 pending.run_id 交给由应用负责生命周期的任务调度器
result = await runner.execute_run(pending.run_id)
```

不要为每个 HTTP 请求新建 Runner。`start_run` 相当于 create_run + execute_run，只有 pending 状态能首次执行。

## 统一状态

```text
pending → running → waiting_approval → running → completed
              └───────────────┴──────────────→ failed
```

**接口变更：旧的 `paused` 已改为 `waiting_approval`**；接入方同步更新状态判断，没有保留 paused 别名。

`RunSnapshot` 包含：

- run_id、workflow_id、workflow_version；
- status、state（包括 system/inputs/data 等）；
- approval（节点、展示阶段、prompt/context 与 interrupt id）；
- error（type/message）；
- created_at、updated_at（UTC 时间字符串）。

snapshot.status 与 snapshot.state.system.status 一致。所有返回值为深拷贝，修改响应不能改变内部执行状态。结构契约位于 `multica/contracts/run_snapshot.schema.json`。

### 数据更新与错误

- 创建前完成协议/图/输入校验；错误向外抛出，不注册 Run。
- 执行过程中消费 LangGraph values 流，逐节点更新**已完成节点的数据**；同时以原始事件更新 current_node。等待后续慢工具时可以查询到前面节点的结果。
- 节点当前未提交的数据不对外显示。schema 校验失败的节点数据不写回。
- 工具、Agent、Schema、绑定或图执行失败：返回 failed 快照，保留最后提交的数据和错误；不会自动重试。
- 任务取消：更新 failed/CancelledError 后继续向调用方抛 CancelledError；保留已观察到的节点提交，不代表外部副作用已撤销。
- RunNotFoundError：未找到运行记录。
- RunConflictError：重复 run_id、错误状态执行/恢复、过期审批 ID，或仅有存储记录而图未加载。
- `approved` 必须是真正的 bool，不能传入字符串 `"false"`。

应用状态是 Runner 投影，不反向修改 LangGraph checkpoint：等待审批和失败时，以 Runner 快照为准。业务是否闭环看 `data.task_status` 等业务字段，而不是只看 completed（被拒绝后正常转人工，也可以完成工作流）。

## 审批和并发

使用 LangGraph interrupt ID 作为 approval_id。每个运行的 asyncio.Lock 串行化执行和恢复；同一审批并发批准只有一次有效，第二次返回冲突。后续节点产生新的审批 ID，旧审批不能消费新请求。

目前只支持一个待审批中断，与单活跃路径 DAG 一致。API 层负责身份鉴别、审批权限、审批人审计与设备侧授权，本层不把 bool 当作安全凭证。

## 存储边界

```python
from runtime.execution import InMemoryRunStore, WorkflowRunner

store = InMemoryRunStore()
runner = WorkflowRunner(registry=registry, runtime=runtime, agent=agent, store=store)
```

`RunStore` 为快照存储端口，提供 create/save/get；create 必须原子拒绝重复 ID。默认 InMemoryRunStore 保存深拷贝，用于单进程 Demo 和测试。当前端口同步、必须快速非阻塞；正式数据库持久化应演进为 async repository，不能把慢数据库 IO 直接放入此端口阻塞事件循环。

RunStore 只存快照，不存工作流版本、已编译图、锁、原始事件或 checkpoint。已有快照可查询，但新 Runner 不会据此自动重建图；不会允许以相同 ID 覆盖存储记录。

checkpointer 可注入，默认 InMemorySaver。**替换 RunStore 或注入持久化 checkpoint 都不等于实现进程重启恢复。** 后续还需版本仓库、运行上下文恢复、图重建、事件持久化和跨进程锁。

共享 checkpointer 时 run_id/thread_id 必须全局唯一；仅共享 checkpointer 而不共享运行元数据仍可能造成 ID 冲突，当前不支持该部署方式。

## 事件

收集编译器原始事件，补充 approval_required / approval_resolved；为单个 Run 分配 seq（从 1 开始），支持 after_seq 增量读取。普通执行失败只保留一个 run_failed，不重复投影编译器已有失败事件。

这些是内存原始事件，不是角色 C 拥有的前端 RunEvent，不提供 SSE 或可靠持久化投递。错误信息和审批 context 可能含敏感业务数据，对外展示前应由平台层过滤。

## 已知限制

- 只有单进程锁，不支持多 worker 同时恢复。
- 不保证写操作 exactly-once；工具必须提供幂等、防重及状态核对。
- 内存存储和事件没有历史清理、分页上限或长期可靠性保证。
- 没有取消 API、失败重试、超时/预算策略或后台任务队列。
- 没有实现存储故障下的事务一致性；默认存储用于 Demo，不应当作高可用数据库服务。

## 测试与示例

在 `multica/backend/` 下：

```bash
uv run python -m examples.runner_demo
uv run python -m unittest discover -s ../tests/runtime -v
```

runner_demo 使用协议 1.1、有类型的 FakeRegistry 和内存实现。自动批准仅用于展示，生产必须由人工确认命令触发 resume_run。
