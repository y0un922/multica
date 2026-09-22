"""Execution-scoped loopback bridge. Tokens never enter model prompts."""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from aiohttp import web

from ..workflow.schema import SchemaValueError, validate_value


class CapabilityBridge:
    def __init__(self, capabilities, invoke, *, call_timeout=60, output_schema=None):
        self.capabilities = frozenset(capabilities)
        self.invoke = invoke
        self.call_timeout = call_timeout
        self.output_schema = output_schema
        self.token = secrets.token_urlsafe(32)
        self.url = ""
        self.result: dict[str, Any] | None = None
        self.post_result_call = False
        self._calls = {}
        self._lock = asyncio.Lock()
        self._runner = None
        self._active = False

    async def __aenter__(self):
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/invoke", self._invoke)
        app.router.add_post("/result", self._result)
        self._runner = web.AppRunner(app, shutdown_timeout=1)
        await self._runner.setup()
        try:
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            port = self._runner.addresses[0][1]
            self.url = f"http://127.0.0.1:{port}"
            self._active = True
            return self
        except BaseException:
            await self._runner.cleanup()
            raise

    async def __aexit__(self, *args):
        self._active = False
        for _, task in self._calls.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*(task for _, task in self._calls.values()), return_exceptions=True)
        await self._runner.cleanup()
        self.token = ""

    async def _body(self, request):
        auth = request.headers.get("Authorization", "")
        if not self._active or not secrets.compare_digest(auth, "Bearer " + self.token):
            raise web.HTTPUnauthorized()
        # This endpoint is not browser-facing. Prevent browser-origin requests.
        if request.headers.get("Origin"):
            raise web.HTTPForbidden()
        try:
            def reject(value):
                raise ValueError(f"non-finite number: {value}")
            body = json.loads(await request.text(), parse_constant=reject)
            json.dumps(body, allow_nan=False)
            if not isinstance(body, dict):
                raise ValueError("object required")
            return body
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="invalid JSON object")

    async def _invoke(self, request):
        body = await self._body(request)
        cap, args, call_id = body.get("capability"), body.get("args"), body.get("call_id")
        if not isinstance(cap, str) or cap not in self.capabilities:
            raise web.HTTPForbidden(text="capability not allowed")
        if not isinstance(args, dict) or not isinstance(call_id, str) or not 0 < len(call_id) <= 256:
            raise web.HTTPBadRequest(text="args object and call_id required")
        signature = json.dumps([cap, args], sort_keys=True, allow_nan=False)
        async with self._lock:
            if self.result is not None:
                self.post_result_call = True
                raise web.HTTPConflict(text="execution already submitted")
            previous = self._calls.get(call_id)
            if previous:
                if previous[0] != signature:
                    raise web.HTTPConflict(text="call_id reused with different arguments")
                task = previous[1]
            else:
                if len(self._calls) >= 100:
                    raise web.HTTPTooManyRequests(text="tool call budget exceeded")
                task = asyncio.create_task(self._execute(cap, args))
                self._calls[call_id] = (signature, task)
        result = await asyncio.shield(task)
        return web.json_response(result)

    async def _execute(self, cap, args):
        try:
            async with asyncio.timeout(self.call_timeout):
                result = await self.invoke(cap, args)
                if not isinstance(result, dict):
                    raise ValueError("capability result must be an object")
                json.dumps(result, allow_nan=False)
                return {"ok": True, "value": result}
        except Exception as exc:
            # Do not retry side effects here. The owning runtime decides policy.
            return {"ok": False, "error": str(exc) or type(exc).__name__}

    async def _result(self, request):
        body = await self._body(request)
        value = body.get("value")
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="result must be an object")
        try:
            validate_value(value, self.output_schema, "agent.result")
        except SchemaValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        async with self._lock:
            if any(not task.done() for _, task in self._calls.values()):
                raise web.HTTPConflict(text="capability calls still running")
            if self.result is not None:
                raise web.HTTPConflict(text="result already submitted")
            self.result = value
        return web.json_response({"ok": True})
