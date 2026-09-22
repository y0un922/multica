import asyncio
import json
import sys
import unittest

from runtime.agent import PiAgentExecutor, PiRpcError


def client(events, *, delay=0, timeout=5):
    script = (
        "import sys,json,time\n"
        "json.loads(sys.stdin.readline())\n"
        f"time.sleep({delay!r})\n"
        f"events=json.loads({json.dumps(events)!r})\n"
        "for e in events: print(json.dumps(e), flush=True)\n"
        "for line in sys.stdin: pass\n"
    )
    return PiAgentExecutor([sys.executable, "-u", "-c", script],
                           timeout=timeout, shutdown_timeout=0.1, tool_bridge=False)


def response(text='{"diagnosis":"test"}', stop="stop"):
    return {"type": "message_end", "message": {
        "role": "assistant", "stopReason": stop,
        "content": [{"type": "text", "text": text}]}}


ACK = {"type": "response", "id": "task", "success": True}
END = {"type": "agent_settled"}


class PiRpcTests(unittest.IsolatedAsyncioTestCase):
    async def run_agent(self, agent, capabilities=None):
        return await agent.run(goal="test", context={},
                               capabilities=capabilities or [], invoke=None)

    async def test_success(self):
        result = await self.run_agent(client([ACK, response(), END]))
        self.assertEqual(result, {"diagnosis": "test"})

    async def test_retry_waits_for_settled(self):
        result = await self.run_agent(client([
            ACK, response("", "error"), {"type": "agent_end", "willRetry": True},
            response(), END]))
        self.assertEqual(result["diagnosis"], "test")

    async def test_invalid_results(self):
        for text in ["[]", "not JSON", '{"value":NaN}', '{"value":1e999}']:
            with self.subTest(text=text), self.assertRaises(PiRpcError):
                await self.run_agent(client([ACK, response(text), END]))

    async def test_provider_error(self):
        with self.assertRaises(PiRpcError):
            await self.run_agent(client([ACK, response("", "error"), END]))

    async def test_rejected(self):
        with self.assertRaises(PiRpcError):
            await self.run_agent(client([{"type": "response", "id": "task", "success": False}]))

    async def test_capability_rejected(self):
        with self.assertRaises(NotImplementedError):
            await self.run_agent(client([]), ["history.query"])

    async def test_timeout(self):
        with self.assertRaisesRegex(PiRpcError, "timed out"):
            await self.run_agent(client([], delay=10, timeout=0.2))

    async def test_cancel(self):
        task = asyncio.create_task(self.run_agent(client([], delay=10)))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
