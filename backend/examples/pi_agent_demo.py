"""Real Pi/model + existing simulated tools. Stops at the approval boundary.

Run from backend: uv run python -m examples.pi_agent_demo --help
This performs model requests and may incur provider charges. No real device I/O.
"""
import argparse
import asyncio
import json
from dataclasses import asdict

from examples.demo import FakeRegistry, FakeRuntime, load_typed_spec
from runtime.agent import PiAgentExecutor
from runtime.execution import WorkflowRunner


async def main(args):
    command = ["node", args.pi_cli] if args.pi_cli else ["pi"]
    agent = PiAgentExecutor(command, provider=args.provider, model=args.model,
                            agent_dir=args.agent_dir, timeout=args.timeout)
    spec = load_typed_spec()
    # Make demo evidence gathering deterministic in intent, not hard-code an answer.
    nodes = [node.model_copy(update={"goal": (
        "调用 history.query 获取模拟扭矩基线；结合当前扭矩，生成 changes.torque 调整方案。"
        "这是模拟环境，不操作真实设备。使用 submit_result 提交符合输出 Schema 的结果。"
    )}) if node.kind == "agent_task" else node for node in spec.nodes]
    spec = spec.model_copy(update={"nodes": nodes})
    runner = WorkflowRunner(registry=FakeRegistry(), runtime=FakeRuntime(), agent=agent)
    snapshot = await runner.start_run(spec, {"equipment_id": "TBM-01"})
    print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
    print("Demo does not auto-approve. Inspect the proposal before resuming a run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi-cli", help="Installed Pi bin.pi JS path; runs with node")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--agent-dir", help="Isolated Pi configuration directory")
    parser.add_argument("--timeout", type=float, default=120)
    asyncio.run(main(parser.parse_args()))
