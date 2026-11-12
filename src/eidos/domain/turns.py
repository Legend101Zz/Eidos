"""Bounded investigator turns, accepted decisions and stable tool vocabulary.

Design §"Bounded investigator turns and recovery" / §"Stable intent before effects".
"""

from __future__ import annotations

import re
from typing import Any

from eidos.domain.assessment import validate_claim_block
from eidos.domain.errors import Invalid, PayloadTooLarge
from eidos_wire import canonical_json, command_key

# Application payload envelope for anything crossing a workflow boundary. Temporal's hard blob
# limit is 2 MiB (warning at 512 KiB); we stay far below and stage larger content as artifacts.
WORKFLOW_PAYLOAD_LIMIT = 128 * 1024
MODEL_CONTEXT_CHAR_LIMIT = 60_000
FINALIZATION_READ_ROUNDS = 2

# Stable effectful tool vocabulary. Installed components are catalog data, never new tools.
EFFECT_TOOLS = {
    "run_component": "run_component",
    "plan_trial": "plan_trial",
    "launch_trial": "launch_trial",
    "plan_branch": "plan_branch",
    "launch_branch": "launch_branch",
    "author_component": "validate_component",  # composite: upload source bundle, then validate (builds a component)
    "author_source": "register_artifact",  # candidate project files as a source bundle (run_component inputs.source)
    "publish_component": "publish_component",
    "bind_component": "bind_component",
    "retire_component": "retire_component",
    "file_claim": "file_claim",
    "draft_evidence_plan": "draft_evidence_plan",
    "freeze_evidence_plan": "freeze_evidence_plan",
    "launch_evidence_plan": "launch_evidence_plan",
    "assess_candidate": "assess_candidate",
    "export_model": "export_model",
    "record_note": "record_note",
}
# Arguments each effect tool cannot run without. A call missing any of them is rejected before
# acceptance, so "schema-valid" also means "executable" (live provider finding: strict-mode JSON
# schemas can silently drop free-form objects).
REQUIRED_ARGS = {
    "run_component": ("component_ref",), "plan_trial": ("component_ref",), "launch_trial": ("trial_id",),
    "plan_branch": ("parent_checkpoint_id", "component_ref", "migration", "stream_policy"),
    "launch_branch": ("trial_id",), "author_component": ("files",), "author_source": ("files",),
    "publish_component": ("bundle_artifact_id", "validation_job_id"), "bind_component": ("component_ref",),
    "retire_component": ("component_ref",), "file_claim": ("statement", "kind", "scope"),
    "draft_evidence_plan": ("claim_id", "charter_id", "template_ref"), "freeze_evidence_plan": ("plan_id",),
    "launch_evidence_plan": ("plan_id",), "assess_candidate": ("success_rule_id",), "export_model": ("checkpoint_artifact_id", "behavior_check_job_id"),
    "record_note": ("text",),
}
CONTROL_TOOLS = ("wait_for_jobs", "request_input", "conclude", "stop")
READ_TOOLS = ("inspect_evidence", "search_components", "describe_component", "get_job", "read_artifact")
STOP_REASONS = ("calls-exhausted", "invalid-proposals", "waiting", "input-requested", "concluded", "budget-exhausted",
                "deadline", "cancelled", "error", "stale-revision", "runtime-bounded")
CONCLUDING_ACTIONS = ("conclude", "stop")


# ADR 0020: the wall reserved for the conclusion, unless the investigation configures its own threshold.
CONCLUDE_FRACTION = 0.10
MIN_CONCLUDE_SECONDS = 60.0
MAX_CONCLUDE_SECONDS = 600.0
MIN_TURN_SECONDS = 10
WALL_REACHED = "wall-deadline-reached"


def conclude_threshold(wall_seconds: float, configured: float | None = None) -> float:
    if configured is not None:
        return max(0.0, float(configured))
    return min(MAX_CONCLUDE_SECONDS, max(MIN_CONCLUDE_SECONDS, CONCLUDE_FRACTION * float(wall_seconds)))


def completion_state(deadline_unix: float, now: float, threshold: float) -> dict[str, Any]:
    """The turn's view of the investigation's wall deadline: recorded in the turn spec and shown to the investigator."""
    remaining = float(deadline_unix) - now
    return {"deadline_unix": float(deadline_unix), "remaining_seconds": round(remaining, 3),
            "conclude_threshold_seconds": float(threshold), "conclude_now": remaining <= threshold}


def turn_seconds(configured: int, completion: dict[str, Any]) -> int:
    """How long a decision of this turn may take. Earlier turns stop where the conclusion's reserve begins, so the
    last turn still has its time; the concluding turn may use what is left."""
    remaining = float(completion["remaining_seconds"])
    usable = remaining if completion["conclude_now"] else remaining - float(completion["conclude_threshold_seconds"])
    return max(MIN_TURN_SECONDS, min(int(configured), int(usable)))


def turn_boundary(completion: dict[str, Any]) -> float:
    """When a turn must be over, retries and execution included: where the conclusion's reserve begins, or for the concluding
    turn the wall deadline itself. An exploratory turn (R14 #4, #5: ``exploration_deadline_unix``, set by begin_turn)
    ends where the reproduction hold-back begins, so neither its activities nor their retries can spend it."""
    deadline = float(completion["deadline_unix"])
    if completion.get("conclude_now") or completion.get("finalization"):
        return deadline
    if completion.get("exploration_deadline_unix") is not None:
        return min(float(completion["exploration_deadline_unix"]), deadline - float(completion["conclude_threshold_seconds"]))
    return deadline - float(completion["conclude_threshold_seconds"])


# ADR 0022: dependent batches. One accepted decision may name an earlier call's result in a later call's arguments as
# {"$ref": "call[<index>].<field>"} or {"$ref": "call[<index>].outputs.<slot>"}; the persisted decision keeps the
# reference, and the investigator resolves it only when the dependent call is executed. Calls are ordered, so a call can
# depend on earlier calls only: a forward or self reference (the only way to write a cycle) is refused.
REF_KEY = "$ref"
# (?!\n): Python's $ also matches before a final newline; the published pattern must mean the same in every engine.
REF_PATTERN = r"^call\[(0|[1-9][0-9]*)\]\.(outputs\.[A-Za-z0-9_\-]+|[a-z_]+)(?!\n)$"
_REF = re.compile(REF_PATTERN)
# Calls that admit a job. They queue sequentially under the one-CPU policy (a decision may ask for concurrency only under
# an explicit resource policy the project allows), and a reference to one of them means "after it has succeeded".
JOB_TOOLS = ("run_component", "launch_trial", "launch_branch", "author_component")
# Calls that admit several evidence-arm jobs over time. ADR 0022 addendum R11: they use the same CPU allowance as JOB_TOOLS
# and hold it until the plan's last job has ended; ``plan_id`` is known at launch, so a reference to one does not wait.
PLAN_TOOLS = ("launch_evidence_plan", "assess_candidate")
COMPUTE_TOOLS = (*JOB_TOOLS, *PLAN_TOOLS)
# What each tool's receipt can be asked for. Jobs also expose outputs.<slot> once they have succeeded.
RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "run_component": ("job_id", "trial_id", "lock_digest", "component"),
    "plan_trial": ("trial_id", "lock_digest"), "launch_trial": ("job_id", "trial_id", "lock_digest"),
    "plan_branch": ("trial_id", "branch_id", "lock_digest"), "launch_branch": ("job_id", "trial_id", "lock_digest"),
    "author_component": ("job_id", "component_ref", "bundle_artifact_id"), "author_source": ("artifact_id",),
    "publish_component": ("component_ref", "digest"), "file_claim": ("claim_id",), "draft_evidence_plan": ("plan_id",),
    "freeze_evidence_plan": ("plan_id", "plan_digest"), "launch_evidence_plan": ("plan_id",),
    "assess_candidate": ("plan_id", "claim_id", "request_id", "status", "plan_digest"), "export_model": ("export_id",),
}
# Literal by contract: an accepted decision has been checked against this component and environment (accepted means
# runnable), which a value known only at execution cannot be.
LITERAL_ARGS = ("component_ref", "environment", "investigation_id", "purpose")
MAX_CONCURRENT_JOBS_KEY = "max_concurrent_jobs"


def check_payload(obj: Any, *, limit: int = WORKFLOW_PAYLOAD_LIMIT, what: str = "payload") -> int:
    size = len(canonical_json(obj).encode())
    if size > limit:
        raise PayloadTooLarge(f"{what} is {size} bytes, above the {limit}-byte workflow envelope",
                              details={"size": size, "limit": limit})
    return size


def validate_decision(proposal: dict[str, Any], *, max_calls: int) -> dict[str, Any]:
    """Validate a model's proposed decision before it can be accepted.

    proposal = {"rationale", "hypotheses": [...], "calls": [{"tool", "args"}], "control": {...}}
    """
    calls = proposal.get("calls") or []
    if not isinstance(calls, list):
        raise Invalid("calls must be a list")
    if len(calls) > max_calls:
        raise Invalid(f"decision has {len(calls)} calls; this turn allows {max_calls}")
    for i, c in enumerate(calls):
        if c.get("tool") not in EFFECT_TOOLS:
            raise Invalid(f"call {i}: unknown or non-effect tool {c.get('tool')!r}",
                          details={"allowed": sorted(EFFECT_TOOLS)})
        if not isinstance(c.get("args"), dict):
            raise Invalid(f"call {i}: args must be an object")
        missing = [a for a in REQUIRED_ARGS.get(c["tool"], ()) if c["args"].get(a) in (None, "", {}, [])]
        if missing:
            raise Invalid(f"call {i} ({c['tool']}): missing required args {missing}",
                          details={"tool": c["tool"], "missing": missing})
    _validate_refs(calls)
    resource_policy = _resource_policy(proposal.get("resource_policy"))
    control = proposal.get("control") or {}
    if control and control.get("action") not in CONTROL_TOOLS:
        raise Invalid(f"unknown control action {control.get('action')!r}")
    if control.get("action") in ("conclude", "stop") and control.get("claim") is not None:
        validate_claim_block(control["claim"])  # ADR 0021
    for c in calls:
        if c["tool"] in ("plan_trial", "launch_trial", "draft_evidence_plan") and "purpose" not in c["args"]:
            c["args"]["purpose"] = proposal.get("purpose", "open-search")
    return {"rationale": str(proposal.get("rationale", ""))[:4000],
            "hypotheses": proposal.get("hypotheses") or [],
            "purpose": proposal.get("purpose", "open-search"),
            "calls": calls, "control": control, **({"resource_policy": resource_policy} if resource_policy else {})}


def iter_refs(value: Any, path: str = "") -> Any:
    """Yield (path, reference text) for every {"$ref": ...} object in ``value``; a malformed one is refused."""
    if isinstance(value, dict):
        if REF_KEY in value:
            if len(value) != 1 or not isinstance(value[REF_KEY], str):
                raise Invalid(f"{path or 'args'}: a reference is exactly {{\"$ref\": \"call[i].field\"}}",
                              details={"path": path})
            yield path, value[REF_KEY]
            return
        for k, v in value.items():
            yield from iter_refs(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from iter_refs(v, f"{path}[{i}]")


def parse_ref(text: str) -> tuple[int, str] | None:
    """(call index, field or ``outputs.<slot>``) of a reference, or None if it does not match the grammar."""
    m = _REF.match(text)
    return (int(m.group(1)), m.group(2)) if m else None


def _validate_refs(calls: list[dict[str, Any]]) -> None:
    validate_refs(calls)


def validate_refs(calls: list[dict[str, Any]], *, result_fields: dict[str, tuple[str, ...]] | None = None,
                  job_tools: tuple[str, ...] = JOB_TOOLS) -> None:
    """ADR 0022: every reference must parse, and name an existing earlier call and a field that call produces. Sets each
    call's ``depends_on``. The MCP adapter (ADR 0025) passes its own vocabulary's result fields and job tools."""
    fields_of = RESULT_FIELDS if result_fields is None else result_fields
    for i, c in enumerate(calls):
        c.pop("depends_on", None)  # derived here; a proposal never states its own dependencies
        depends: set[int] = set()
        for path, text in iter_refs(c["args"]):
            parsed = parse_ref(text)
            if parsed is None:
                raise Invalid(f"call {i} ({c['tool']}): {path}: malformed reference {text!r}; expected "
                              "call[<index>].<field> or call[<index>].outputs.<slot>",
                              details={"call": i, "path": path, "ref": text})
            target, field = parsed
            if path.split(".")[0] in LITERAL_ARGS:
                raise Invalid(f"call {i} ({c['tool']}): {path} must be literal, not a reference (the lab checks it when "
                              "the decision is accepted)", details={"call": i, "path": path, "ref": text})
            if target >= len(calls):
                raise Invalid(f"call {i} ({c['tool']}): {path}: {text!r} names call {target}, but this decision has "
                              f"{len(calls)} calls", details={"call": i, "path": path, "ref": text})
            if target >= i:
                raise Invalid(f"call {i} ({c['tool']}): {path}: {text!r} is a forward or self reference; a call may "
                              "depend only on earlier calls, so a cycle cannot be written",
                              details={"call": i, "path": path, "ref": text})
            tool = calls[target]["tool"]
            if not (field in fields_of.get(tool, ()) or (field.startswith("outputs.") and tool in job_tools)):
                allowed = [*fields_of.get(tool, ()), *(["outputs.<slot>"] if tool in job_tools else [])]
                raise Invalid(f"call {i} ({c['tool']}): {path}: {text!r} names no result of {tool}; it produces {allowed}",
                              details={"call": i, "path": path, "ref": text, "allowed": allowed})
            depends.add(target)
        if depends:
            c["depends_on"] = sorted(depends)


def _resource_policy(policy: Any) -> dict[str, int]:
    """A decision's explicit resource policy (ADR 0022); absent means one job at a time."""
    if policy in (None, {}):
        return {}
    if not isinstance(policy, dict) or set(policy) != {MAX_CONCURRENT_JOBS_KEY}:
        raise Invalid("resource_policy accepts exactly {\"max_concurrent_jobs\": <integer >= 1>}")
    value = policy[MAX_CONCURRENT_JOBS_KEY]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise Invalid("resource_policy.max_concurrent_jobs must be an integer >= 1")
    return {MAX_CONCURRENT_JOBS_KEY: value}


def resolve_refs(value: Any, lookup: Any, path: str = "") -> tuple[Any, list[dict[str, Any]]]:
    """Replace every reference in ``value`` with ``lookup(index, field)``. Returns the value and its provenance
    (path, reference text, resolved value) for the call's receipt."""
    if isinstance(value, dict):
        if REF_KEY in value:
            parsed = parse_ref(value[REF_KEY])
            if parsed is None:
                raise Invalid(f"{path}: malformed reference {value[REF_KEY]!r}")
            got = lookup(*parsed)
            return got, [{"path": path, "ref": value[REF_KEY], "value": got}]
        out, used = {}, []
        for k, v in value.items():
            out[k], more = resolve_refs(v, lookup, f"{path}.{k}" if path else str(k))
            used += more
        return out, used
    if isinstance(value, list):
        items = [resolve_refs(v, lookup, f"{path}[{i}]") for i, v in enumerate(value)]
        return [i[0] for i in items], [u for i in items for u in i[1]]
    return value, []


def keyed_calls(project_id: str, turn_id: str, decision_id: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach stable command keys from the persisted decision's call order."""
    return [{**c, "index": i, "command_key": command_key(project_id, turn_id, decision_id, i)}
            for i, c in enumerate(calls)]
