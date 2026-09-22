import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock

import aiohttp

from runtime.agent.bridge import CapabilityBridge
from runtime.agent import PiAgentExecutor, PiRpcError


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.invoke = AsyncMock(return_value={"count": 3})
        self.bridge = CapabilityBridge(["history.query"], self.invoke)
        await self.bridge.__aenter__()
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.bridge.__aexit__()

    async def post(self, path, body, token=None):
        async with self.client.post(self.bridge.url + path, json=body, headers={
            "Authorization": "Bearer " + (self.bridge.token if token is None else token)
        }) as response:
            return response.status, await response.text()

    def call(self, call_id="c1", **kw):
        return {"call_id": call_id, "capability": "history.query", "args": {}, **kw}

    async def test_auth_and_allowlist(self):
        self.assertEqual((await self.post("/invoke", self.call(), token="bad"))[0], 401)
        self.assertEqual((await self.post("/invoke", self.call(capability="control.apply")))[0], 403)
        self.invoke.assert_not_awaited()

    async def test_dedup_and_conflict(self):
        replies = await asyncio.gather(*(self.post("/invoke", self.call()) for _ in range(3)))
        self.assertTrue(all(status == 200 for status, _ in replies))
        self.invoke.assert_awaited_once_with("history.query", {})
        self.assertEqual((await self.post("/invoke", self.call(args={"different": True})))[0], 409)

    async def test_error_cached_without_retry(self):
        self.invoke.side_effect = RuntimeError("test failure")
        for _ in range(2):
            status, text = await self.post("/invoke", self.call())
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(text)["ok"])
        self.invoke.assert_awaited_once()

    async def test_result_closes_calls(self):
        self.assertEqual((await self.post("/result", {"value": []}))[0], 400)
        self.assertEqual((await self.post("/result", {"value": {"diagnosis": "x"}}))[0], 200)
        self.assertEqual(self.bridge.result, {"diagnosis": "x"})
        self.assertEqual((await self.post("/invoke", self.call()))[0], 409)
        self.assertEqual((await self.post("/result", {"value": {}}))[0], 409)

    async def test_pending_call_blocks_submission(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return {}
        self.invoke.side_effect = slow
        pending = asyncio.create_task(self.post("/invoke", self.call()))
        await entered.wait()
        try:
            self.assertEqual((await self.post("/result", {"value": {}}))[0], 409)
        finally:
            release.set()
            await pending

    async def test_output_schema_rejection_then_correction(self):
        from runtime.workflow.schema import DataSchema
        self.bridge.output_schema = DataSchema.model_validate({
            "type": "object", "properties": {"diagnosis": {"type": "string"}},
            "required": ["diagnosis"], "additionalProperties": False})
        status, text = await self.post("/result", {"value": {"diagnosis": 5}})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(text)["ok"])
        self.assertIsNone(self.bridge.result)
        self.assertEqual((await self.post("/result", {"value": {"diagnosis": "ok"}}))[0], 200)

    async def test_budget(self):
        self.bridge._calls = {str(i): ("x", asyncio.create_task(asyncio.sleep(0))) for i in range(100)}
        self.assertEqual((await self.post("/invoke", self.call()))[0], 429)


FAKE_PI = r'''
import sys, os, json, urllib.request
json.loads(sys.stdin.readline())
def emit(e): print(json.dumps(e), flush=True)
def post(path, body):
    req=urllib.request.Request(os.environ['TBM_BRIDGE_URL']+path,
        data=json.dumps(body).encode(), headers={
        'Authorization':'Bearer '+os.environ['TBM_BRIDGE_TOKEN'],
        'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req))
emit({'type':'response','id':'task','success':True})
emit({'type':'tool_execution_start','toolName':'invoke_capability'})
result=post('/invoke', {'call_id':'t1','capability':'history.query','args':{'ring':10}})
assert result['ok'], result
post('/result', {'value':{'diagnosis':result['value']['diagnosis']}})
emit({'type':'message_end','message':{'role':'assistant','stopReason':'toolUse','content':[]}})
emit({'type':'agent_settled'})
for line in sys.stdin: pass
'''


class BridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_roundtrip(self):
        invoke = AsyncMock(return_value={"diagnosis": "abnormal"})
        agent = PiAgentExecutor([sys.executable, "-u", "-c", FAKE_PI],
                                timeout=10, shutdown_timeout=0.1)
        result = await agent.run(goal="diagnose", context={}, capabilities=["history.query"], invoke=invoke)
        self.assertEqual(result, {"diagnosis": "abnormal"})
        invoke.assert_awaited_once_with("history.query", {"ring": 10})

    async def test_workflow_compiler_bridge_integration(self):
        from runtime.workflow.compiler import compile_workflow, initial_state
        from runtime.workflow.spec import WorkflowSpec
        from runtime.workflow.interfaces import CapabilityInfo, ToolResult
        schema = {"type": "object", "properties": {"diagnosis": {"type": "string"}},
                  "required": ["diagnosis"], "additionalProperties": False}
        spec = WorkflowSpec.model_validate({
            "id": "pi_test", "version": "1", "name": "Pi test", "entrypoint": "diagnose",
            "nodes": [{"id": "diagnose", "kind": "agent_task", "name": "Diagnosis",
                       "goal": "diagnose", "capabilities": ["history.query"],
                       "output_schema": schema, "outputs": {"diagnosis": "$.data.diagnosis"}}],
            "edges": [{"source": "diagnose", "target": "$end"}]})
        class Registry:
            def get(self, name):
                return CapabilityInfo(id=name, output_schema=schema)
        runtime = type("Runtime", (), {})()
        runtime.invoke = AsyncMock(return_value=ToolResult(status="ok", value={"diagnosis": "abnormal"}))
        # The real compiler supplies schema metadata to the optional richer port.
        script = FAKE_PI.replace("json.loads(sys.stdin.readline())", "json.loads(sys.stdin.readline())\nassert json.loads(os.environ['TBM_OUTPUT_SCHEMA'])['required'] == ['diagnosis']")
        agent = PiAgentExecutor([sys.executable, "-u", "-c", script], shutdown_timeout=0.1)
        events = []
        async def emit(event): events.append(event)
        graph = compile_workflow(spec, registry=Registry(), runtime=runtime,
                                 agent=agent, event_sink=emit)
        state = await graph.ainvoke(initial_state(spec, {}))
        self.assertEqual(state["data"]["diagnosis"], "abnormal")
        self.assertEqual(state["system"]["status"], "completed")
        self.assertEqual(sum(e["type"] == "tool_started" for e in events), 1)
        self.assertEqual(sum(e["type"] == "tool_finished" for e in events), 1)
        runtime.invoke.assert_awaited_once_with("history.query", {"ring": 10})

    async def test_text_without_submission_is_not_success(self):
        script = '''import sys,json
json.loads(sys.stdin.readline())
for e in [{"type":"response","id":"task","success":True},
{"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[]}},
{"type":"agent_settled"}]: print(json.dumps(e),flush=True)
for line in sys.stdin: pass
'''
        agent = PiAgentExecutor([sys.executable, "-u", "-c", script], shutdown_timeout=0.1)
        with self.assertRaisesRegex(PiRpcError, "without submit_result"):
            await agent.run(goal="test", context={}, capabilities=[], invoke=None)
