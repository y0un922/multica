"""Application snapshots, separate from LangGraph's internal checkpoint state."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

RunStatus = Literal["pending", "running", "waiting_approval", "completed", "failed"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunSnapshot:
    run_id: str
    workflow_id: str
    workflow_version: str
    status: RunStatus
    state: dict[str, Any]
    approval: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
