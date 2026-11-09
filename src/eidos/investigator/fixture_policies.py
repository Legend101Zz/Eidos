"""Deterministic FIXTURE investigator policies (development and journey tests only).

Nothing here is, or says anything about, an LLM. A fixture policy is a pure function of the
``DecisionRequest`` it receives: the authorized investigation view at the turn's ledger horizon
(``request.view``) and the calls already decided in this turn (``request.turn_so_far``). It keeps no
state of its own, so a turn that is recovered, replayed or re-decided after a crash reaches the same
decision from the same ledger evidence.

``switch-order-scripted`` walks the design's worked path for the switch-order model:

1. no baseline evaluation yet -> train OrderedSwitchNet, then evaluate the checkpoint;
2. both-correct is low while reference accuracy is high -> score the pairs by stratum;
3. author a small diagnostic component (``author_component`` with files), validate, publish and run it;
4. repair with a weight edit, evaluate, behaviour-check the edited checkpoint in a fresh process;
5. file a claim citing job ids, request an export, then conclude.

Every turn launches at most one dependent stage and then waits for the jobs it started: results appear in
the view of a later turn, never in the turn that launched them.
"""

from __future__ import annotations

from typing import Any

from eidos.investigator.runtime import DecisionRequest, fixture_policy

POLICY = "switch-order-scripted"
ADAPTER = "eidos_adapter:ADAPTER"
LOW_BOTH_CORRECT, HIGH_REFERENCE = 0.5, 0.9
WALL = 120

DIAGNOSTIC_MANIFEST = """{
  "id": "fixture.stratum-gap", "release": "1.0.0", "implements": "stratum-gap/1", "kind": "artifact-op",
  "entry": "stratum_gap:run", "scientific_effects": ["observe"],
  "input": {"report": "grouped-pair-counts/1"}, "output": {"gap": "stratum-gap-report/1"},
  "requires": ["artifact.reader/1", "artifact.writer/1"], "reads": ["report"], "writes": ["output:gap"],
  "isolation": "immutable-artifacts", "receipt": ["worst_group", "gap", "metrics"],
  "summary": "authored by the fixture policy: largest both-correct gap between pair strata"
}
"""
DIAGNOSTIC_CODE = '''def run(inputs, config, *, reader, writer, context):
    report = reader.json(inputs["report"])
    groups = report["by_group"]
    rates = {name: g["both_correct_rate"] for name, g in groups.items()}
    worst = min(rates, key=lambda k: rates[k])
    best = max(rates, key=lambda k: rates[k])
    out = {"rates": rates, "worst_group": worst, "best_group": best, "gap": rates[best] - rates[worst]}
    writer.stage_json("gap", out, schema="stratum-gap-report/1")
    return {"worst_group": worst, "gap": out["gap"], "metrics": {"gap": out["gap"]}}
'''


def _catalog_ref(view: dict[str, Any], prefix: str) -> str | None:
    for c in view.get("catalog") or []:
        if str(c.get("ref", "")).startswith(prefix):
            return c["ref"]
    return None


def _jobs(view: dict[str, Any], prefix: str, *, status: str | None = "succeeded") -> list[dict[str, Any]]:
    return [j for j in view.get("jobs") or [] if str(j.get("component") or "").startswith(prefix)
            and (status is None or j.get("status") == status)]


def _out(job: dict[str, Any], slot: str) -> str | None:
    return next((o["artifact_id"] for o in job.get("outputs") or [] if o["slot"] == slot), None)


def _by_input(jobs: list[dict[str, Any]], port: str, artifact: str | None) -> dict[str, Any] | None:
    return next((j for j in jobs if artifact and (j.get("inputs") or {}).get(port) == artifact), None)


def _wait(rationale: str, calls: list[dict[str, Any]], purpose: str = "hypothesis-test") -> dict[str, Any]:
    return {"rationale": rationale, "purpose": purpose, "calls": calls, "control": {"action": "wait_for_jobs"}}


def _run(ref: str, inputs: dict[str, str], config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"tool": "run_component", "args": {"component_ref": ref, "inputs": inputs, "config": config,
                                              "wall_seconds": WALL, **extra}}


def _note(text: str) -> dict[str, Any]:
    return {"tool": "record_note", "args": {"kind": "observation", "text": "[fixture policy] " + text}}


@fixture_policy(POLICY)
def switch_order_scripted(req: DecisionRequest) -> dict[str, Any]:
    view = req.view or {}
    if req.turn_so_far:  # one stage per turn; the loop wakes us when the launched jobs finish
        return _wait("stage launched; wait for its jobs", [])
    jobs = view.get("jobs") or []
    if any(j.get("status") not in ("succeeded", "failed", "cancelled") for j in jobs):
        return _wait("jobs from an earlier turn are still running", [])
    failed = [j["id"] for j in jobs if j.get("status") in ("failed", "cancelled")]
    if failed:
        return {"rationale": f"job(s) {failed} failed; stopping", "calls": [],
                "control": {"action": "stop", "summary": f"fixture policy stopped after failed jobs {failed}"}}
    if not view.get("charter"):
        return {"rationale": "no committed charter in the view", "calls": [],
                "control": {"action": "request_input", "summary": "commit a study charter first"}}
    train, evaluate, edit = (_catalog_ref(view, p) for p in ("eidos.pytorch.train@", "eidos.pytorch.evaluate@",
                                                              "eidos.pytorch.weight-edit@"))
    scorer, behave = _catalog_ref(view, "lab.pairs.by_group@"), _catalog_ref(view, "eidos.pytorch.behavior-check@")
    if not all((train, evaluate, edit, scorer, behave)):
        return {"rationale": "required components missing from the catalog", "calls": [],
                "control": {"action": "request_input", "summary": "install train/evaluate/edit/score/behaviour components"}}

    # 1. baseline model and evaluation ---------------------------------------------------------------
    trains = _jobs(view, "eidos.pytorch.train@")
    if not trains:
        return _wait("no baseline exists: train OrderedSwitchNet seed 41 for 45 steps", [_run(
            train, {}, {"adapter": ADAPTER, "builder": "OrderedSwitchNet", "steps": 45, "seed": 41}),
            _note("baseline training admitted"), _note("evaluation follows once the checkpoint exists"),
            _note("recorded before the turn closes")])
    ckpt = _out(trains[0], "checkpoint")
    evals = _jobs(view, "eidos.pytorch.evaluate@")
    base_eval = _by_input(evals, "checkpoint", ckpt)
    if not base_eval:
        return _wait("evaluate the baseline checkpoint on the test split", [_run(
            evaluate, {"checkpoint": ckpt}, {"adapter": ADAPTER, "evaluation": {
                "split": "test", "data_seed": 76102, "pairs": 4000}})])
    # 2. diagnosis: pair scorer by stratum -------------------------------------------------------------
    scores = _jobs(view, "lab.pairs.by_group@")
    base_score = _by_input(scores, "predictions", _out(base_eval, "predictions"))
    if not base_score:
        return _wait("score the baseline pairs by stratum", [_run(
            scorer, {"predictions": _out(base_eval, "predictions")}, {"group_by": "stratum"})])
    m = (base_score.get("receipt") or {}).get("metrics") or {}
    bc, ra = m.get("both_correct_rate"), m.get("reference_accuracy")
    if bc is None or ra is None:
        return {"rationale": "scorer receipt has no metrics", "calls": [],
                "control": {"action": "stop", "summary": "no usable scorer receipt"}}
    if not (bc < LOW_BOTH_CORRECT and ra > HIGH_REFERENCE):
        return {"rationale": f"both-correct {bc} / reference {ra}: no discrepancy to explain", "calls": [],
                "control": {"action": "conclude", "summary": f"baseline both-correct {bc}, reference {ra}; nothing to repair",
                            "scope": {"model": "OrderedSwitchNet seed 41 step 45"}}}
    # 3. author a diagnostic component, publish it, run it on the scorer's report ---------------------------
    validations = [j for j in _jobs(view, "builtin:validator", status=None) if j.get("purpose") == "build"]
    gap_ref = next((c["ref"] for c in view.get("catalog") or [] if str(c.get("ref", "")).startswith("fixture.stratum-gap@")), None)
    if not validations and not gap_ref:
        return _wait("both-correct is low while reference accuracy is high: author a stratum-gap diagnostic", [
            _note(f"both_correct_rate={bc} reference_accuracy={ra} (job {base_score['id']})"),
            {"tool": "author_component", "args": {"environment": "default", "files": {
                "eidos-component.json": DIAGNOSTIC_MANIFEST, "stratum_gap.py": DIAGNOSTIC_CODE}}}], "measurement-development")
    if validations and not gap_ref:
        v = validations[-1]
        return _wait("publish the validated diagnostic", [{"tool": "publish_component", "args": {
            "bundle_artifact_id": (v.get("inputs") or {}).get("bundle"), "validation_job_id": v["id"]}},
            {"tool": "bind_component", "args": {"component_ref": "fixture.stratum-gap@1.0.0", "mode": "add"}}],
            "measurement-development")
    gaps = _jobs(view, "fixture.stratum-gap@")
    if not gaps:
        return _wait("run the diagnostic on the baseline report", [_run(
            gap_ref, {"report": _out(base_score, "report")}, {}, purpose="measurement-development")],
            "measurement-development")
    # 4. repair: weight edit, evaluation, fresh-process behaviour check ---------------------------------------
    edits = _jobs(view, "eidos.pytorch.weight-edit@")
    if not edits:
        return _wait("the exception stratum is worst: silence the recorder readout weight", [_run(
            edit, {"checkpoint": ckpt}, {"edits": [{"entry": "readout.weight", "index": [0, -1], "op": "set", "value": 0.0}],
                                         "reason": "fixture policy repair"})])
    edited = _out(edits[0], "checkpoint")
    edited_eval = _by_input(evals, "checkpoint", edited)
    if not edited_eval:
        return _wait("evaluate the edited checkpoint", [_run(evaluate, {"checkpoint": edited}, {
            "adapter": ADAPTER, "evaluation": {"split": "test", "data_seed": 76102, "pairs": 4000}})])
    checks = _jobs(view, "eidos.pytorch.behavior-check@")
    check = _by_input(checks, "checkpoint", edited)
    if not check:
        return _wait("behaviour-check the edited checkpoint in a fresh process", [_run(
            behave, {"checkpoint": edited}, {"adapter": ADAPTER, "builder": "OrderedSwitchNet"})])
    if (check.get("receipt") or {}).get("behavior_match") is not True:
        return {"rationale": "behaviour check did not match; no export", "calls": [],
                "control": {"action": "stop", "summary": "edited checkpoint failed its behaviour check"}}
    # 5. claim, export, conclude -----------------------------------------------------------------------------
    if not view.get("claims"):
        return _wait("file a claim citing the evidence and request the export", [
            {"tool": "file_claim", "args": {
                "statement": "Silencing the recorder readout weight changes the paired both-correct rate of OrderedSwitchNet "
                             f"seed 41 at step 45 (unedited job {base_eval['id']}, edited job {edited_eval['id']})",
                "kind": "intervention-effect", "scope": {"builder": "OrderedSwitchNet", "seed": 41, "step": 45},
                "supporting": [base_eval["id"], edited_eval["id"], base_score["id"]]}},
            {"tool": "export_model", "args": {"checkpoint_artifact_id": edited, "behavior_check_job_id": check["id"]}}],
            "confirmation") | {
            # Nothing is launched here, so waiting would park the loop until the max-idle timer (claims and
            # exports are not news); conclude in the same decision once the claim and export are recorded.
            "control": {"action": "conclude", "summary": "Edited checkpoint exported after a passing behaviour check "
                        "(fixture policy; no real model reasoned about this)",
                        "scope": {"model": "OrderedSwitchNet seed 41 step 45"}}}
    return {"rationale": "evidence gathered, claim filed, export requested", "calls": [],
            "control": {"action": "conclude", "summary": "Edited checkpoint exported after a passing behaviour check "
                        "(fixture policy; no real model reasoned about this)",
                        "scope": {"model": "OrderedSwitchNet seed 41 step 45"}}}
