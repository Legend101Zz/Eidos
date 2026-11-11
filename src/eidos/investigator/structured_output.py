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
# Reasoning models served through OpenAI-compatible endpoints may inline their reasoning as <think>...</think>
# before the answer; braces inside it must not be mistaken for the reply's JSON object.
THINK = re.compile(r"^\s*<think>.*?</think>", re.S)


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
    think = THINK.match(s)
    if think:
        s = s[think.end():].strip()
        repairs.append("think-block")
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
                obj = json.loads(json.dumps(obj))  # sets, bytes, complex, non-string keys: not JSON
            except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError, OverflowError):
                raise StructuredOutputError(f"the reply is not valid JSON ({exc.msg} at char {exc.pos})",
                                            repairs) from None
            if fixed != candidate:
                repairs.append("trailing-comma")
            repairs.append("single-quotes-or-python-literals")
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
 "control": {"action": "wait_for_jobs|request_input|conclude|stop", "summary": "...", "scope": {}, "claim": {}}}
Use double quotes, no trailing commas, no comments. "calls" may be empty; "control" may be omitted."""


# -- read requests (ADR 0016) -------------------------------------------------------------------------------------
# In the text modes the reply is either a decision or a read request: {"rationale": ..., "read": [{tool, args}]}.
# The runtime executes the allowed read tools, sends the results back and asks again; every round is one of the
# decision's model requests. Read tools are never effects: a decision that names one in "calls" is refused.
REQUIRED = object()
READ_TOOLS: dict[str, tuple[tuple[str, tuple[type, ...], Any], ...]] = {
    "inspect_evidence": (("artifact_id", (str,), REQUIRED), ("max_bytes", (int,), 8192)),
    "search_components": (("text", (str,), ""), ("capability", (str, type(None)), None)),
    "describe_component": (("component_ref", (str,), REQUIRED),),
    "get_job": (("job_id", (str,), REQUIRED),),
}
READ_TOOL_DOCS = {
    "inspect_evidence": '{"artifact_id": "<id>", "max_bytes": <optional int, at most 16384>}: bounded preview of an '
                        "artifact's content (text/JSON/CSV)",
    "search_components": '{"text": "<words>", "capability": "<optional>"}: search the installed component catalog',
    "describe_component": '{"component_ref": "<id@release>"}: manifest, qualification and approvals of one release',
    "get_job": '{"job_id": "<id>"}: status, receipts and outputs of one job',
}
MAX_READS_PER_REQUEST = 4
MAX_INSPECT_BYTES = 16384


def read_shape(tools: list[str]) -> str:
    """Prompt text for the read-request shape, listing only the read tools available to this decision."""
    lines = "\n".join(f"- {t} {READ_TOOL_DOCS[t]}" for t in tools)
    return ("To read evidence before deciding, reply INSTEAD with ONE JSON object of this shape (no \"calls\", "
            "no \"control\"):\n"
            '{"rationale": "<what you need to check>", "read": [{"tool": "<read tool>", "args": {<its arguments>}}]}\n'
            f"Read tools (read-only; never put them in \"calls\"):\n{lines}\n"
            f"The lab runs the reads (at most {MAX_READS_PER_REQUEST} per request) and sends you the results. Every "
            "read request uses one of your model requests for this decision; reserve the last one for your decision.")


def split_read_request(obj: dict[str, Any], allowed: list[str]) -> tuple[list[Any] | None, dict[str, Any], list[str]]:
    """Classify a reply object. Returns (reads, decision object, repairs): ``reads`` is None for a decision.

    Structural problems raise ``StructuredOutputError`` (the reply is then re-asked once, as any malformed reply);
    problems of a single read (unknown tool, bad arguments) are reported back to the model by the runtime."""
    if "read" not in obj:
        return None, obj, []
    reads = obj["read"]
    rest = {k: v for k, v in obj.items() if k != "read"}
    if reads is None or reads == []:
        return None, rest, ["empty-read"]
    if not allowed:
        raise StructuredOutputError('no read tools are available for this decision: reply with a decision, '
                                    'without "read"')
    if not isinstance(reads, list):
        raise StructuredOutputError('"read" must be a list of {"tool": ..., "args": {...}} objects')
    if obj.get("calls") or obj.get("control"):
        raise StructuredOutputError('a reply is either a read request or a decision, not both: drop "read" or drop '
                                    '"calls" and "control"')
    return reads, rest, []


def check_read(entry: Any, allowed: list[str]) -> tuple[str, dict[str, Any]]:
    """Validate one read: a known, allowed read tool with exactly its arguments. Returns (tool, arguments in
    signature order with defaults) or raises ``ValueError`` with a message for the model."""
    if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
        raise ValueError('each read must be {"tool": "<read tool>", "args": {...}}')
    tool = entry["tool"]
    if tool not in allowed:
        raise ValueError(f"{tool!r} is not an available read tool (available: {', '.join(allowed)})")
    args = entry.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise ValueError(f"{tool}: args is not a JSON object") from None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(f"{tool}: args must be a JSON object")
    spec = READ_TOOLS[tool]
    unknown = sorted(set(args) - {name for name, _, _ in spec})
    if unknown:
        raise ValueError(f"{tool}: unknown argument(s) {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for name, types, default in spec:
        if name not in args:
            if default is REQUIRED:
                raise ValueError(f"{tool}: missing required argument {name!r}")
            out[name] = default
            continue
        value = args[name]
        if not isinstance(value, types) or isinstance(value, bool):
            raise ValueError(f"{tool}: argument {name!r} has the wrong type")
        out[name] = value
    if tool == "inspect_evidence":
        out["max_bytes"] = max(1, min(int(out["max_bytes"]), MAX_INSPECT_BYTES))
    return tool, out


def read_tools_in_calls(calls: list[dict[str, Any]], allowed: list[str]) -> str | None:
    """A message for the re-ask when a decision puts a read tool into its effect calls, else None."""
    bad = [(i, c.get("tool")) for i, c in enumerate(calls) if c.get("tool") in READ_TOOLS]
    if not bad:
        return None
    named = ", ".join(f"calls.{i}.tool {t!r}" for i, t in bad)
    how = ('to read evidence, reply with a read request {"rationale": ..., "read": [...]} instead of a decision'
           if allowed else "no read tools are available for this decision; decide with the evidence you have")
    return f"{named}: read tools are not effects and never belong in \"calls\"; {how}"
