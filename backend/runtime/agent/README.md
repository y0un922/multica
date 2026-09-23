# Pi RPC + execution-scoped Capability Bridge

`PiAgentExecutor` implements `AgentExecutor` and the optional
`ContractAgentExecutor` port. Each node execution owns a separate Pi process,
ephemeral session, loopback HTTP listener and random bearer token.

```python
from runtime.agent import PiAgentExecutor
from runtime.execution import WorkflowRunner

agent = PiAgentExecutor(
    command=["pi"],
    timeout=120,
    provider="your-provider",
    model="your-model",
)
runner = WorkflowRunner(registry=registry, runtime=runtime, agent=agent)
```

On Windows, the launcher now automatically resolves `pi` / `pi.cmd` from PATH,
reads the adjacent npm package's `bin.pi`, and launches it with Node without a
shell. Existing environment and Pi configuration are preserved. Native `pi.exe`
and explicit custom commands remain supported. For nonstandard installations,
you can still specify Node and the entry point explicitly. For Pi 0.85.1 this is:
`command=["node", "/absolute/path/to/pi-coding-agent/dist/bundle/cli.js"]`.
Do not use `shell=True` with prompts. Configure provider credentials outside
source code. `agent_dir` selects an isolated Pi configuration directory.

## Communication

```text
LangGraph agent_task
  -> PiAgentExecutor -> stdin/stdout JSONL RPC -> Pi
  -> Pi invoke_capability tool -> authenticated loopback HTTP bridge
  -> current node invoke callback -> CapabilityRuntime
  <- capability result -> Pi
  -> Pi submit_result tool -> bridge validates output schema
  -> agent_settled -> executor returns dict -> compiler validates and binds state
```

The compiler passes a defensive copy of capability input/output schemas and the
node output schema through `AgentTaskContract`. Existing executors implementing
only `run()` continue to work. Run/node IDs are metadata, never authorization.

The extension `pi_extension.ts` is loaded explicitly, despite discovery being
disabled. Pi handles its TypeScript and `typebox` imports. No project npm install
is needed. It registers only:

- `invoke_capability`: identifier plus arguments; contracts are included in the
  tool description. Python's existing invoke callback enforces actual schemas,
  allowlists and workflow events.
- `submit_result`: its value parameter uses the node output JSON Schema when
  available; Python revalidates before accepting. The tool returns terminate=true.
  Submission must happen alone, after capability calls finish. Calls after a
  submission cause execution failure rather than silently accepting a partial result.

Credentials are injected into the child environment, not model context. The
listener binds only 127.0.0.1 on an ephemeral port, requires a random token,
rejects browser-origin requests and limits request bodies to 1 MiB. Tool results
shown to Pi are bounded to 50 KiB. This is not an OS sandbox: trusted extensions
and same-user processes can access environment information.

## Lifecycle and errors

- Prompt acceptance is not completion. Wait for `agent_settled`, verify the final
  assistant stop reason and require a submitted result. Pin Pi supporting
  `agent_settled` and terminating tools (local startup checked with 0.85.1).
- Invalid submissions do not commit a result; Pi receives the validation error
  and may correct it. The compiler performs its own final validation as well.
- Capability calls go through the original compiler callback. The bridge does
  not duplicate tool events, execute external APIs directly, or grant approvals.
- Matching call_id + payload shares one cached execution, including failures.
  Reusing an ID with a different payload is rejected. This deduplication is only
  within one execution, not persistent exactly-once behavior across restarts.
- The bridge does not automatically retry calls. A new model-generated call ID
  is a new invocation. Keep device writes in explicit approved capability nodes;
  the workflow validator already forbids side-effecting agent capabilities.
- Maximum 100 distinct calls per execution. Each call and the overall RPC task
  have deadlines. Cancellation closes the Pi process and cancels bridge tasks;
  an external operation already committed cannot be rolled back by cancellation.
- Queue clearing/abort precede process kill after the shutdown grace period.
  The listener/token are discarded at the end of each execution.
- Stdout uses strict LF JSONL; stderr is separately drained with bounded memory.
  No thinking content is projected to the front end.

`tool_bridge=False` retains context-only mode, strict JSON text parsing and
rejection of nonempty capability lists, mainly for compatibility/testing.

## Real-model simulation demo

```sh
cd multica/backend
uv run python -m examples.pi_agent_demo --provider YOUR_PROVIDER --model YOUR_MODEL
# Windows: add --pi-cli "C:/.../pi-coding-agent/dist/bundle/cli.js"
```

This uses real Pi/model requests (possibly billable) with the existing fake
CapabilityRegistry/Runtime and typed anomaly workflow. It requests history data,
submits a proposal and stops at the workflow's human approval boundary. It never
auto-approves or calls real devices. The example uses an in-memory runner, so the
printed paused run is not resumable after the example process exits.

## Tests and current limits

```sh
cd multica/backend
uv run python -m unittest discover -s ../tests/runtime -v
```

Automated tests use a fake Pi subprocess that sends real authenticated HTTP
requests. They cover compiler -> RPC -> bridge -> invoke -> result -> state,
authentication, allowlists, deduplication, schema rejection/correction, deadlines
and cancellation. A real Pi startup/get_state smoke check loads the extension
without calling a model. Provider/model tool-use behavior still requires a real
model smoke test with configured credentials.

Not implemented: durable Pi session recovery, process pools, Pi-to-Pi direct
messaging, and detailed Pi activity projection. Agents cooperate through existing
Workflow state bindings. For large schemas, replace environment-based contract
transfer with a protected configuration endpoint/file to avoid OS environment
size limits.
