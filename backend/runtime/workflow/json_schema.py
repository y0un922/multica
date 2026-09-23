"""Validation for provider-owned JSON Schema documents."""
from typing import Any
from jsonschema import Draft202012Validator

from .schema import SchemaValueError


def check_provider_schema(schema: dict[str, Any]) -> None:
    """Validate provider schema and forbid remote references at the A boundary."""
    Draft202012Validator.check_schema(schema)

    def check_refs(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef"}:
                    if not isinstance(value, str) or not value.startswith("#/$defs/"):
                        raise ValueError("provider JSON Schema contains unsupported reference")
                elif key == "$recursiveRef":
                    # Draft 2020-12 ignores the older draft's recursive keyword.
                    raise ValueError("provider JSON Schema contains unsupported recursive reference")
                elif key in {"properties", "patternProperties", "$defs", "definitions",
                             "dependentSchemas"}:
                    # Map keys are user-defined names, including possibly '$ref'.
                    for child in value.values():
                        check_refs(child)
                elif key not in {"enum", "const", "default", "examples"}:
                    check_refs(value)
        elif isinstance(node, list):
            for value in node:
                check_refs(value)

    check_refs(schema)


def validate_json_schema(value: Any, schema: dict[str, Any] | None, path: str) -> None:
    """Validate provider Schema; legacy v1.0 untyped capabilities remain supported."""
    if schema is None:
        return
    try:
        check_provider_schema(schema)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    except Exception as exc:
        raise ValueError(f"{path}: invalid JSON Schema: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.path)
        suffix = f" at {location}" if location else ""
        raise SchemaValueError(path + suffix, error.message)
