"""Component configuration contracts: a small, dependency-free JSON-Schema subset.

A release may declare ``config_schema`` in its manifest. It is shown to investigators in the
catalog, enforced when a decision is accepted (so "schema-valid" means "runnable"), and enforced
again at resolution. Supported keywords: type, required, properties, additionalProperties (bool),
items, minItems, maxItems, enum, pattern, minimum, maximum, minLength, ``firstItemPattern``
(regex the first array item must match, e.g. an interpreter or script) and ``anyOf`` (a list of schemas; the value
must satisfy at least one, as well as the rest of the schema).
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, type | tuple[type, ...]] = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}


def validate_config(value: Any, schema: dict[str, Any], path: str = "config") -> list[str]:
    problems: list[str] = []
    t = schema.get("type")
    if t:
        py = _TYPES.get(t)
        ok = isinstance(value, py) if py else True
        if t in ("integer", "number") and isinstance(value, bool):
            ok = False
        if not ok:
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not one of {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{path}: {value!r} does not match {schema['pattern']!r}")
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: at most {schema['maxItems']} items")
        if value and "firstItemPattern" in schema and not (isinstance(value[0], str)
                                                           and re.search(schema["firstItemPattern"], value[0])):
            problems.append(f"{path}[0]: {value[0]!r} does not match {schema['firstItemPattern']!r}")
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(value):
                problems += validate_config(v, schema["items"], f"{path}[{i}]")
    if isinstance(schema.get("anyOf"), list) and schema["anyOf"]:
        branches = [validate_config(value, branch, path) for branch in schema["anyOf"]]
        if all(branches):
            problems.append(f"{path}: matches none of its alternatives (" + "; or ".join(
                ", ".join(b) for b in branches) + ")")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                problems.append(f"{path}.{k}: required")
        props = schema.get("properties") or {}
        for k, v in value.items():
            if k in props:
                problems += validate_config(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}.{k}: not allowed")
    return problems


def validate_shape_schema(schema: Any, path: str, depth: int = 0) -> list[str]:
    """Validate the bounded subset used by operator-declared conclusion scope contracts."""
    if not isinstance(schema, dict) or not schema or depth > 8:
        return [f"{path}: expected a nonempty schema object within depth 8"]
    supported = {"type", "required", "properties", "items", "enum", "anyOf", "minLength"}
    problems = [f"{path}.{key}: unsupported keyword" for key in schema if key not in supported]
    kind = schema.get("type")
    if "type" in schema and (not isinstance(kind, str) or kind not in _TYPES):
        problems.append(f"{path}.type: unsupported type")
    if "required" in schema and (kind != "object" or not isinstance(schema["required"], list)
            or not all(isinstance(key, str) and key for key in schema["required"])):
        problems.append(f"{path}.required: expected property names for an object")
    if "properties" in schema:
        properties = schema["properties"]
        if kind != "object" or not isinstance(properties, dict):
            problems.append(f"{path}.properties: expected a schema map for an object")
        else:
            for key, child in properties.items():
                problems.extend(validate_shape_schema(child, f"{path}.properties.{key}", depth + 1))
    if "items" in schema:
        if kind != "array":
            problems.append(f"{path}.items: requires array type")
        else:
            problems.extend(validate_shape_schema(schema["items"], f"{path}.items", depth + 1))
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        problems.append(f"{path}.enum: expected a nonempty list")
    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            problems.append(f"{path}.anyOf: expected a nonempty schema list")
        else:
            for index, child in enumerate(alternatives):
                problems.extend(validate_shape_schema(child, f"{path}.anyOf[{index}]", depth + 1))
    if "minLength" in schema and (kind != "string" or type(schema["minLength"]) is not int
            or schema["minLength"] < 0):
        problems.append(f"{path}.minLength: expected a nonnegative integer for a string")
    return problems
