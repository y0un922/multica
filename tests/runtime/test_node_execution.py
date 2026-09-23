"""Node type drives dispatch; legacy kind templates continue to work."""
import unittest
from copy import deepcopy

from demo_workbench.template import template_draft, DemoRegistry
from examples.demo import FakeRuntime
from runtime.execution import WorkflowRunner
from runtime.workflow import WorkflowSpec


class NodeExecutionTests(unittest.IsolatedAsyncioTestCase):
    def make_spec(self, pi_nodes=()):
        draft, options = template_draft()
        draft['workflow']['nodes'] = [deepcopy(options[n['id']]['pi']) if n['id'] in pi_nodes else n
                                      for n in draft['workflow']['nodes']]
        return draft['workflow']

    async def test_two_pi_agents_config_context_and_tool_dispatch(self):
        data = self.make_spec(('read_status', 'diagnose'))
        for n in data['nodes']:
            if n['type'] == 'pi':
                n['pi'] = {'provider': 'provider-'+n['id'], 'model': 'model-'+n['id'], 'timeout': 15}
        spec = WorkflowSpec.model_validate(data)
        created, executed = [], []
        class Executor:
            def __init__(self, node):
                self.node = node
            async def run(self, *, goal, context, capabilities, invoke):
                executed.append((self.node.id, deepcopy(context)))
                if self.node.id == 'read_status':
                    return await invoke('telemetry.read', context)
                history = await invoke('history.query', {})
                return {'changes': {'torque': history['baseline']}}
        def factory(node):
            created.append((node.id, node.pi.provider, node.pi.model, node.pi.timeout))
            return Executor(node)
        runtime = FakeRuntime()
        runner = WorkflowRunner(registry=DemoRegistry(), runtime=runtime, agent_factory=factory)
        snapshot = runner.create_run(spec, {'equipment_id': 'TBM-01'})
        self.assertEqual(len(created), 2)
        self.assertEqual(executed, [])  # compiling does not start any agent
        self.assertEqual(created[1], ('diagnose', 'provider-diagnose', 'model-diagnose', 15))
        paused = await runner.execute_run(snapshot.run_id)
        self.assertEqual(paused.status, 'waiting_confirmation')
        self.assertEqual(executed, [('read_status', {'equipment_id': 'TBM-01'}), ('diagnose', {'torque': 4.12})])
        self.assertEqual([name for name, _ in runtime.calls], ['telemetry.read', 'history.query'])
        final = await runner.resume_run(paused.run_id, confirmation_id=paused.confirmation.id, decision='accepted')
        self.assertEqual(final.state['data']['task_status'], 'closed')
        self.assertEqual(len(executed), 2)  # apply/verify/task stay native tools
        events = runner.get_events(final.run_id)
        starts = {e['payload']['node_id']: e['payload']['executor_type'] for e in events
                  if e['type'] == 'node.started' and 'executor_type' in e['payload']}
        self.assertEqual(starts['read_status'], 'pi')
        self.assertEqual(starts['apply'], 'tool')

    async def test_unknown_type_conflicts_and_missing_v12_type(self):
        for update in ({'type': 'typo'}, {'type': 'pi', 'kind': 'capability'}, {'type': ['pi']}):
            data = self.make_spec()
            data['nodes'][0].update(update)
            with self.assertRaises(ValueError):
                WorkflowSpec.model_validate(data)
        data = self.make_spec()
        del data['nodes'][0]['type']
        with self.assertRaisesRegex(ValueError, 'explicit node type'):
            WorkflowSpec.model_validate(data)

    async def test_legacy_templates_and_roundtrip(self):
        from examples.demo import load_typed_spec
        old = load_typed_spec()
        self.assertEqual(old.nodes[0].type, 'tool')
        self.assertEqual(old.nodes[2].type, 'pi')
        spec = WorkflowSpec.model_validate(self.make_spec(('diagnose',)))
        self.assertEqual(WorkflowSpec.model_validate_json(spec.model_dump_json()), spec)

    async def test_pi_requires_contracts_and_safe_capabilities(self):
        data = self.make_spec(('diagnose',))
        data['nodes'][2].pop('output_schema')
        with self.assertRaisesRegex(ValueError, 'output_schema'):
            WorkflowSpec.model_validate(data)
        data = self.make_spec(('diagnose',))
        data['nodes'][2]['capabilities'] = ['control.apply']
        spec = WorkflowSpec.model_validate(data)
        with self.assertRaises(ValueError):
            WorkflowRunner(registry=DemoRegistry(), runtime=FakeRuntime(), agent_factory=lambda n: object()).create_run(
                spec, {'equipment_id': 'TBM-01'})

    async def test_pi_config_rejects_launch_override_and_invalid_timeout(self):
        for config in ({'timeout': 0}, {'timeout': -1}, {'command': 'arbitrary shell'}, {'model': ''}):
            data = self.make_spec(('diagnose',))
            data['nodes'][2]['pi'] = config
            with self.assertRaises(ValueError):
                WorkflowSpec.model_validate(data)

    async def test_v12_keeps_strict_static_type_checks(self):
        from runtime.workflow import compile_workflow
        from runtime.workflow.interfaces import CapabilityInfo
        from langgraph.checkpoint.memory import InMemorySaver
        from examples.demo import FakeAgent
        data = self.make_spec(('diagnose',))
        class UntypedRegistry(DemoRegistry):
            def get(self, name):
                if name == 'history.query':
                    return CapabilityInfo(name)
                return super().get(name)
        with self.assertRaises(ValueError):
            compile_workflow(WorkflowSpec.model_validate(data), registry=UntypedRegistry(),
                             runtime=FakeRuntime(), agent=FakeAgent(), checkpointer=InMemorySaver())
        data['nodes'][2]['input_schema']['properties']['torque'] = {'type': 'string'}
        with self.assertRaises(ValueError):
            compile_workflow(WorkflowSpec.model_validate(data), registry=DemoRegistry(),
                             runtime=FakeRuntime(), agent=FakeAgent(), checkpointer=InMemorySaver())

    async def test_pi_output_still_validated(self):
        class Bad:
            async def run(self, **kwargs):
                return {'changes': 'wrong structure'}
        runner = WorkflowRunner(registry=DemoRegistry(), runtime=FakeRuntime(), agent_factory=lambda n: Bad())
        result = await runner.start_run(WorkflowSpec.model_validate(self.make_spec(('diagnose',))), {'equipment_id': 'TBM-01'})
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.confirmation)
        self.assertNotIn('changes', result.state['data'])
