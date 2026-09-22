# 盾构智能施工 Agent Workbench

## 团队目录约定

```text
multica/
├── frontend/             前端 / 角色 A（待实现）
├── backend/
│   ├── runtime/          Workflow & Agent Runtime / 角色 B
│   ├── capabilities/     Capability & Platform / 角色 C（待实现）
│   ├── simulation/       模拟施工世界 / 角色 C（待实现）
│   ├── projection/       事件投影 / 角色 C（待实现）
│   ├── api/              REST / SSE / 角色 C（待实现）
│   ├── examples/         后端运行示例
│   ├── main.py           当前后端演示入口
│   ├── pyproject.toml    后端共享 Python 依赖配置
│   └── uv.lock
├── contracts/            跨团队共享协议
│   └── workflow.schema.json
└── tests/
    └── runtime/          角色 B 的工作流协议与编译器测试
```

标注“待实现”的目录仅表示约定，不代表已有实现。角色 B 的实现位于 `backend/runtime/`；后端共享配置变更需与角色 C 协调。共享协议留在根级 `contracts/`，便于前后端共同使用。

## 接口交接

角色 C 接入请先阅读：[角色C_接口交接.md](角色C_接口交接.md)。

## 当前实现

已实现 WorkflowSpec v1.0/v1.1、Schema 静态与运行期校验、LangGraph 编译器，以及 Runner 运行管理层（pending / running / waiting_approval / completed / failed）。详细协议、接入接口与限制参见 [后端说明](backend/README.md) 和 [Schema 协议](backend/runtime/workflow/SCHEMA.md)；启动、查询、审批恢复接口见 [Runner 说明](backend/runtime/execution/README.md)。

```bash
cd backend
uv sync
uv run python main.py
uv run python -m examples.runner_demo
uv run python -m unittest discover -s ../tests/runtime -v
```

示例使用 FakeAgent、FakeRuntime 与内存 checkpoint，不依赖真实模型或外部设备。
