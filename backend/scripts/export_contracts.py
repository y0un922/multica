"""Export A models and A/B v0.1 envelope mirrors for integration review."""
import json
from pathlib import Path

from pydantic import TypeAdapter

from runtime.execution import RunSnapshot, AgentEvent, Confirmation
from runtime.workflow.tool_contracts import ToolContext, ToolResult
from runtime.workflow import WorkflowSpec

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def schemas():
    return {
        "workflow.schema.json": WorkflowSpec.model_json_schema(),
        "run_snapshot.schema.json": TypeAdapter(RunSnapshot).json_schema(),
        "agent_event.schema.json": AgentEvent.model_json_schema(),
        "confirmation.schema.json": Confirmation.model_json_schema(),
        "tool_context.schema.json": ToolContext.model_json_schema(),
        "tool_result.schema.json": ToolResult.model_json_schema(),
    }


def main():
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, schema in schemas().items():
        (CONTRACTS_DIR / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(filename)


if __name__ == "__main__":
    main()
