"""Bounded structured-output repair for models that do not reliably produce the decision schema (ADR 0014).

G4 (v1.0.0) found that local models (qwen2.5 3B/7B/14B) failed decision-schema conformance mostly through malformed
output and exhausted output retries. This module is the text side of the repair layer:

- ``extract_json``: strict extraction of ONE JSON object from a reply: code fences, leading/trailing prose,
  trailing commas, and Python-style literals/single quotes (parsed with ``ast.literal_eval``, never ``eval``).
  Every transformation is returned as a named repair; nothing is silent.
- ``normalize``: lenient field shapes the prompt allows (``args`` as an object, ``scope`` as an object) mapped onto
  the typed ``Decision`` model (``args_json``/``scope_json`` strings), recorded as repairs too.
- ``validation_message``: a short, schema-guided error for the single re-ask.

The runtime (``PydanticAIRuntime`` with ``output_mode`` ``repair`` or ``json``) decides when to re-ask and counts
re-asks and repairs in the decision's usage and settings, so receipts and budgets show them.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)
TRAILING_COMMA = re.compile(r",\s*([}\]])")


class StructuredOutputError(ValueError):
    def __init__(self, message: str, repairs: list[str] | None = None):
        super().__init__(message)
        self.repairs = list(repairs or [])


def _balanced_object(s: str) -> tuple[str, int, int] | None:
    """The first balanced {...} in ``s`` (string-aware), with its start and end offsets."""
    start = s.find("{")
    while start != -1:
        depth, i, quote, esc = 0, start, None, False
        while i < len(s):
            ch = s[i]
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1], start, i + 1
            i += 1
        start = s.find("{", start + 1)
    return None


def extract_json(text: str) -> tuple[dict[str, Any], list[str]]:
    """Extract one JSON object from a model reply. Returns (object, repairs applied)."""
    repairs: list[str] = []
    s = (text or "").strip()
    if not s:
        raise StructuredOutputError("the reply is empty", repairs)
    m = FENCE.search(s)
    outside = {"leading-prose": False, "trailing-prose": False}
    if m:
        outside = {"leading-prose": bool(s[:m.start()].strip()), "trailing-prose": bool(s[m.end():].strip())}
        s = m.group(1).strip()
        repairs.append("code-fence")
    found = _balanced_object(s)
    if found is None:
        raise StructuredOutputError("no JSON object found in the reply", repairs)
    candidate, start, end = found
    if s[:start].strip() or outside["leading-prose"]:
        repairs.append("leading-prose")
    if s[end:].strip() or outside["trailing-prose"]:
        repairs.append("trailing-prose")
    try:
        obj = json.loads(candidate)
    except ValueError:
        fixed = TRAILING_COMMA.sub(r"\1", candidate)
        try:
            obj = json.loads(fixed)
            repairs.append("trailing-comma")
        except json.JSONDecodeError as exc:
            py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", fixed)))
            try:
                obj = ast.literal_eval(py)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                raise StructuredOutputError(f"the reply is not valid JSON ({exc.msg} at char {exc.pos})",
                                            repairs) from None
            if fixed != candidate:
                repairs.append("trailing-comma")
            repairs.append("single-quotes-or-python-literals")
            obj = json.loads(json.dumps(obj))  # literal_eval may yield tuples/sets: back to JSON types
    if not isinstance(obj, dict):
        raise StructuredOutputError("the reply's JSON is not an object", repairs)
    return obj, repairs


def normalize(obj: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Map the prompt's lenient shapes onto the typed Decision fields."""
    repairs: list[str] = []
    out = dict(obj)
    calls = []
    for c in out.get("calls") or []:
        if not isinstance(c, dict):
            calls.append(c)
            continue
        c = dict(c)
        if "args_json" not in c and "args" in c:
            args = c.pop("args")
            c["args_json"] = args if isinstance(args, str) else json.dumps(args if args is not None else {})
            repairs.append("args-object")
        elif isinstance(c.get("args_json"), dict):
            c["args_json"] = json.dumps(c["args_json"])
            repairs.append("args-object")
        calls.append(c)
    if "calls" in out:
        out["calls"] = calls
    ctl = out.get("control")
    if isinstance(ctl, str):
        out["control"] = {"action": ctl}
        repairs.append("control-string")
    elif isinstance(ctl, dict) and "scope" in ctl and "scope_json" not in ctl:
        ctl = dict(ctl)
        scope = ctl.pop("scope")
        ctl["scope_json"] = scope if isinstance(scope, str) else json.dumps(scope)
        out["control"] = ctl
        repairs.append("scope-object")
    return out, sorted(set(repairs))


def validation_message(exc: Exception, limit: int = 1200) -> str:
    """A compact error for the re-ask: field paths and messages, never the whole reply."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            parts = [f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in errors()[:12]]
            return "; ".join(parts)[:limit]
        except Exception:  # noqa: BLE001 - fall back to the plain message
            pass
    return str(exc)[:limit]


JSON_SHAPE = """Reply with ONE JSON object and nothing else (no prose, no code fence):
{"rationale": "<why>", "purpose": "reproduction|measurement-development|open-search|hypothesis-test|confirmation",
 "hypotheses": [{"statement": "...", "prediction": "...", "falsifier": "..."}],
 "calls": [{"tool": "<effect tool name>", "args": {<that tool's arguments>}}],
 "control": {"action": "wait_for_jobs|request_input|conclude|stop", "summary": "...", "scope": {}}}
Use double quotes, no trailing commas, no comments. "calls" may be empty; "control" may be omitted."""
