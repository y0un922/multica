# 节点级执行模式与多 Agent 编排

版本：WorkflowSpec v1.2

## 1. 目标

Workflow 不再通过一次 Run 的全局开关选择 FakeAgent 或 Pi，而是让**每一个节点在模板中声明执行类型**：

```text
WorkflowSpec
    ├── type: tool     → CapabilityRuntime
    ├── type: pi       → 节点专属 AgentExecutor
    ├── type: decision  → 固定条件路由
    └── type: approval  → 人工确认 / interrupt
```

因此同一条流程可以是：

```text
读取状态        → tool
异常诊断        → pi / Agent A
生成处置方案    → pi / Agent B
人工确认        → approval
执行调整        → tool
效果复核        → pi / Agent C
```

这就是本项目当前阶段的多 Agent 编排方式：Agent 通过 Workflow State 和显式数据绑定协作，而不是通过私聊或共享隐式记忆协作。

## 2. 节点类型

| `type` | 内部 `kind` | 执行者 | 是否可以产生副作用 |
|---|---|---|---|
| `tool` | `capability` | `CapabilityRuntime` | 取决于 Capability；写操作受审批约束 |
| `pi` | `agent_task` | 节点专属 `AgentExecutor` | Agent 只能调用白名单内的非副作用能力 |
| `decision` | `decision` | 内置比较器 | 否 |
| `approval` | `approval` | LangGraph interrupt + 人工系统 | 它本身不执行设备动作 |

`type` 是模板对外的业务协议；`kind` 是 Runtime 内部兼容字段。v1.2 推荐只写 `type`，解析器会归一化为内部 `kind`。

未知 `type`、`type` 与 `kind` 冲突、v1.2 节点缺少 `type`，均必须校验失败，不能默认当作 Tool。

### 2.1 Tool 节点

```json
{
  "id": "read_status",
  "name": "读取设备状态",
  "type": "tool",
  "capability": "telemetry.read",
  "inputs": {
    "equipment_id": {
      "type": "ref",
      "path": "$.inputs.equipment_id"
    }
  },
  "outputs": {
    "torque": "$.data.torque"
  }
}
```

执行过程：

```text
解析 inputs
    ↓
Capability 输入 Schema 校验
    ↓
CapabilityRuntime.invoke
    ↓
ToolResult 状态检查
    ↓
输出 Schema 校验
    ↓
写回 RunState.data
```

Tool 节点适用于确定性数据读取、设备操作、任务创建、报告生成等能力。

### 2.2 Pi 节点

```json
{
  "id": "diagnose",
  "name": "异常诊断",
  "type": "pi",
  "goal": "根据当前参数和历史数据判断异常原因",
  "capabilities": ["history.query"],
  "pi": {
    "provider": "optional-provider",
    "model": "optional-model",
    "timeout": 120
  },
  "inputs": {
    "torque": {
      "type": "ref",
      "path": "$.data.torque"
    }
  },
  "outputs": {
    "diagnosis": "$.data.diagnosis"
  },
  "input_schema": {
    "type": "object",
    "properties": {
      "torque": {"type": "number"}
    },
    "required": ["torque"],
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "diagnosis": {"type": "string"}
    },
    "required": ["diagnosis"],
    "additionalProperties": false
  }
}
```

Pi 节点执行过程：

```text
解析 Node inputs
    ↓
Agent input Schema 校验
    ↓
agent_factory(node) 创建本节点 AgentExecutor
    ↓
PiAgentExecutor 启动独立 Pi 会话
    ↓
Pi 通过 CapabilityBridge 调用白名单能力
    ↓
Pi submit_result
    ↓
Agent output Schema 校验
    ↓
写回 RunState.data
```

每次节点执行使用独立 Pi 进程和临时会话。多个 Pi 节点可以使用不同 provider/model，也可以全部继承服务默认配置。

`pi` 配置只允许：

```json
{
  "provider": "...",
  "model": "...",
  "timeout": 120
}
```

模板不能覆盖：

- Pi 启动命令；
- 配置目录；
- API Key；
- shell 命令；
- Extension 路径。

这些属于服务端安全配置。

## 3. 节点级 Agent 工厂

Compiler 保留旧的共享 Agent 接口，同时新增节点级工厂：

```python
from runtime.workflow import compile_workflow

graph = compile_workflow(
    spec,
    registry=registry,
    runtime=capability_runtime,
    agent_factory=lambda node: build_agent_for_node(node),
    checkpointer=checkpointer,
)
```

工厂接收完整的 `AgentNode`：

```python
def build_agent_for_node(node):
    return PiAgentExecutor(
        command=pi_command,
        provider=node.pi.provider or default_provider,
        model=node.pi.model or default_model,
        timeout=node.pi.timeout or default_timeout,
        agent_dir=server_agent_dir,
    )
```

工厂的职责是**创建执行器**，不是执行模型调用。编译阶段不应产生模型请求、工具请求或设备副作用。

Runner 通过同一工厂装配工作流：

```python
runner = WorkflowRunner(
    registry=registry,
    runtime=runtime,
    agent_factory=build_agent_for_node,
)
```

如果工作流没有 `pi` 节点，可以只提供 ToolRuntime；不会创建 Pi Agent。

## 4. 配置继承优先级

节点参数优先级如下：

```text
node.pi.provider/model/timeout
    ↓ 未填写
服务启动参数 --provider / --model / --timeout
    ↓ 未填写
Pi 自身配置、默认模型选择和 models.json
```

Pi 配置目录优先级：

```text
--agent-dir
    ↓ 未填写
PI_CODING_AGENT_DIR
    ↓ 未填写
当前操作系统用户的 ~/.pi/agent
```

Windows 下服务自动把 PATH 中的 `pi.cmd` 解析为 npm 包的 `bin.pi`，再使用 Node 启动，不使用 `shell=True`。因此：

```powershell
uv run python -m demo_workbench.server --enable-pi
```

即可复用当前用户已有的 `models.json`。

## 5. Tool 与 Pi 的替换规则

### 5.1 可以替换的节点

一般只读、分析类节点可以在 Tool 和 Pi 之间切换：

```text
telemetry.read
history.query
knowledge.search
异常诊断
方案生成
效果复核
```

切换时必须保持：

1. 相同或兼容的输入绑定；
2. 相同的输出字段；
3. 相同的输出 Schema；
4. 相同的后续边和路由语义。

例如 Tool 和 Pi 都必须输出：

```json
{
  "abnormal": true
}
```

后续 decision 节点才可以继续使用：

```text
$.data.abnormal
```

### 5.2 不应直接替换的节点

#### 人工审批

`approval` 不能改成普通 Pi 节点来绕过人工确认。

如果业务要求自动决策，应显式增加一个 `pi` 节点输出建议，再保留 `approval`：

```text
Pi 风险评估
    ↓
人工确认
    ↓
Tool 执行动作
```

#### 有副作用的 Tool

`control.apply`、`task.create`、`task.record` 等写操作不应直接交给 Pi 自由调用。推荐：

```text
Pi 生成结构化方案
    ↓
approval
    ↓
tool: control.apply
```

编译器和校验器继续禁止 Agent 白名单包含 `side_effect` 或 `requires_approval` 能力。

## 6. 模板切换示例

### Tool 版本

```json
{
  "id": "diagnose",
  "name": "异常诊断",
  "type": "tool",
  "capability": "diagnose_fault",
  "inputs": {
    "torque": {"type": "ref", "path": "$.data.torque"}
  },
  "outputs": {
    "diagnosis": "$.data.diagnosis"
  }
}
```

### Pi 版本

```json
{
  "id": "diagnose",
  "name": "异常诊断",
  "type": "pi",
  "goal": "结合当前参数、历史工况和知识案例完成异常诊断",
  "capabilities": ["history.query", "knowledge.search"],
  "inputs": {
    "torque": {"type": "ref", "path": "$.data.torque"}
  },
  "outputs": {
    "diagnosis": "$.data.diagnosis"
  },
  "input_schema": {
    "type": "object",
    "properties": {"torque": {"type": "number"}},
    "required": ["torque"],
    "additionalProperties": false
  },
  "output_schema": {
    "type": "object",
    "properties": {"diagnosis": {"type": "string"}},
    "required": ["diagnosis"],
    "additionalProperties": false
  }
}
```

两者的节点 ID、输入来源和输出路径相同，因此下游流程不需要改变。

## 7. 多 Agent 的协作方式

当前不使用 Pi-to-Pi 直接消息。Agent 之间通过 Workflow State 协作：

```text
Agent A：异常诊断
    输出 $.data.diagnosis
            ↓
Agent B：方案生成
    输入 $.data.diagnosis
    输出 $.data.changes
            ↓
Agent C：效果复核
    输入 $.data.before / $.data.after
    输出 $.data.verification
```

这样可以对每个 Agent 单独做：

- 输入校验；
- 工具白名单；
- 输出校验；
- 超时和失败处理；
- 运行事件；
- 模型替换。

这也是当前系统适合 Demo 和后续生产接入的原因：工作流负责确定性编排，Agent 负责局部任务。

## 8. 事件与展示

每个节点事件包含执行类型：

```json
{
  "type": "node.started",
  "run_id": "run-001",
  "node_id": "diagnose",
  "ui_stage_id": "diagnosis",
  "executor_type": "pi"
}
```

Tool 节点：

```json
{
  "type": "node.started",
  "node_id": "read_status",
  "executor_type": "tool"
}
```

前端可以据此显示：

```text
异常诊断 · Pi Agent
读取设备状态 · Tool
人工确认 · Approval
```

注意：`executor_type` 只说明节点执行方式，不应暴露思维链。Pi 的工具调用只显示行为、能力名、参数摘要和结果状态。

## 9. 校验清单

发布或编译前必须检查：

- v1.2 的每个节点都有合法 `type`；
- `type` 与旧 `kind` 一致；
- Tool 节点的 capability 已注册；
- Pi 节点有 input/output Schema；
- Pi 使用的能力都在 capabilities 白名单内；
- Pi 不能使用副作用或需审批的能力；
- 输入绑定有上游生产者；
- 输出路径没有覆盖冲突；
- decision / approval 的分支完整；
- 工作流无非法循环；
- `approval` 到写操作的路径不能被绕过；
- 全部节点都可从入口到达；
- 节点输出与下游输入类型兼容。

## 10. 测试验收

### 协议测试

```powershell
cd multica/backend
uv run python -B -m unittest discover -s ../tests/runtime -v
```

重点测试：

- `type=tool` 解析为 Tool；
- `type=pi` 解析为 Agent；
- 未知 type 失败；
- type/kind 冲突失败；
- v1.2 缺 type 失败；
- 旧 v1.0/v1.1 模板仍可运行；
- Pi 节点必须有结构化契约；
- Pi 不能暴露写能力；
- 两个 Pi 节点使用不同配置且按顺序运行；
- Tool 节点不会创建 Agent。

### Demo 测试

```powershell
uv run python -B -m unittest discover -s ../tests/demo -v
```

### 手工验收

1. 默认模板全是 Tool，运行不启动 Pi；
2. 将一个节点改为 Pi，只该节点启动 Pi；
3. 将两个节点改为 Pi，两个节点分别执行；
4. 其余 Tool 节点仍直接调用 ToolRuntime；
5. 审批前不调用 `control.apply`；
6. Pi 输出写回 Workflow State；
7. Pi 失败只使当前 Run 失败，不影响其他已完成 Run；
8. Tool / Pi 切换后，下游节点仍能消费相同字段；
9. 页面节点图显示 `tool / pi / approval / decision`；
10. 未启用 Pi 时，含 `type=pi` 的流程不能偷偷降级为 Tool 或 FakeAgent。

## 11. 当前明确不做

- Pi Agent 之间直接聊天；
- 共享长期 Agent Memory；
- Pi 修改 Workflow 图；
- Pi 直接调用任意 Python 或 shell；
- Pi 绕过人工审批执行写操作；
- 未经幂等协议支持的写操作自动重试；
- 并行 Pi 节点、subflow、wait 和循环；
- 生产级持久化 Runner、事件流和鉴权。
