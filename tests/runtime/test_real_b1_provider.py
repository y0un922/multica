"""Real A Runner + real B Provider, no fake B, HTTP, confirmation or B2."""

import unittest

try:
    from backend.integration import create_golden_path_backend
except ImportError:  # B checkout is optional for A's standalone tests.
    create_golden_path_backend = None

from examples.real_b1 import build_real_b1_workflow
from runtime.execution import WorkflowRunner
from runtime.workflow import WorkflowSpec, WorkflowValidationError, validate_workflow
from runtime.workflow.boundary import JsonBoundaryToolRuntime, JsonEventPublisher
from runtime.workflow.catalog import CatalogRegistry
from runtime.workflow.interfaces import CapabilityInfo

TOOLS = [
    "query_tbm_status", "query_sensor_history", "detect_parameter_anomaly",
    "query_geological_data", "diagnose_fault", "estimate_risk",
]


class WholeResultBindingTests(unittest.TestCase):
    def test_whole_result_binding_is_restricted_to_one_tool_output(self):
        spec = WorkflowSpec.model_validate({
            "id": "root_binding", "version": "1", "name": "Root binding",
            "entrypoint": "status",
            "nodes": [{"id": "status", "name": "Status", "kind": "capability",
                       "capability": "query_tbm_status", "outputs": {
                           "$": "$.data.current_status", "other": "$.data.other",
                       }}],
            "edges": [{"source": "status", "target": "$end"}],
        })

        class Registry:
            def get(self, name):
                return CapabilityInfo(id=name)

        with self.assertRaisesRegex(WorkflowValidationError, "whole-result binding"):
            validate_workflow(spec, Registry())


@unittest.skipUnless(create_golden_path_backend is not None, "install Backend B for real A/B E2E")
class RealB1GoldenPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_runner_with_b_provider_for_both_scenarios(self):
        for scenario in ("normal", "torque_anomaly"):
            with self.subTest(scenario=scenario):
                async with create_golden_path_backend(scenario=scenario) as b:
                    registry = CatalogRegistry(b.list_tools_json())
                    runner = WorkflowRunner(
                        registry=registry, runtime=JsonBoundaryToolRuntime(b),
                        event_publisher=JsonEventPublisher(b),
                    )
                    final = await runner.start_run(
                        build_real_b1_workflow(), {"machine_id": "TBM-01"}
                    )
                    self.assertEqual(final.status, "completed", final.error)
                    data = final.state["data"]
                    self.assertEqual(data["sensor_history"]["records"][-1], data["current_status"])
                    self.assertEqual(data["geology"]["machine_id"], "TBM-01")
                    self.assertEqual(data["diagnosis"]["machine_id"], "TBM-01")
                    self.assertEqual(data["anomaly"]["abnormal"], scenario == "torque_anomaly")
                    self.assertEqual(data["risk"]["risk_level"],
                                     "high" if scenario == "torque_anomaly" else "low")
                    if scenario == "torque_anomaly":
                        self.assertTrue(data["diagnosis"]["candidates"])
                        self.assertTrue(data["diagnosis"]["evidence"])
                    calls = await b.tool_call_store.list_by_run(final.run_id)
                    self.assertEqual([call.tool_name for call in calls], TOOLS)
                    self.assertTrue(all(call.status.value == "ok" for call in calls))
                    events = await b.event_store.list_events(final.run_id)
                    self.assertEqual([event.sequence for event in events],
                                     list(range(1, len(events) + 1)))
                    self.assertEqual([event.type.value for event in events if
                                      event.type.value.startswith("tool.")],
                                     [kind for _ in TOOLS for kind in
                                      ("tool.started", "tool.completed")])
                    self.assertEqual(len(events), 26)  # 14 A lifecycle + 12 B tool
                    self.assertEqual(events[0].type.value, "run.started")
                    self.assertEqual(events[-1].type.value, "run.completed")
                    self.assertEqual([e["sequence"] for e in runner.get_events(final.run_id)],
                                     [event.sequence for event in events if
                                      not event.type.value.startswith("tool.")])
                    self.assertEqual(await b.store.tasks.list(), [])
                    self.assertEqual(await b.store.confirmations.list_by_run(final.run_id), [])

    async def test_real_b_failure_is_classified_once_and_a_fails_without_retry(self):
        async with create_golden_path_backend() as b:
            runner = WorkflowRunner(
                registry=CatalogRegistry(b.list_tools_json()),
                runtime=JsonBoundaryToolRuntime(b), event_publisher=JsonEventPublisher(b),
            )
            failed = await runner.start_run(
                build_real_b1_workflow(), {"machine_id": "TBM-99"}
            )
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error["code"], "TBM_NOT_FOUND")
            self.assertEqual(failed.error["status"], "fatal_error")
            calls = await b.tool_call_store.list_by_run(failed.run_id)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].result.error.code, "TBM_NOT_FOUND")
            events = await b.event_store.list_events(failed.run_id)
            self.assertEqual([e.sequence for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[-1].type.value, "run.failed")
