"""Adapt a supplied GET /api/tools response without importing B internals.

HTTP/auth/loading belongs to the composition root. Unsupported JSON Schema
keywords fail explicitly rather than silently weakening workflow validation.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .interfaces import CapabilityInfo


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    description: str
    category: Literal["data", "analysis", "action"]
    side_effect: bool
    timeout_seconds: float = Field(default=30.0, gt=0)
    # Preserve the complete provider JSON Schema verbatim. Validation of tool
    # arguments/results is performed with jsonschema, not a private subset.
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class CatalogRegistry:
    def __init__(self, entries: list[dict] | list[CatalogEntry], *,
                 confirmation_tools: set[str] | None = None):
        """confirmation_tools is A policy, not an invented B catalog field."""
        parsed = TypeAdapter(list[CatalogEntry]).validate_python(entries)
        names = [entry.name for entry in parsed]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tool name in catalog")
        confirmation_tools = set(confirmation_tools or ())
        if confirmation_tools - set(names):
            raise ValueError("confirmation policy references tools absent from catalog")
        self._entries = {entry.name: entry.model_copy(deep=True) for entry in parsed}
        self._capabilities = {
            entry.name: CapabilityInfo(
                id=entry.name, side_effect=entry.side_effect,
                requires_approval=entry.name in confirmation_tools,
                input_schema=entry.input_schema, output_schema=entry.output_schema,
            ) for entry in parsed
        }

    def get(self, capability_id: str) -> CapabilityInfo | None:
        from copy import deepcopy
        return deepcopy(self._capabilities.get(capability_id))

    def search(self, query: str = "") -> list[CatalogEntry]:
        query = query.casefold()
        return [entry.model_copy(deep=True) for entry in self._entries.values()
                if query in entry.name.casefold() or query in entry.description.casefold()]
