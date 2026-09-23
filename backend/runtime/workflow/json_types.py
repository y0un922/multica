"""Strict JSON values shared by public tool and event boundaries.

Do not coerce Python objects to JSON here. Tool providers must first validate
with their strict Output Model, then export JSON before constructing ToolResult.
"""
import math
from typing import Annotated

from pydantic import BeforeValidator, JsonValue as PydanticJsonValue, TypeAdapter


def validate_json(value):
    """Reject non-JSON Python objects, non-string keys and non-finite numbers."""
    def visit(item, ancestors):
        kind = type(item)
        if item is None or kind in (str, bool, int):
            return
        if kind is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return
        if kind not in (list, dict):
            raise ValueError(f"not a JSON value: {kind.__name__}")
        if id(item) in ancestors:
            raise ValueError("JSON values cannot contain cycles")
        ancestors.add(id(item))
        try:
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError("JSON object keys must be strings")
                    visit(child, ancestors)
            else:
                for child in item:
                    visit(child, ancestors)
        finally:
            ancestors.remove(id(item))

    visit(value, set())
    return value


JsonValue = Annotated[PydanticJsonValue, BeforeValidator(validate_json)]
JsonObject = Annotated[dict[str, JsonValue], BeforeValidator(validate_json)]
JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)
JSON_OBJECT_ADAPTER = TypeAdapter(JsonObject)
