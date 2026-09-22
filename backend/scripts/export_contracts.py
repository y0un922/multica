"""Export Backend A owned contracts: uv run python -m scripts.export_contracts."""
import json
from pathlib import Path

from pydantic import TypeAdapter

from runtime.execution import RunSnapshot
from runtime.workflow import WorkflowSpec

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def schemas():
    return {
        "workflow.schema.json": WorkflowSpec.model_json_schema(),
        "run_snapshot.schema.json": TypeAdapter(RunSnapshot).json_schema(),
    }


def main():
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, schema in schemas().items():
        (CONTRACTS_DIR / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(filename)


if __name__ == "__main__":
    main()
