import json
import unittest
from pydantic import TypeAdapter
from runtime.execution import RunSnapshot

from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_typed_spec
from runtime.execution import WorkflowRunner
from scripts.export_contracts import CONTRACTS_DIR, schemas


def snapshot_dict(snapshot):
    return TypeAdapter(RunSnapshot).dump_python(snapshot, mode="json")

class ContractTests(unittest.IsolatedAsyncioTestCase):
    def test_exports_match_code(self):
        for name, expected in schemas().items():
            with self.subTest(name=name):
                actual = json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))
                self.assertEqual(actual, expected)
                Draft202012Validator.check_schema(actual)

    def test_workflow_examples_match_export(self):
        validator = Draft202012Validator(schemas()["workflow.schema.json"])
        examples = Path(__file__).resolve().parents[2] / "backend" / "examples"
        for name in ("advance_anomaly.json", "advance_anomaly_typed.json"):
            with self.subTest(name=name):
                validator.validate(json.loads((examples / name).read_text(encoding="utf-8")))

    async def test_all_snapshot_statuses_match_export(self):
        validator = Draft202012Validator(schemas()["run_snapshot.schema.json"])
        runner = WorkflowRunner(registry=FakeRegistry(), runtime=FakeRuntime(), agent=FakeAgent())
        pending = runner.create_run(load_typed_spec(), {"equipment_id": "TBM"})
        validator.validate(snapshot_dict(pending))
        waiting = await runner.execute_run(pending.run_id)
        validator.validate(snapshot_dict(waiting))
        final = await runner.resume_run(waiting.run_id, confirmation_id=waiting.confirmation.id, decision="accepted")
        validator.validate(snapshot_dict(final))
        # Exercise contract literals independently of asynchronous scheduling.
        for status in ("running", "waiting_confirmation", "completed", "failed"):
            sample = snapshot_dict(final)
            sample["status"] = status
            sample["state"]["system"]["status"] = status
            validator.validate(sample)
        for obsolete in ("pending", "waiting_approval", "paused"):
            sample = snapshot_dict(final)
            sample["status"] = obsolete
            with self.subTest(obsolete=obsolete), self.assertRaises(ValidationError):
                validator.validate(sample)
