"""Model-free, single-pass investigator over the authorized ledger view (fixture evidence only)."""
from __future__ import annotations

import json
import math
from typing import Any

from eidos.domain.assessment import candidate_lock_job_id
from eidos.domain.exploration import policy
from eidos.domain.promotion import PORTFOLIO_REF, suggested_lock
from eidos.investigator.runtime import DecisionRequest, fixture_policy

COMMAND = "eidos.project.command@1.0.9"


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _conclude(reason: str, candidate: dict[str, Any] | None = None, *, abstain: bool = False) -> dict[str, Any]:
    positive = candidate is not None
    claim: dict[str, Any] = {"disposition": "candidate" if positive else "abstain" if abstain else "null"}
    measurements = []
    if candidate:
        selection, paired = candidate["lock"]["selection"], candidate["paired"]
        reason += (f' Validation {selection["metric"]}={candidate["validation"]["value"]}, '
                   f'SE={candidate["validation"]["se"]}; paired gain={paired["paired_delta"]}, '
                   f'interval=[{paired["paired_delta_lo"]}, {paired["paired_delta_hi"]}]; '
                   f'job={candidate["lock"]["job_id"]}.')
        value = candidate["validation"]["value"]
        baseline = value - paired["paired_delta"] if selection["direction"] == "higher_is_better" else value + paired["paired_delta"]
        measurements = [dict(metric=selection["metric"], direction=selection["direction"], split="validation",
                             denominator=int(paired["paired_n"]), units="fraction" if selection["metric"].endswith("accuracy") else "RMSE",
                             baseline=baseline, candidate=value, evidence_receipt=candidate["lock"]["job_id"],
                             uncertainty_method=f'Paired bootstrap interval [{paired["paired_delta_lo"]}, {paired["paired_delta_hi"]}]; validation SE {candidate["validation"]["se"]}.')]
        claim.update(point_estimate=dict(metric=selection["metric"], value=paired["paired_delta"],
                                         split="validation", direction=selection["direction"]),
                     evidence_refs=[candidate["lock"]["job_id"]])
    reason += " Fixture policy; validation selection evidence only; independent evaluation remains outstanding."
    answer = dict(status="improvement_claimed" if positive else "abstained" if abstain else "no_improvement_found",
                  claim_type="recipe_improvement" if positive else "no_positive_claim", claim=reason,
                  scope="Visible validation data; pending independent evaluation.",
                  measurements=measurements, alternative_explanations=["Selection used the same visible data."],
                  abstention=dict(abstained=abstain, reason=reason if abstain else None,
                                  unresolved=["Missing eligible evidence."] if abstain else [],
                                  next_discriminating_test="Run the missing mechanical checks." if abstain else None))
    return dict(rationale=reason, purpose="reproduction", calls=[],
                control=dict(action="conclude", summary=reason, claim=claim, scope=dict(answer=answer)))


def _supported(candidate: dict[str, Any], task: dict[str, Any]) -> bool:
    paired = candidate.get("paired") or {}
    values: list[Any] = [paired.get(k) for k in ("paired_delta", "paired_delta_lo", "paired_delta_hi")]
    # The lab expresses paired deltas as gain for both accuracy and RMSE.
    if not all(_number(v) for v in values) or not 0 < values[1] <= values[0] <= values[2]:
        return False
    if (candidate.get("readiness") or {}).get("readiness_ok") != 1:
        return False
    if not (candidate.get("reproduction") or {}).get("reproduced_by"):
        return False
    # A structured bar is optional in today's authorized task. Never infer one from question prose.
    bar = task.get("practical_bar")
    return not (bar is not None and (not _number(bar) or bar < 0 or values[0] < bar))


@fixture_policy("autopilot-v2")
def autopilot(req: DecisionRequest) -> dict[str, Any]:
    view = req.view or {}
    body = (view.get("investigation") or {}).get("body") or {}
    config = body.get("investigator") or {}
    if (config.get("exploration") or {}).get("policy") != "exploration-policy/3" or config.get("context_policy") != "context-policy/4":
        return _conclude("autopilot-v2 requires exploration-policy/3 and context-policy/4.", abstain=True)
    if req.conclude_only:
        return _conclude("Conclude-only request: no further mechanical steps are allowed.", abstain=True)
    feedback = req.turn_so_far
    if view.get("admission_refusals") or any(d.get("rejected") or d.get("error") or
            any(not r.get("ok") for r in d.get("receipts", [])) for d in feedback):
        return _conclude("A mechanical step was refused; no retry or improvement claim.", abstain=True)
    jobs = view.get("jobs") or []
    if any(j.get("status") in ("failed", "cancelled", "pruned", "outcome_unknown") for j in jobs):
        return _conclude("A mechanical job did not succeed; no improvement claim.", abstain=True)
    if any(j.get("status") != "succeeded" for j in jobs):
        previous_wait = any(not d.get("calls") and (d.get("control") or {}).get("action") == "wait_for_jobs" for d in feedback)
        previous_wait |= any(((t.get("result") or {}).get("control") or {}).get("summary") == "autopilot-v2: wait once"
                             for t in view.get("turns", []))
        if previous_wait:
            return _conclude("Jobs remain unfinished after the bounded wait.", abstain=True)
        return dict(rationale="Wait for admitted jobs.", calls=[],
                    control=dict(action="wait_for_jobs", summary="autopilot-v2: wait once"))
    task = body.get("task") or {}
    kind = task.get("task_type")
    inputs = {k: v for k, v in (body.get("inputs") or {}).items() if k in ("train", "validation", "task")}
    if kind not in ("classification", "regression") or not all(inputs.get(k) for k in ("train", "validation")):
        return _conclude("Missing structured task type or train/validation inputs.", abstain=True)
    prepared = suggested_lock({**view, "policy": policy(config)})
    state = view.get("exploration") or {}
    incumbent = state.get("incumbent") or {}
    locks = [incumbent["job_id"]] if incumbent.get("job_id") else []
    if "incumbent" not in state:  # legacy/offline fixture views may only carry note JSON
        locks = [candidate_lock_job_id(n) for n in view.get("pinned_notes", []) if n.get("kind") == "candidate_lock"]
    # The view horizon is fixed for a turn. A successful note receipt from this
    # turn is also ledger evidence, even before the next turn can see the note.
    for decision in feedback:
        for index, call in enumerate(decision.get("calls", [])):
            args = call.get("args") or {}
            if (prepared and call.get("tool") == "record_note" and args.get("kind") == "candidate_lock"
                    and args.get("text") == json.dumps(prepared["lock"], sort_keys=True)
                    and any(receipt.get("ok") and receipt.get("index") == index for receipt in decision.get("receipts", []))):
                locks.append(prepared["lock"]["job_id"])
    if locks:
        if prepared and prepared["lock"]["job_id"] in locks and _supported(prepared, task) and req.finalization_has_eligible_lock is not False:
            return _conclude("The recorded lock has readiness, second-seed reproduction and a positive paired interval.", prepared)
        return _conclude("The recorded lock lacks eligible supporting evidence.", abstain=True)
    if prepared:
        if not _supported(prepared, task):
            return _conclude("Prepared evidence does not clear the paired-interval, reproduction or practical-gain rule.")
        # This is the exact prepared lock text emitted by context-policy/4. Its selection fixes the rule too.
        return dict(rationale="Choose the lab-prepared candidate on its recorded validation evidence.", purpose="reproduction",
                    calls=[dict(tool="record_note", args=dict(kind="candidate_lock",
                                text=json.dumps(prepared["lock"], sort_keys=True),
                                evidence_refs=[prepared["lock"]["job_id"]]))], control={})
    if any(j.get("macro") or str(j.get("component", "")).startswith("eidos.search.recipe-portfolio@") for j in jobs):
        return _conclude("Preparation finished without a complete candidate fact.", abstain=True)
    baseline = view.get("baseline_record") or {}
    ref = (config.get("exploration") or {}).get("project_command_ref", COMMAND)
    if ref != COMMAND:
        return _conclude("The required pinned project-command release is unavailable.", abstain=True)
    if not baseline:
        if jobs or feedback:
            return _conclude("Baseline validation evidence is missing after the baseline step.", abstain=True)
        return dict(rationale="Reproduce the supplied baseline without authoring.", purpose="reproduction",
                    calls=[dict(tool="run_component", args=dict(component_ref=ref, environment=body.get("environment", "default"),
                        purpose="reproduction", inputs=inputs,
                        config=dict(argv=["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", "{seed}"],
                                    files={k: k + (".json" if k == "task" else ".npz") for k in inputs}, seed=11)))],
                    control=dict(action="wait_for_jobs"))
    base_job: dict[str, Any] = next((j for j in jobs if j.get("id") == baseline.get("job_id")), {})
    if not any(o.get("slot") == "validation_predictions" for o in base_job.get("outputs", [])):
        return _conclude("The baseline has no validation predictions.", abstain=True)
    return dict(rationale="Screen the default portfolio and prepare its promotion source once.", purpose="open-search",
                calls=[dict(tool="run_component", args=dict(component_ref=PORTFOLIO_REF, environment=body.get("environment", "default"),
                            purpose="open-search", inputs=inputs, config=dict(mode="portfolio", task_type=kind, include="auto", scheme="auto"))),
                       dict(tool="prepare_candidate", args=dict(source={"$ref": "call[0].outputs.promotion_source"}, seeds=[11, 23]))],
                control=dict(action="wait_for_jobs"))
