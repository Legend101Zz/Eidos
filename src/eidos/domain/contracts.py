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
