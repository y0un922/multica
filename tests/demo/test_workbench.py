"""HTTP tests for the local composition root; no provider credentials/model calls."""
import asyncio
from copy import deepcopy
import unittest
import sys

from runtime.agent import PiAgentExecutor

from aiohttp.test_utils import AioHTTPTestCase

from demo_workbench.server import KEY, create_app
from examples.demo import FakeAgent


class WorkbenchTests(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    async def setup_draft(self):
        response = await self.client.get('/demo/template')
        data = await response.json()
        self.headers = {'X-Demo-Token': data['token']}
        self.options = data['node_options']
        return data['draft']

    async def post(self, path, value):
        return await self.client.post('/demo/' + path, json=value, headers=self.headers)

    async def compile(self, pi_nodes=()):
        draft = await self.setup_draft()
        draft['workflow']['nodes'] = [self.options[n['id']]['pi'] if n['id'] in pi_nodes else n
                                      for n in draft['workflow']['nodes']]
        response = await self.post('compile', draft)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def start(self, **options):
        compiled = await self.compile(options.pop('pi_nodes', ()))
        response = await self.post('runs', {'compiled_id': compiled['compiled_id'], **options})
        self.assertEqual(response.status, 202, await response.text())
        return (await response.json())['run_id']

    async def settled(self, run_id):
        for _ in range(150):
            response = await self.client.get(f'/demo/runs/{run_id}')
            data = await response.json()
            if data['snapshot']['status'] != 'running':
                return data
            await asyncio.sleep(.01)
        self.fail('run did not settle')

    async def test_compile_is_side_effect_free_and_immutable(self):
        result = await self.compile()
        self.assertTrue(result['valid'])
        self.assertIn('$end', [n['id'] for n in result['graph']['nodes']])
        wb = self.app[KEY]
        self.assertFalse(wb.runs)
        self.assertFalse(wb.tasks)
        draft = await self.setup_draft()
        draft['workflow']['nodes'][0]['capability'] = 'missing.tool'
        response = await self.post('compile', draft)
        self.assertEqual(response.status, 400)
        self.assertIn('missing.tool', await response.text())
        self.assertEqual(wb.compiled[result['compiled_id']].nodes[0].capability, 'telemetry.read')

    async def test_approval_closes_loop_and_events_share_sequence(self):
        run_id = await self.start()
        paused = await self.settled(run_id)
        self.assertEqual(paused['snapshot']['status'], 'waiting_confirmation')
        self.assertEqual(paused['snapshot']['state']['data']['changes'], {'torque': 3.5})
        self.assertIn('proposal.generate', [c['tool_name'] for c in paused['calls']])
        self.assertNotIn('control.apply', [c['tool_name'] for c in paused['calls']])
        decision = {'confirmation_id': paused['snapshot']['confirmation']['id'], 'decision': 'accepted'}
        response = await self.post(f'runs/{run_id}/confirm', decision)
        self.assertEqual(response.status, 200, await response.text())
        # Same decision redelivery is in-process idempotent.
        self.assertEqual((await self.post(f'runs/{run_id}/confirm', decision)).status, 200)
        final = await self.settled(run_id)
        self.assertEqual(final['world']['torque'], 3.5)
        self.assertEqual(final['snapshot']['state']['data']['task_status'], 'closed')
        self.assertEqual([c['tool_name'] for c in final['calls']].count('control.apply'), 1)
        response = await self.client.get(f'/demo/runs/{run_id}/events')
        events = (await response.json())['events']
        self.assertEqual([e['sequence'] for e in events], list(range(1, len(events)+1)))
        self.assertIn('tool.completed', [e['type'] for e in events])
        self.assertEqual(events[-1]['type'], 'run.completed')
        response = await self.client.get(f'/demo/runs/{run_id}/events?after_sequence={events[-1]["sequence"]}')
        self.assertEqual((await response.json())['events'], [])

    async def test_reject_and_stale_confirmation(self):
        run_id = await self.start()
        paused = await self.settled(run_id)
        response = await self.post(f'runs/{run_id}/confirm', {'confirmation_id': 'stale', 'decision': 'accepted'})
        self.assertEqual(response.status, 409)
        response = await self.post(f'runs/{run_id}/confirm', {
            'confirmation_id': paused['snapshot']['confirmation']['id'], 'decision': 'rejected'})
        self.assertEqual(response.status, 200)
        final = await self.settled(run_id)
        self.assertEqual(final['snapshot']['state']['data']['task_status'], 'needs_attention')
        self.assertEqual(final['world']['torque'], 4.12)
        self.assertNotIn('control.apply', [c['tool_name'] for c in final['calls']])

    async def test_normal_and_isolated_runs(self):
        normal = await self.start(scenario='normal')
        result = await self.settled(normal)
        self.assertEqual(result['snapshot']['status'], 'completed')
        self.assertEqual([c['tool_name'] for c in result['calls']], ['telemetry.read'])
        anomaly = await self.start()
        result = await self.settled(anomaly)
        self.assertEqual(result['world']['torque'], 4.12)
        self.assertEqual((await self.settled(normal))['world']['torque'], 3.0)

    async def test_security_and_bad_input(self):
        draft = await self.setup_draft()
        response = await self.client.post('/demo/compile', json=draft)
        self.assertEqual(response.status, 403)
        response = await self.client.post('/demo/compile', json=draft,
                                         headers={**self.headers, 'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/demo/template', headers={'Host': 'evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.post('compile', {'workflow': None})
        self.assertEqual(response.status, 400)
        response = await self.post('runs', {'compiled_id': 'unknown'})
        self.assertEqual(response.status, 400)
        response = await self.client.get('/demo/runs/missing')
        self.assertEqual(response.status, 404)

    async def test_pi_requires_explicit_enable(self):
        compiled = await self.compile(('diagnose',))
        response = await self.post('runs', {'compiled_id': compiled['compiled_id']})
        self.assertEqual(response.status, 400)
        self.assertFalse(self.app[KEY].runs)

    async def test_pi_factory_wiring_and_failure(self):
        calls = []
        def factory(node):
            calls.append(node.id)
            return FakeAgent()
        self.app[KEY].pi_factory = factory
        run_id = await self.start(pi_nodes=('diagnose',))
        result = await self.settled(run_id)
        self.assertEqual(calls, ['diagnose'])
        self.assertEqual(result['agent'], 'mixed')
        self.assertEqual(result['snapshot']['status'], 'waiting_confirmation')
        class BrokenAgent:
            async def run(self, **kwargs):
                raise RuntimeError('model unavailable')
        self.app[KEY].pi_factory = lambda node: BrokenAgent()
        failed = await self.start(pi_nodes=('diagnose',))
        result = await self.settled(failed)
        self.assertEqual(result['snapshot']['status'], 'failed')
        self.assertIn('model unavailable', result['snapshot']['error']['message'])

    async def test_http_to_rpc_bridge_roundtrip(self):
        # Real RPC subprocess and authenticated HTTP bridge; fake model peer.
        # This tests transport, not actual provider tool-use behavior.
        script = r'''
import sys, os, json, urllib.request
json.loads(sys.stdin.readline())
def emit(event): print(json.dumps(event), flush=True)
def post(path, body):
    req = urllib.request.Request(os.environ['TBM_BRIDGE_URL'] + path,
        data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + os.environ['TBM_BRIDGE_TOKEN'],
            'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=5))
assert 'changes' in json.loads(os.environ['TBM_OUTPUT_SCHEMA'])['required']
emit({'type': 'response', 'id': 'task', 'success': True})
result = post('/invoke', {'call_id': 'history-1', 'capability': 'history.query', 'args': {}})
assert result['ok'], result
submitted = post('/result', {'value': {'changes': {'torque': result['value']['baseline']}}})
assert submitted['ok'], submitted
emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse', 'content': []}})
emit({'type': 'agent_settled'})
for line in sys.stdin: pass
'''
        self.app[KEY].pi_factory = lambda node: PiAgentExecutor(
            [sys.executable, '-u', '-c', script], timeout=10, shutdown_timeout=.1)
        run_id = await self.start(pi_nodes=('diagnose',))
        result = await self.settled(run_id)
        self.assertEqual(result['snapshot']['status'], 'waiting_confirmation', result)
        self.assertEqual(result['snapshot']['state']['data']['changes'], {'torque': 3.5})
        self.assertEqual([c['tool_name'] for c in result['calls']], ['telemetry.read', 'history.query'])
        response = await self.post(f'runs/{run_id}/confirm', {
            'confirmation_id': result['snapshot']['confirmation']['id'], 'decision': 'accepted'})
        self.assertEqual(response.status, 200)
        final = await self.settled(run_id)
        self.assertEqual(final['snapshot']['state']['data']['task_status'], 'closed')

    async def test_run_cannot_override_node_types(self):
        compiled = await self.compile()
        response = await self.post('runs', {'compiled_id': compiled['compiled_id'], 'agent': 'pi'})
        self.assertEqual(response.status, 400)
        self.assertIn('node.type', await response.text())
        self.assertFalse(self.app[KEY].runs)

    async def test_tool_nodes_do_not_construct_pi(self):
        def forbidden(node):
            raise AssertionError('Tool template must not construct an Agent')
        self.app[KEY].pi_factory = forbidden
        run_id = await self.start()
        result = await self.settled(run_id)
        self.assertEqual(result['snapshot']['status'], 'waiting_confirmation')
        self.assertEqual(result['node_executors']['diagnose'], 'tool')

    async def test_invalid_topology_and_approval_bypass(self):
        draft = await self.setup_draft()
        broken = deepcopy(draft)
        broken['workflow']['edges'][0]['target'] = 'missing_node'
        self.assertEqual((await self.post('compile', broken)).status, 400)
        broken = deepcopy(draft)
        for edge in broken['workflow']['edges']:
            if edge['source'] == 'diagnose':
                edge['target'] = 'apply'
        self.assertEqual((await self.post('compile', broken)).status, 400)

    async def test_page_served(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        self.assertIn('Workflow Draft', await response.text())


if __name__ == '__main__':
    unittest.main()
