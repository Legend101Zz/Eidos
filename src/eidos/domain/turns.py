"""Bounded investigator turns, accepted decisions and stable tool vocabulary.

Design §"Bounded investigator turns and recovery" / §"Stable intent before effects".
"""

from __future__ import annotations

from typing import Any

from eidos.domain.assessment import validate_claim_block
from eidos.domain.errors import Invalid, PayloadTooLarge
from eidos_wire import canonical_json, command_key

# Application payload envelope for anything crossing a workflow boundary. Temporal's hard blob
# limit is 2 MiB (warning at 512 KiB); we stay far below and stage larger content as artifacts.
WORKFLOW_PAYLOAD_LIMIT = 128 * 1024
MODEL_CONTEXT_CHAR_LIMIT = 60_000

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
                "deadline", "cancelled", "error", "stale-revision")


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
            "calls": calls, "control": control}


def keyed_calls(project_id: str, turn_id: str, decision_id: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach stable command keys from the persisted decision's call order."""
    return [{**c, "index": i, "command_key": command_key(project_id, turn_id, decision_id, i)}
            for i, c in enumerate(calls)]
