"""Narrow, recorded normalization of MCP tool arguments (ADR 0017).

Some MCP clients or providers re-encode native tool-call arguments on the way to the server: inside a value whose
schema they do not know (an ``object`` without properties, such as a component ``config``), an array arrives as
``{"item": [...]}`` and a number as a string (``"11"``). The lab correctly refuses those shapes. This module undoes
exactly two shapes, and only where a schema the adapter holds says what the value must be:

* ``item-wrapper``: an object with the single key ``item`` whose value is an array, where the schema says ``array``.
* ``numeric-string``: a string in canonical JSON number syntax, where the schema says ``integer`` (an integer literal
  only: ``"11"``, ``"-3"``) or ``number`` (a finite JSON number literal: ``"0.5"``, ``"1e-3"``) whose value is kept
  exactly: integer literals become exact integers; any other literal is converted only if the resulting double, as
  serialized (its shortest round-trip decimal), denotes the same decimal value, so ``"1e-400"`` (underflow) and
  ``"9007199254740993.0"`` (precision loss) are left untouched.

Anything else is left untouched for the lab to refuse: a one-element ``{"item": "x"}``, a wrapper with other keys,
``" 11"``, ``"011"``, ``"+1"``, ``"1.0"`` for an integer, ``"NaN"``, booleans, and every value whose schema is absent,
a list of types or not one of those two types. Every change is returned (path, rule, expected type, original value)
so the caller records it; the lab's validators are never relaxed: the lab validates the normalized arguments as
strictly as any others.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
MAX_NUMERIC_STRING = 64  # longer numeric strings are left untouched (no real argument needs more; int() caps at 4300)
MAX_RECORDED_ORIGINAL = MAX_NUMERIC_STRING


def _number(s: str, want: str) -> int | float | None:
    """The value of ``s`` if it is a canonical JSON literal of type ``want`` (``integer`` or ``number``)."""
    if len(s) > MAX_NUMERIC_STRING:
        return None
    if want == "integer":
        return int(s) if _INTEGER.fullmatch(s) else None
    if not _NUMBER.fullmatch(s):
        return None
    if _INTEGER.fullmatch(s):
        return int(s)
    v = float(s)
    # F2b: only a conversion that keeps the supplied decimal value is made. The float travels the command path as
    # JSON, i.e. as its shortest round-trip decimal (repr), so that decimal must equal the string's exactly:
    # "0.1" and "1e-3" pass; "1e-400" (underflows to 0.0) and "9007199254740993.0" (rounds to ...992.0) or any
    # literal with more significant digits than a double holds are left untouched for the lab to refuse.
    if not math.isfinite(v) or Decimal(repr(v)) != Decimal(s):
        return None
    return v


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def normalize(value: Any, schema: dict[str, Any] | None, path: str = "") -> tuple[Any, list[dict[str, Any]]]:
    """``(normalized value, changes)``. ``value`` is never modified in place; unchanged parts are returned as is."""
    if not isinstance(schema, dict):
        return value, []
    want = schema.get("type")
    changes: list[dict[str, Any]] = []
    if want == "array" and isinstance(value, dict) and set(value) == {"item"} and isinstance(value["item"], list):
        value = value["item"]
        changes.append({"path": path, "rule": "item-wrapper", "expected": "array",
                        "original": {"item": f"<array of {len(value)}>"}})
    elif want in ("integer", "number") and isinstance(value, str):
        n = _number(value, want)
        if n is not None:
            changes.append({"path": path, "rule": "numeric-string", "expected": want,
                            "original": value[:MAX_RECORDED_ORIGINAL], "normalized": n})
            return n, changes
        return value, changes
    if want == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        out = []
        for i, v in enumerate(value):
            nv, ch = normalize(v, schema["items"], f"{path}[{i}]")
            out.append(nv)
            changes += ch
        return (out if any(a is not b for a, b in zip(out, value, strict=True)) else value), changes
    if want == "object" and isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        props = schema["properties"]
        new = dict(value)
        for k, v in value.items():
            if k in props:
                nv, ch = normalize(v, props[k], _join(path, k))
                if ch:
                    new[k] = nv
                    changes += ch
        return (new if changes else value), changes
    return value, changes


def normalize_arguments(args: dict[str, Any], input_schema: dict[str, Any],
                        config_schema: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize a tool call's top-level arguments against the tool's MCP input schema and, when given, its
    ``config`` argument against the component's ``config_schema``. Returns ``(arguments, changes)``."""
    out, changes = normalize(args, input_schema)
    if config_schema and isinstance(out.get("config"), dict):
        cfg, ch = normalize(out["config"], config_schema, "config")
        if ch:
            out = {**out, "config": cfg}
            changes += ch
    return out, changes
