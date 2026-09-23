"""Public tool boundary from BACKEND_AB_INTERFACE.MD v0.1.

No platform implementation or workflow state is imported here.
"""
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .json_types import JsonObject, JsonValue


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolStatus(str, Enum):
    OK = "ok"
    RETRYABLE_ERROR = "retryable_error"
    FATAL_ERROR = "fatal_error"


class ErrorCategory(str, Enum):
    VALIDATION_ERROR = "validation_error"
    RESOURCE_NOT_FOUND = "resource_not_found"
    TIMEOUT = "timeout"
    EXTERNAL_SERVICE_ERROR = "external_service_error"
    PERMISSION_DENIED = "permission_denied"
    CONFLICT = "conflict"
    BUSINESS_ERROR = "business_error"
    INTERNAL_ERROR = "internal_error"


class ToolContext(ContractModel):
    run_id: str
    node_id: str | None = None
    project_id: str | None = None
    user_id: str | None = None
    trace_id: str


class ToolError(ContractModel):
    category: ErrorCategory
    code: str
    message: str
    retryable: bool
    details: JsonObject | None = None


class ToolResult(ContractModel):
    """Success data is strict Output Model-validated, JSON-exported provider data.

    This envelope checks JSON validity, not the tool-specific output schema.
    """
    status: ToolStatus
    data: JsonValue = None
    error: ToolError | None = None
    metadata: JsonObject = Field(default_factory=dict)


class ToolInvocationError(RuntimeError):
    def __init__(self, result: ToolResult, *, tool_name: str, node_id: str | None):
        self.tool_name = tool_name
        self.node_id = node_id
        self.status = result.status
        self.error = result.error
        self.category = result.error.category if result.error else None
        self.code = result.error.code if result.error else None
        self.message = result.error.message if result.error else "tool failed without error details"
        super().__init__(f"{tool_name} at {node_id}: {self.status.value} "
                         f"[{self.category}/{self.code}] {self.message}")


def require_ok(result: ToolResult, *, tool_name: str, node_id: str | None = None) -> JsonValue:
    """Return successful data unchanged; never turn a failed tool into empty data."""
    if result.status != ToolStatus.OK:
        raise ToolInvocationError(result, tool_name=tool_name, node_id=node_id)
    return result.data


class ToolRuntime(Protocol):
    async def invoke(self, tool_name: str, arguments: JsonObject,
                     context: ToolContext) -> ToolResult: ...
