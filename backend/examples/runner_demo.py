"""Run lifecycle through the service interface intended for API integration."""
import asyncio
from dataclasses import asdict
import json

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_typed_spec
from runtime.execution import WorkflowRunner


async def main():
    runner = WorkflowRunner(registry=FakeRegistry(), runtime=FakeRuntime(), agent=FakeAgent())
    paused = await runner.start_run(load_typed_spec(), {"equipment_id": "TBM-01"})
    print("Run:", paused.run_id, paused.status)
    print("Approval:", json.dumps(paused.approval, ensure_ascii=False))
    # Demo only: production approval must come from an authenticated human command.
    final = await runner.resume_run(paused.run_id, approval_id=paused.approval["id"], approved=True)
    print(json.dumps(asdict(final), ensure_ascii=False, indent=2))
    print("Raw events:", len(runner.get_events(final.run_id)))


if __name__ == "__main__":
    asyncio.run(main())
