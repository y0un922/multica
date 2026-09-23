# 节点级 Workflow Draft 联调台

验证 **模板 → 节点执行配置 → 校验编译 → 业务图 → 混合 Tool/Pi 执行 → 审批 → 模拟复核**。

## 启动

```powershell
cd D:\CS\TBM\multica\backend
uv sync
uv run python -m demo_workbench.server --enable-pi
```

打开 **http://127.0.0.1:8010**。Windows 自动解析 PATH 中 npm 安装的 Pi，用 Node 启动；无需手动指定 CLI。默认继承 `PI_CODING_AGENT_DIR`，未设置时使用 `~/.pi/agent` 下现有配置。

仅测试工具时，可以省略 `--enable-pi`。默认模板的任务节点都是 `tool`，不会调用模型。

## 页面操作

1. 页面加载模板，并显示 **节点执行配置**。
2. 对“读取设备状态”“生成方案”“重新读取参数”，分别选择 **Tool 直接执行** 或 **Pi Agent**。
3. Pi 节点可编辑目标、工具白名单、provider、model、timeout。模型/超时留空继承服务默认值。
4. 点击 **校验并编译**，查看图中每个节点的执行类型。这一步不会请求模型或执行工具。
5. 点击 **运行已编译草稿**。含 Pi 节点时提示可能计费。
6. 查看工具调用事件、状态回写和待审批方案，选择批准或拒绝。

推荐的多 Agent 测试：读取节点设为 Pi、方案节点设为 Pi、复核读取保持 Tool。这样由两个独立 Pi 节点协作，其他节点不受影响。

工具模式的方案节点调用 Demo 能力 `proposal.generate`，确定性返回 `changes.torque=3.5`。全 Tool 模式批准后预期 `4.12 → 3.5`，`task_status=closed`。拒绝时不执行 `control.apply`，记录 `needs_attention`。正常场景在判断节点后结束。

规则 `decision` 和人工确认 `approval` 保留独立控制语义。带副作用的工具暂不提供 Pi 切换，避免绕过已有审批和 Agent 只读能力边界。若要让 Pi 做判断，应使用 `pi` 节点输出布尔字段，再由 `decision` 读取并路由。

## 模板协议 v1.2

每个节点明确声明 `type`：

| type | 执行方式 | 内部兼容 kind |
|---|---|---|
| `tool` | ToolRuntime 直接调用 capability | capability |
| `pi` | 节点专属 AgentExecutor，经桥接使用白名单工具 | agent_task |
| `decision` | 安全条件比较及分支 | decision |
| `approval` | 人工确认 / interrupt | approval |

未知 type 报错，不会静默按 tool 执行。v1.2 必须有 type；旧 v1.0/v1.1 的 kind 模板仍兼容。type 与 kind 同时出现时必须一致。v1.2 仍要求输入/状态 Schema 和 Pi 节点输入/输出契约。

```json
{
  "id": "diagnose",
  "name": "生成方案",
  "type": "pi",
  "goal": "调用 history.query 并返回符合 Schema 的方案",
  "capabilities": ["history.query"],
  "pi": {"provider": "my-provider", "model": "my-model", "timeout": 120},
  "inputs": {"torque": {"type": "ref", "path": "$.data.torque"}},
  "outputs": {"changes": "$.data.changes"},
  "input_schema": {"type": "object", "properties": {"torque": {"type": "number"}}, "required": ["torque"]},
  "output_schema": {
    "type": "object",
    "properties": {"changes": {"type": "object", "properties": {"torque": {"type": "number"}}, "required": ["torque"]}},
    "required": ["changes"]
  }
}
```

切为工具时使用同样的数据绑定，配置工具而不是 Agent 参数：

```json
{
  "id": "diagnose",
  "name": "生成方案",
  "type": "tool",
  "capability": "proposal.generate",
  "inputs": {"torque": {"type": "ref", "path": "$.data.torque"}},
  "outputs": {"changes": "$.data.changes"}
}
```

不是只改 type 就忽略其余不相容字段：页面选择器会替换节点专属配置并保留输入输出绑定。手工编辑时 Pi 节点需移除 `capability`，Tool 节点需移除 goal/capabilities/pi/agent schemas。服务端严格拒绝未知字段。

`pi` 设置只接受 provider/model/timeout，不能从模板覆盖子进程命令、配置目录或凭据。

## 编译与 Runner

Compiler/Runner 新增 `agent_factory(node)` 注入端口，每个 Pi 节点创建独立 executor。factory 只装配对象，不得请求模型。原有 `agent=` 共享执行器端口保留，供旧集成和测试使用。

Demo factory 将节点模型配置合并到服务默认配置：

```text
node.pi.provider/model/timeout
    优先于
服务 --provider / --model / --timeout
    未指定 provider/model 时
Pi 自身的配置和默认选择逻辑
```

执行期仍执行白名单、输入输出 Schema 和状态绑定检查。Pi 无权直接决定图拓扑或审批结果。Agent 之间经 Workflow 状态交接，不共享 Pi 会话。

## HTTP API

所有 POST 要求 JSON 和模板接口返回的 `X-Demo-Token`。仅本机同源使用。

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/demo/template` | 草稿、节点切换选项、Pi 开关和 token |
| POST | `/demo/compile` | workflow 校验编译，返回 compiled_id 和业务图 |
| POST | `/demo/runs` | compiled_id、scenario、inputs；后台执行返回 202 |
| GET | `/demo/runs/{id}` | snapshot、node_executors、调用记录、模拟世界 |
| GET | `/demo/runs/{id}/events?after_sequence=N` | A/工具共享序号的事件增量 |
| POST | `/demo/runs/{id}/confirm` | confirmation_id、decision=accepted/rejected |

**Run 级 `agent=fake/pi` 已移除，传入时明确报错。** 执行方式只取自编译后的节点 type，不能在运行请求里覆盖。快照 `agent` 仅是 `tool/mixed` 汇总，准确配置见 `node_executors`。节点事件包含 executor_type。

Pi 未启用仍可预览编译，但含 Pi 节点的流程不能运行，不会悄悄降级为 FakeAgent。

## 测试

```powershell
uv run python -B -m unittest discover -s ../tests/runtime -q
uv run python -B -m unittest discover -s ../tests/demo -v
```

覆盖新旧协议、未知/冲突类型、独立节点配置、两个 Agent 状态交接、工具直调、Schema 错误、审批约束，以及 HTTP → RPC 子进程 → Bridge → 工具 → submit_result → 审批恢复的端到端测试。自动化使用模拟 RPC 对端，不请求付费模型。

## 限制

- 仅本机开发 Demo，不是正式平台接口；默认监听 127.0.0.1:8010。
- 数据、事件、checkpoint 全部在内存，退出即丢失；刷新页面不自动恢复运行视图。
- 页面使用 SVG 和轮询，不是完整 React Flow 编辑器/SSE。
- 审批直接调用 Runner，不替代角色 C 的鉴权、确认存储和可靠事件投递。
- 每进程最多保存 100 次编译、100 个 Run。
- 仍是顺序/分支多 Agent，不支持并行、持久 Agent 会话或任意写工具交给模型。
- 每次节点执行启动临时 Pi 进程；配置要求详见 [Agent 说明](../runtime/agent/README.md)。
