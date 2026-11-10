"""Versioned context policy ``context-policy/1``.

Packs an authorized investigation view (taken at the turn's ledger horizon) into a bounded
prompt and records exactly what was selected, omitted and truncated. The view itself is stored
as the source manifest so a different policy can be evaluated offline on the same past.
"""

from __future__ import annotations

import json
from typing import Any

from eidos.domain.turns import CONTROL_TOOLS, EFFECT_TOOLS, MODEL_CONTEXT_CHAR_LIMIT
from eidos_wire import canonical_json, sha256_bytes

POLICY_ID = "context-policy/1"
MAX_JOBS = 25
MAX_CATALOG = 30

TOOL_GUIDE = """You are the Eidos investigator. Each turn you propose ONE decision as JSON:
{"rationale": str, "purpose": one of reproduction|measurement-development|open-search|hypothesis-test|confirmation,
 "hypotheses": [{"statement","prediction","falsifier"}], "calls": [{"tool","args"}...], "control": {"action", "summary"}}
Effect tools (executed in order only after the lab accepts your decision):
  run_component {component_ref, inputs:{port: artifact_id}, config, environment?, wall_seconds?, prediction?, falsifier?}
  plan_trial {component_ref, inputs, config, environment?} -> trial_id; launch_trial {trial_id}
  plan_branch {parent_checkpoint_id, component_ref, migration, stream_policy, seed?, intervention, inputs, config}
    migration = {<every parent state entry, e.g. "model", "optimizer", "rng.torch", "data_cursor">: "copy"|"transform"|
    "init"|"drop"}; stream_policy "paired" or "independent" (independent needs seed); intervention = {"kind": "none"|
    "change-weights"|"change-learning"|"change-structure", "component_ref"?, "config"?}. A refusal lists the parent's
    state entries. -> trial_id; launch_branch {trial_id}. A returned trial_id is listed under "Results of your recent calls" in
    the NEXT turn; launch it there.
  author_source {files:{path: text}}  -> a source-bundle artifact id: candidate project files (e.g. a changed project.py)
    for run_component inputs.source of eidos.project.command (the whole-trainer path). No build, no new component.
    The id is listed under "Artifacts you created" in your NEXT turn's context; run it there (a call cannot use
    an id returned by an earlier call of the same decision).
  author_component {files:{path: text}, environment: "<registered environment name string, e.g. \"default\">"}
    -> builds a NEW component (needs an eidos-component.json manifest in files): a validation job; then
    publish_component {bundle_artifact_id, validation_job_id}. The manifest "entry" is "module:function", called as
    function(inputs, config, *, reader=None, writer=None, context=None) (keyword arguments injected by name):
    reader.json(port)/reader.rows(port)/reader.path(port) read inputs; writer.stage_json(slot, obj, schema=...) or
    writer.stage_file(slot, path, schema=...) write the outputs declared in the manifest; a returned dict goes to the
    job receipt. A training extension in eidos.pytorch.train config.extensions is {"name", "component_ref":
    "<published training-extension release>"} (pinned and mounted by the lab) or {"name", "spec": "module:attr"}
    (importable code); a release ref never goes in "spec".
  bind_component {component_ref, capability?}; retire_component {component_ref}
  file_claim {statement, kind, scope}; draft_evidence_plan {claim_id, charter_id, template_ref, draft}
    draft = {"success_rule_id": <charter rule id>, "assessor": {"ref": <approved assessor ref>},
             "analysis_unit": "pair", "cluster_by": "seed", "seed_policy": {"seeds": [..]},
             "required_isolation": {"profile": "dev:same-user", "controls": [], "waiver": "<reason>"} or {"sealed": true},
             "arms": [{"id", "role": "baseline"|"candidate", "seed"?, "run": {component_ref, inputs, config, environment}}
                      or {"id", "role", "runs": [{"seed", "cluster", "jobs": [{"id", "component", "inputs", "config",
                      "environment"}]}]}]}  (a pipeline job input may name an earlier job's output: "<job id>.<slot>").
    No thresholds in a draft: the rule comes from the charter. Missing fields are listed as obligations, and a
    plan with obligations cannot be frozen. template_ref and the assessor must be in "Approved protocols".
    Ordinary (multiclass) accuracy from two training jobs' held-out predictions (charter metric identity
    "classification-report/1:accuracy"): run eidos.templates.improves-under-budget@1.2.0 with request {claim,
    success_rule_id, assessor: {"ref": "eidos.assessors.paired-ci@1.1.0"}, required_isolation, classification:
    {labels: <labels artifact id>, predictions: {baseline: {"<seed>": <artifact id>}, candidate: {"<seed>":
    <artifact id>}}}} and pass its draft output as draft_artifact_id; each arm runs
    eidos.task.paired-classification-report (ids must match exactly).
  freeze_evidence_plan {plan_id}; launch_evidence_plan {plan_id} (the assessment then runs by itself; its outcome
    appears under "Your evidence plans")
  export_model {checkpoint_artifact_id, behavior_check_job_id}; record_note {kind, text, evidence_refs}
Control actions: wait_for_jobs (sleep until admitted work finishes), request_input, conclude {summary, scope}, stop.
Success rules come only from the committed charter; you cannot set thresholds. Cite artifact/job ids for numbers."""


def pack(view: dict[str, Any], catalog: list[dict[str, Any]], *, char_limit: int = MODEL_CONTEXT_CHAR_LIMIT) -> dict[str, Any]:
    inv = view["investigation"]["body"]
    sel: dict[str, list] = {"jobs": [], "claims": [], "catalog": []}
    omitted: dict[str, list] = {"jobs": [], "catalog": []}
    lines = [f"# Investigation {view['investigation']['id']} (status {view['investigation']['status']}, horizon {view['horizon']})",
             f"Question: {inv.get('question')}"]
    qh = inv.get("question_history") or []
    if len(qh) > 1:  # the human revised the question: show it, with what changed and the policy for admitted work
        last = qh[-1]
        lines.append(f"(Question revision {last['question_revision']} by {last['by']}; previous question: "
                     f"{str(last.get('previous'))[:500]!r}; work admitted earlier: {last.get('running_job_policy')})")
    if view.get("environments"):
        lines.append(f"Registered environments (names for `environment`): {', '.join(view['environments'])}")
    if inv.get("model_artifact_id"):
        lines.append(f"Model artifact: {inv['model_artifact_id']}  environment: {inv.get('environment')}")
    ch = view.get("charter")
    if ch:
        lines.append("## Charter (fixed success rules; read-only)")
        lines.append(json.dumps({k: ch.get(k) for k in ("study_id", "exploration_only", "population", "comparator",
                                                         "metrics", "success_rules", "resource_limits")}, sort_keys=True)[:4000])
    else:
        lines.append("## Charter: NONE committed — exploratory runs will be refused until a researcher commits one.")
    steer = inv.get("steering") or []
    if steer:
        lines.append("## Operator steering (most recent last)")
        lines += [f"- [{s['by']}] {s['instruction']}" for s in steer[-5:]]
    if inv.get("inputs"):
        lines.append("Declared input artifacts (port -> artifact id): " + json.dumps(inv["inputs"], sort_keys=True))
    if inv.get("task"):
        lines.append("Task: " + json.dumps(inv["task"], sort_keys=True)[:2000])
    if inv.get("notes"):
        lines.append(f"Notes: {inv['notes']}")
    lines.append("## Budget (exploration scope)")
    lines.append(json.dumps(view.get("budgets"), sort_keys=True))
    jobs = view.get("jobs") or []
    recent = jobs[-MAX_JOBS:]
    omitted["jobs"] = [j["id"] for j in jobs[:-MAX_JOBS]] if len(jobs) > MAX_JOBS else []
    lines.append(f"## Jobs ({len(jobs)} total; showing {len(recent)} most recent)")
    for j in recent:
        sel["jobs"].append(j["id"])
        rec = j.get("receipt") or {}
        metrics = rec.get("metrics") if isinstance(rec, dict) else None
        lines.append(json.dumps({"id": j["id"], "component": j["component"], "status": j["status"], "purpose": j["purpose"],
                                 "inputs": j.get("inputs"), "config": _clip(j.get("config")), "metrics": metrics,
                                 "error": _error_view(j.get("error")),
                                 "outputs": [{"slot": o["slot"], "artifact_id": o["artifact_id"], "role": o["role"],
                                              "schema": o["schema"]} for o in j.get("outputs", [])]}, sort_keys=True))
    created = view.get("created_artifacts") or []
    if created:
        lines.append(f"## Artifacts you created ({len(created)}; unused ones have used_by [])")
        for c in created[-20:]:
            lines.append(json.dumps(c, sort_keys=True))
    results = view.get("call_results") or []
    if results:
        lines.append(f"## Results of your recent calls ({len(results)}; ids here are what later calls take)")
        for r in results[-20:]:
            lines.append(json.dumps(r, sort_keys=True, default=str))
    plans = view.get("evidence_plans") or []
    if plans:
        lines.append("## Your evidence plans")
        for pl in plans:
            lines.append(json.dumps(pl, sort_keys=True, default=str))
    approved = view.get("approved_protocols")
    if approved is not None:
        lines.append("## Approved (qualified) protocols: templates and assessors usable in a plan, by claim kind")
        lines.append(json.dumps(approved, sort_keys=True) if approved else "none: no plan can be frozen yet")
    claims = view.get("claims") or []
    if claims:
        lines.append("## Claims")
        for c in claims:
            sel["claims"].append(c["id"])
            lines.append(json.dumps(c, sort_keys=True, default=str))
    lines.append(f"## Component catalog ({len(catalog)} active; showing {min(len(catalog), MAX_CATALOG)})")
    for c in catalog[:MAX_CATALOG]:
        sel["catalog"].append(c["ref"])
        lines.append(json.dumps({k: c.get(k) for k in ("ref", "implements", "kind", "effects", "input", "optional_input",
                                                        "output", "summary", "config_schema") if c.get(k) is not None},
                                sort_keys=True))
    omitted["catalog"] = [c["ref"] for c in catalog[MAX_CATALOG:]]
    if len(catalog) > MAX_CATALOG:
        lines.append("(more components exist: use search via the catalog query; they are data, not tools)")
    lines.append("## Tools\n" + TOOL_GUIDE)
    text = "\n".join(lines)
    truncated = False
    if len(text) > char_limit:
        text = text[: char_limit - 200] + "\n...[context truncated by context-policy/1]\n" + TOOL_GUIDE
        truncated = True
    return {"policy": POLICY_ID, "text": text, "selections": sel, "omissions": omitted,
            "truncations": {"context_truncated": truncated}, "chars": len(text),
            "source_manifest_digest": "sha256:" + sha256_bytes(canonical_json(view).encode()),
            "known_tools": sorted(EFFECT_TOOLS) + list(CONTROL_TOOLS)}


def _error_view(err: Any) -> Any:
    """The failure reason first (category and message, bounded), not a clipped dump with the traceback."""
    if not isinstance(err, dict):
        return _clip(err)
    return {"category": err.get("category"), "message": str(err.get("message", ""))[:1200]}


def _clip(x: Any, n: int = 600) -> Any:
    if x is None:
        return None
    s = json.dumps(x, sort_keys=True, default=str)
    return x if len(s) <= n else s[:n] + "…"
