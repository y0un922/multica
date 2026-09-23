"""Validation for provider-owned JSON Schema documents."""
from typing import Any
from jsonschema import Draft202012Validator


def validate_json_schema(value: Any, schema: dict[str, Any], path: str) -> None:
    """Validate without reducing B's schema to A's legacy schema subset."""
    try:
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    except Exception as exc:
        raise ValueError(f"{path}: invalid JSON Schema: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.path)
        suffix = f" at {location}" if location else ""
        raise ValueError(f"{path}{suffix}: {error.message}")
