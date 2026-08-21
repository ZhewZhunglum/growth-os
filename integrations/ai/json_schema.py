from __future__ import annotations

from typing import Any, Mapping

from integrations.errors import StructuredOutputError


def validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the deliberately small JSON Schema subset used by V1 adapters."""

    if "enum" in schema and value not in schema["enum"]:
        raise StructuredOutputError(f"{path} is not one of the allowed enum values")

    expected = schema.get("type")
    if isinstance(expected, list):
        errors = []
        for candidate in expected:
            try:
                validate_json_schema(value, {**schema, "type": candidate}, path)
                return
            except StructuredOutputError as exc:
                errors.append(str(exc))
        raise StructuredOutputError(f"{path} does not match any allowed JSON type")

    if expected == "object":
        if not isinstance(value, dict):
            raise StructuredOutputError(f"{path} must be an object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise StructuredOutputError(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise StructuredOutputError(f"{path} contains unexpected fields: {sorted(unexpected)}")
        for key, child in value.items():
            if key in properties:
                validate_json_schema(child, properties[key], f"{path}.{key}")
        return

    if expected == "array":
        if not isinstance(value, list):
            raise StructuredOutputError(f"{path} must be an array")
        if len(value) < schema.get("minItems", 0):
            raise StructuredOutputError(f"{path} contains too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise StructuredOutputError(f"{path} contains too many items")
        if "items" in schema:
            for index, child in enumerate(value):
                validate_json_schema(child, schema["items"], f"{path}[{index}]")
        return

    type_matches = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected in type_matches and not type_matches[expected]:
        raise StructuredOutputError(f"{path} must be a {expected}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise StructuredOutputError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise StructuredOutputError(f"{path} is longer than maxLength")
