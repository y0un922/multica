"""Deliberately bounded JSON Schema dialect; never fetch remote references.

Supported keywords form a subset of Draft 2020-12. Unsupported keywords fail at
parse time rather than silently weakening compile-time checks.
"""
import json
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_serializer, model_validator


class DataSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["object", "array", "string", "number", "integer", "boolean", "null"]
    properties: dict[str, "DataSchema"] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    additionalProperties: bool = True
    items: "DataSchema | None" = None
    enum: list[JsonValue] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def valid_keywords(self):
        if self.type != "object" and self.model_fields_set & {"properties", "required", "additionalProperties"}:
            raise ValueError("properties/required/additionalProperties require object type")
        if self.type != "array" and self.items is not None:
            raise ValueError("items requires array type")
        if self.type == "array" and self.items is None:
            raise ValueError("array requires an items schema")
        if len(set(self.required)) != len(self.required):
            raise ValueError("required contains duplicates")
        if not set(self.required) <= self.properties.keys():
            raise ValueError("required fields must be declared in properties")
        raw = self.as_json_schema()
        Draft202012Validator.check_schema(raw)
        if self.enum is not None:
            if not self.enum:
                raise ValueError("enum must not be empty")
            base = {k: v for k, v in raw.items() if k != "enum"}
            for value in self.enum:
                if not Draft202012Validator(base).is_valid(value):
                    raise ValueError("enum value does not conform to its schema")
        return self

    @model_serializer
    def serialize(self):
        return self.as_json_schema()

    def as_json_schema(self, *, partial: bool = False) -> dict:
        """partial is for incrementally populated RunState.data, not tool outputs."""
        result = {"type": self.type}
        if self.description is not None:
            result["description"] = self.description
        if self.type == "object":
            result.update(properties={k: v.as_json_schema(partial=partial) for k, v in self.properties.items()},
                          required=[] if partial else self.required,
                          additionalProperties=self.additionalProperties)
        if self.type == "array":
            # Arrays are assigned atomically; item objects must be complete.
            result["items"] = self.items.as_json_schema()
        if self.enum is not None:
            result["enum"] = self.enum
        return result


class SchemaValueError(ValueError):
    def __init__(self, location: str, message: str):
        self.location = location
        super().__init__(f"{location}: {message}")


def validate_value(value, schema: DataSchema | None, location: str, *, partial=False):
    if schema is None:
        return
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaValueError(location, "value must be finite JSON data") from exc
    validator = Draft202012Validator(schema.as_json_schema(partial=partial))
    error = next(validator.iter_errors(value), None)
    if error:
        suffix = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
        raise SchemaValueError(location + suffix, error.message)


def object_schema(schema: DataSchema | None, location: str):
    if schema is not None and schema.type != "object":
        raise ValueError(f"{location} must have object type")


def at_path(schema: DataSchema | None, parts: list[str], *, guaranteed=False) -> DataSchema | None:
    """None means unknown (legacy/open object), not a claim of compatibility."""
    for part in parts:
        if schema is None:
            return None
        if schema.type != "object":
            raise ValueError(f"cannot access {part} through {schema.type}")
        if part not in schema.properties:
            if schema.additionalProperties:
                return None
            raise ValueError(f"undeclared field {part}")
        if guaranteed and part not in schema.required:
            raise ValueError(f"field {part} is optional, not guaranteed")
        schema = schema.properties[part]
    return schema


def compatible(source: DataSchema | None, target: DataSchema | None) -> bool:
    """Conservative assignability for the supported dialect; unknown defers to runtime."""
    if source is None or target is None:
        return True
    if source.enum is not None:
        validator = Draft202012Validator(target.as_json_schema())
        return all(validator.is_valid(value) for value in source.enum)
    if target.enum is not None:
        return False
    if source.type != target.type:
        return source.type == "integer" and target.type == "number"
    if source.type == "array":
        return compatible(source.items, target.items)
    if source.type == "object":
        if not set(target.required) <= set(source.required):
            return False
        if not target.additionalProperties and (
            source.additionalProperties or not source.properties.keys() <= target.properties.keys()
        ):
            return False
        for key, target_field in target.properties.items():
            if key in source.properties:
                if not compatible(source.properties[key], target_field):
                    return False
            elif source.additionalProperties:
                # An undeclared field could have any type, including an incompatible one.
                return False
    return True
