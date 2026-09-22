"""Run lifecycle through the service interface intended for API integration."""
import asyncio
from pydantic import TypeAdapter
from runtime.execution import RunSnapshot

import json

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_typed_spec
from runtime.execution import WorkflowRunner


def snapshot_dict(snapshot):
    return TypeAdapter(RunSnapshot).dump_python(snapshot, mode="json")

async def main():
    runner = WorkflowRunner(registry=FakeRegistry(), runtime=FakeRuntime(), agent=FakeAgent())
    paused = await runner.start_run(load_typed_spec(), {"equipment_id": "TBM-01"})
    print("Run:", paused.run_id, paused.status)
    print("Confirmation:", json.dumps(paused.confirmation.model_dump(mode="json"), ensure_ascii=False))
    # Demo only: production approval must come from an authenticated human command.
    final = await runner.resume_run(paused.run_id, confirmation_id=paused.confirmation.id, decision="accepted")
    print(json.dumps(snapshot_dict(final), ensure_ascii=False, indent=2))
    print("A-side AgentEvents:", len(runner.get_events(final.run_id)))


if __name__ == "__main__":
    asyncio.run(main())
