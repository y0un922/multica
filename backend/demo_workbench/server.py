"""Local Demo composition root; workflows/tools/checkpoints live only in memory."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import web
from langgraph.checkpoint.memory import InMemorySaver

from examples.demo import FakeAgent, FakeRegistry, FakeRuntime, load_typed_spec
from runtime.agent import PiAgentExecutor
from runtime.agent.launcher import resolve_pi_command
from runtime.execution import WorkflowRunner
from runtime.execution.events import InMemoryEventBus
from runtime.execution.runner import RunConflictError, RunNotFoundError
from runtime.workflow import WorkflowSpec, compile_workflow
from runtime.workflow.tool_contracts import ToolResult
from .template import DemoRegistry, template_draft


class TracedRuntime(FakeRuntime):
    """Demo-only platform adapter: tools and runner share one sequence allocator."""

    def __init__(self, events, *, torque=4.12):
        super().__init__(torque=torque)
        self.events = events

    async def invoke(self, tool_name, arguments, context):
        payload = {"tool_name": tool_name, "node_id": context.node_id, "arguments": arguments}
        await self.events.emit(run_id=context.run_id, event_type="tool.started", payload=payload)
        try:
            if tool_name == "proposal.generate":
                self.calls.append((tool_name, dict(arguments)))
                result = ToolResult(status="ok", data={"changes": {"torque": 3.5}})
            else:
                result = await super().invoke(tool_name, arguments, context)
        except Exception as exc:
            await self.events.emit(run_id=context.run_id, event_type="tool.failed",
                                   payload={**payload, "error": str(exc)})
            raise
        await self.events.emit(
            run_id=context.run_id,
            event_type="tool.completed" if result.status == "ok" else "tool.failed",
            payload={**payload, "result": result.model_dump(mode="json")},
        )
        return result


@dataclass
class RunSession:
    runner: WorkflowRunner
    runtime: TracedRuntime
    events: InMemoryEventBus
    spec: WorkflowSpec
    mode: str


def presentation(spec):
    """Business graph comes from Spec, never from LangGraph private topology."""
    return {
        "name": spec.name,
        "nodes": [{"id": n.id, "name": n.name, "kind": n.kind, "type": n.type,
                   "ui_stage_id": n.ui_stage_id or n.id} for n in spec.nodes]
                 + [{"id": "$end", "name": "结束", "kind": "end"}],
        "edges": [e.model_dump(mode="json") for e in spec.edges],
    }


class Workbench:
    def __init__(self, pi_factory=None):
        self.token = secrets.token_urlsafe(32)
        self.pi_factory = pi_factory
        self.compiled = {}
        self.runs = {}
        self.tasks = set()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def done(completed):
            self.tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()  # Runner records normal execution failures in its snapshot.
        task.add_done_callback(done)

    async def close(self, app):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def session(self, request):
        try:
            return self.runs[request.match_info["run_id"]]
        except KeyError:
            raise web.HTTPNotFound(text="unknown demo run") from None


KEY = web.AppKey("workbench", Workbench)


@web.middleware
async def boundary(request, handler):
    # No network exposure, CORS or browser cross-site mutations. Host check also
    # prevents DNS rebinding against this unauthenticated local development UI.
    host = urlsplit("http://" + request.host).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise web.HTTPForbidden(text="local host required")
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="cross-origin request rejected")
    if request.method not in ("GET", "HEAD"):
        token = request.headers.get("X-Demo-Token", "")
        if not secrets.compare_digest(token, request.app[KEY].token):
            raise web.HTTPForbidden(text="demo token required")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="application/json required")
    try:
        return await handler(request)
    except (RunConflictError,) as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except RunNotFoundError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def body(request):
    def invalid(value):
        raise ValueError(f"invalid JSON number: {value}")
    value = json.loads(await request.text(), parse_constant=invalid)
    json.dumps(value, allow_nan=False)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


async def index(request):
    return web.FileResponse(Path(__file__).with_name("index.html"))


async def template(request):
    draft, options = template_draft()
    return web.json_response({
        "token": request.app[KEY].token,
        "pi_enabled": request.app[KEY].pi_factory is not None,
        "draft": draft, "node_options": options,
    })


async def compile_draft(request):
    wb = request.app[KEY]
    if len(wb.compiled) >= 100:
        raise web.HTTPTooManyRequests(text="Demo limit: restart to clear 100 compiled drafts")
    draft = await body(request)
    spec = WorkflowSpec.model_validate(draft.get("workflow"))
    # Compile checks topology, schemas and policy. It never invokes an agent/tool.
    compile_workflow(spec, registry=DemoRegistry(), runtime=FakeRuntime(),
                     agent=FakeAgent(), checkpointer=InMemorySaver())
    compiled_id = str(uuid4())
    wb.compiled[compiled_id] = spec.model_copy(deep=True)
    return web.json_response({"compiled_id": compiled_id, "valid": True,
                              "graph": presentation(spec)})


async def start_run(request):
    wb = request.app[KEY]
    data = await body(request)
    compiled_id = data.get("compiled_id")
    if not isinstance(compiled_id, str) or compiled_id not in wb.compiled:
        raise ValueError("compile this draft before running")
    if "agent" in data:
        raise ValueError("Run-level agent override was removed; configure each node.type as pi or tool")
    spec = wb.compiled[compiled_id].model_copy(deep=True)
    pi_nodes = [node for node in spec.nodes if node.type == "pi"]
    mode = "mixed" if pi_nodes else "tool"
    if pi_nodes and wb.pi_factory is None:
        raise ValueError("Template contains type=pi nodes; restart with --enable-pi or switch them to tool")
    if len(wb.runs) >= 100:
        raise web.HTTPTooManyRequests(text="Demo limit: restart to clear 100 runs")
    scenario = data.get("scenario", "anomaly")
    if scenario not in ("normal", "anomaly"):
        raise ValueError("scenario must be normal or anomaly")
    events = InMemoryEventBus()
    runtime = TracedRuntime(events, torque=3.0 if scenario == "normal" else 4.12)
    runner = WorkflowRunner(registry=DemoRegistry(), runtime=runtime,
                            agent_factory=wb.pi_factory, event_publisher=events)
    inputs = data.get("inputs", {"equipment_id": "TBM-01"})
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    snapshot = runner.create_run(spec, inputs)
    wb.runs[snapshot.run_id] = RunSession(runner, runtime, events, spec, mode)
    wb.spawn(runner.execute_run(snapshot.run_id))
    return web.json_response({"run_id": snapshot.run_id, "graph": presentation(spec)}, status=202)


def snapshot_json(state):
    value = asdict(state)
    value["confirmation"] = state.confirmation.model_dump(mode="json") if state.confirmation else None
    return value


async def snapshot(request):
    session = request.app[KEY].session(request)
    run_id = request.match_info["run_id"]
    state = session.runner.get_run(run_id)
    return web.json_response({
        "snapshot": snapshot_json(state), "agent": session.mode,
        "node_executors": {n.id: n.type for n in session.spec.nodes},
        "world": {"torque": session.runtime.torque},
        "calls": [{"tool_name": name, "arguments": args} for name, args in session.runtime.calls],
    })


async def events(request):
    session = request.app[KEY].session(request)
    sequence = int(request.query.get("after_sequence", "0"))
    return web.json_response({"events": [e.model_dump(mode="json") for e in session.events.get_events(
        request.match_info["run_id"], after_sequence=sequence)]})


async def confirm(request):
    session = request.app[KEY].session(request)
    data = await body(request)
    confirmation_id, decision = data.get("confirmation_id"), data.get("decision")
    if not isinstance(confirmation_id, str) or decision not in ("accepted", "rejected"):
        raise ValueError("confirmation_id and decision accepted/rejected required")
    # Local-only direct Runner entry, NOT a replacement for authenticated B Confirmation API.
    result = await session.runner.resume_run(request.match_info["run_id"],
                                            confirmation_id=confirmation_id, decision=decision)
    return web.json_response({"snapshot": snapshot_json(result)})


def create_app(*, pi_factory=None):
    app = web.Application(middlewares=[boundary], client_max_size=1024 * 1024)
    wb = Workbench(pi_factory)
    app[KEY] = wb
    app.on_cleanup.append(wb.close)
    app.add_routes([
        web.get("/", index), web.get("/demo/template", template),
        web.post("/demo/compile", compile_draft), web.post("/demo/runs", start_run),
        web.get("/demo/runs/{run_id}", snapshot),
        web.get("/demo/runs/{run_id}/events", events),
        web.post("/demo/runs/{run_id}/confirm", confirm),
    ])
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--enable-pi", action="store_true", help="Enable potentially billable model runs")
    parser.add_argument("--pi-cli", help="Installed Pi CLI JS path; launches with node")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--agent-dir")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    factory = None
    if args.enable_pi:
        try:
            command = resolve_pi_command(["node", args.pi_cli] if args.pi_cli else ["pi"])
        except OSError as exc:
            parser.error(str(exc))
        print("Pi launch command:", json.dumps(command, ensure_ascii=False))
        factory = lambda node: PiAgentExecutor(
            command, provider=node.pi.provider or args.provider,
            model=node.pi.model or args.model, agent_dir=args.agent_dir,
            timeout=node.pi.timeout if node.pi.timeout is not None else args.timeout)
    web.run_app(create_app(pi_factory=factory), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
