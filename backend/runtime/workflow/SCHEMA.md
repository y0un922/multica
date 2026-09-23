# 工作流数据 Schema（协议 1.1 / 1.2）

## 版本与兼容性

- `spec_version: "1.0"` 保持兼容：Schema 可省略；一旦声明，仍会执行对应的静态检查和运行时校验。
- `spec_version: "1.1"` 为有类型模式：必须声明工作流 `input_schema`、`state_schema`；每个 Agent 节点必须声明 `input_schema`、`output_schema`；所有引用的 Capability（包括 Agent 可用工具）必须提供输入和输出 Schema。
- `spec_version: "1.2"` 继承 1.1 的全部类型检查，并要求每个节点显式声明 `type`：`pi` / `tool` / `decision` / `approval`。解析时归一化为原有内部 kind，未知 type 或 type/kind 冲突会失败。Pi 节点新增 `pi.provider/model/timeout` 可选配置，由节点级 `agent_factory(node)` 装配。完整示例和切换规则见 [节点级 Demo](../../demo_workbench/README.md)。
- 工作流业务版本 `version` 与协议版本独立。不要直接修改运行中版本。
- 老字段 `required_inputs` 继续支持，与 `input_schema.required` 取并集；新工作流建议仅使用 Schema.required。

示例：[advance_anomaly_typed.json](../../examples/advance_anomaly_typed.json)。

## DataSchema 支持范围

这是**显式受限的 JSON Schema Draft 2020-12 子集**，不是任意 JSON Schema 引擎。

| 字段 | 含义 |
|---|---|
| `type` | 必填，object / array / string / number / integer / boolean / null |
| `properties` | object 的属性名 → DataSchema |
| `required` | object 内必需属性；必须在 properties 中声明 |
| `additionalProperties` | bool，默认 true；建议业务契约显式设 false |
| `items` | array 元素的 DataSchema，数组必须声明 |
| `enum` | 可选、非空枚举，成员必须符合当前类型 |
| `description` | 可选描述，不参与校验 |

工作流输入、data 状态、Agent 输入输出和 Capability 输入输出的 Schema 根必须是 object。支持嵌套对象和数组，但**路径绑定仍仅支持对象点路径**。

不支持 `$ref`、远程引用、anyOf/oneOf/allOf、nullable 类型数组、pattern、format、数值区间等关键字；会明确拒绝，不会默默忽略。后续添加关键字时必须同时实现编译期兼容性规则和运行期校验。

```json
{
  "input_schema": {
    "type": "object",
    "properties": {"equipment_id": {"type": "string"}},
    "required": ["equipment_id"],
    "additionalProperties": false
  },
  "state_schema": {
    "type": "object",
    "properties": {
      "torque": {"type": "number"},
      "diagnosis": {"type": "string"}
    },
    "required": ["torque"],
    "additionalProperties": false
  }
}
```

## 编译阶段

在原有拓扑、能力和审批校验之外增加：

1. 常量参数是否符合接收方 Schema；Capability/Agent 必需参数是否齐全、是否传入未声明参数。
2. 工作流输入、上游输出与下游参数类型是否兼容。
3. 输出字段是否存在且 guaranteed（required），目标 data 路径是否声明、类型是否匹配。
4. 嵌套路径是否穿过标量或引用可选字段。
5. decision 的比较类型是否合理。
6. 分支合流时检查每个可能来源的类型，不能只检查一条分支。

兼容性不是“类型名称相等”：integer 可流入 number，反向不允许；对象检查 required 和额外字段策略，数组检查 items，枚举检查集合包含关系。不做类型转换。

1.0 未声明的类型可能推迟到运行时判断；1.1 的引用和输出写入必须有可确定的声明类型。Schema 不代替现有的生产者分析：仅在 state_schema 声明某字段，不代表该字段已经被上游生成。

静态分析不做条件求值、自动插入类型转换，也不完整证明所有终止分支均已填齐最终必需字段；最终 required 会在运行结束时检查。

## 运行阶段

- 创建 Run 前校验 workflow inputs；失败不会注册 Run。
- 直接调用 LangGraph 时也在入口重新校验 inputs，防止绕过 initial_state。
- 每次工具调用前校验参数，工具返回后校验结果（包括 Agent 经 invoke 调用的工具）。
- Agent 调用前校验 context，返回后校验 output_schema。
- 写回 data 前校验增量状态；校验失败不会提交该节点的数据更新。
- 完成节点检查完整 state_schema 后才发出 run_finished。

错误类型为 `SchemaValueError`，message/location 带有节点、能力或数据路径；Runner 将其记录为 failed/error。

**校验输出失败不等于撤销工具副作用。** 工具可能已经执行成功但返回了非法数据，不得因此自动重试写操作。

### state_schema.required 的特殊语义

data 从空对象逐步构造。节点完成时允许对象尚未包含 required 字段，但已有字段必须符合类型、enum 和 additionalProperties 策略。工作流结束时检查完整 required。

数组整体赋值，数组中的对象必须始终完整，不能通过“增量状态”绕过 items.required。对象 enum 也始终完整检查，不做部分枚举匹配。

条件分支才会生成的字段不要声明为全局 required，除非每个结束分支都保证提供。正常施工直接结束而无需生成处置方案，因此示例仅将 torque 设为最终必需字段。

## Capability 接入约定

```python
from runtime.workflow.interfaces import CapabilityInfo

info = CapabilityInfo(
    id="telemetry.read",
    input_schema={
        "type": "object",
        "properties": {"equipment_id": {"type": "string"}},
        "required": ["equipment_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"torque": {"type": "number"}},
        "required": ["torque"],
        "additionalProperties": False,
    },
)
```

CapabilityInfo 构造时将字典归一化成 DataSchema 并验证。若角色 C 提供更完整的 CapabilitySpec，应通过适配器返回该接口；不要直接透传不受支持的 Schema 关键字。编译器会复制本次使用的能力元数据，运行期间 Registry 变化不影响该版本的校验契约。

AgentExecutor 的调用签名保持不变：`run(goal, context, capabilities, invoke)`。Schema 是编译器的校验边界，不代表真实模型接入已完成；真实 Agent 仍需自行实现结构化生成和执行预算。

## 共享协议导出

在 `multica/backend/` 执行：

```bash
uv run python -m scripts.export_contracts
```

生成 `multica/contracts/workflow.schema.json` 与 `run_snapshot.schema.json`。导出的结构 Schema 不代替 Pydantic 跨字段约束、编译器图分析和 Registry 检查，发布前必须调用 validate_workflow/compile_workflow。
