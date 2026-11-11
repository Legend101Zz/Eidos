"""Versioned context policies ``context-policy/2`` (current) and ``context-policy/1`` (replay and older turns).

Packs an authorized investigation view (taken at the turn's ledger horizon) into a bounded
prompt and records exactly what was selected, omitted and truncated. The view itself is stored
as the source manifest so a different policy can be evaluated offline on the same past.
"""

from __future__ import annotations

import json
import re
from typing import Any

from eidos.domain.redaction import redact_text as _redact
from eidos.domain.turns import CONTROL_TOOLS, EFFECT_TOOLS, MODEL_CONTEXT_CHAR_LIMIT
from eidos_wire import canonical_json, sha256_bytes

POLICY_ID = "context-policy/2"  # ADR 0020
POLICY_V1 = "context-policy/1"
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
    success_rule_id, assessor: {"ref": "eidos.assessors.paired-ci@1.2.0"}, required_isolation, classification:
    {labels: <labels artifact id>, predictions: {baseline: {"<seed>": <artifact id>}, candidate: {"<seed>":
    <artifact id>}}}} and pass its draft output as draft_artifact_id; each arm runs
    eidos.task.paired-classification-report (ids must match exactly).
  freeze_evidence_plan {plan_id}; launch_evidence_plan {plan_id} (the assessment then runs by itself; its outcome
    appears under "Your evidence plans")
  export_model {checkpoint_artifact_id, behavior_check_job_id}; record_note {kind, text, evidence_refs}
Control actions: wait_for_jobs (sleep until admitted work finishes), request_input, conclude {summary, scope}, stop.
Success rules come only from the committed charter; you cannot set thresholds. Cite artifact/job ids for numbers."""


def pack(view: dict[str, Any], catalog: list[dict[str, Any]], *, char_limit: int = MODEL_CONTEXT_CHAR_LIMIT,
         policy: str = POLICY_ID) -> dict[str, Any]:
    if policy == POLICY_V1:
        return _pack_v1(view, catalog, char_limit=char_limit)
    return _pack_v2(view, catalog, char_limit=char_limit)


def _pack_v1(view: dict[str, Any], catalog: list[dict[str, Any]], *, char_limit: int) -> dict[str, Any]:
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
                                 "error": _error_view_v1(j.get("error")),
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
    return {"policy": POLICY_V1, "text": text, "selections": sel, "omissions": omitted,
            "truncations": {"context_truncated": truncated}, "chars": len(text),
            "source_manifest_digest": "sha256:" + sha256_bytes(canonical_json(view).encode()),
            "known_tools": sorted(EFFECT_TOOLS) + list(CONTROL_TOOLS)}


def _error_view_v1(err: Any) -> Any:
    """The failure reason first (category and message, bounded), not a clipped dump with the traceback."""
    if not isinstance(err, dict):
        return _clip(err)
    return {"category": err.get("category"), "message": str(err.get("message", ""))[:1200]}


def _clip(x: Any, n: int = 600) -> Any:
    if x is None:
        return None
    s = json.dumps(x, sort_keys=True, default=str)
    return x if len(s) <= n else s[:n] + "…"


# -- context-policy/2 (ADR 0020) --------------------------------------------------------------------------------------
#
# Pinned facts (the question, charter, steering, baseline, lock, success rule, environments, known failures, wall time)
# and the tool guide are always kept, each pinned section within its own budget. The rest is filled in order of value
# until the budget is spent, so the catalog is what goes first. Everything left out is listed in ``omissions``, and the
# final text never exceeds the limit (ADR 0020 addendum R10).
FAILURE_GROUPS = 12
STDERR_TAIL_CHARS = 800
QUESTION_CHARS = 40_000
MAX_STEERING = 5
_EXC = re.compile(r"\b([A-Za-z_][\w.]*(?:Error|Exception))\b:")
_MODULE = re.compile(r"No module named ['\"]?([\w.]+)")
# (characters a section may use, share of the limit it may use, characters of one line of it). The shares only matter for a
# limit well below the default, where the fixed amounts would not fit.
PINNED_BUDGETS: dict[str, tuple[int, float, int]] = {
    "revision": (900, 0.02, 900), "model": (400, 0.01, 400), "charter": (3000, 0.06, 3000), "steering": (4000, 0.07, 1200),
    "inputs": (3000, 0.05, 3000), "task": (2000, 0.04, 2000), "notes": (3000, 0.05, 3000), "budget": (1500, 0.03, 1500),
    "wall": (500, 0.06, 500), "baseline": (1200, 0.03, 1200), "lock": (1500, 0.03, 800), "success_rules": (1500, 0.03, 1500),
    "environments": (6000, 0.10, 900), "failures": (6000, 0.10, 400)}
HEAD_SECTIONS = ("revision", "model", "charter", "steering", "inputs", "task", "notes", "budget")
FACT_SECTIONS = ("wall", "baseline", "lock", "success_rules", "environments", "failures")
# Dropped first when the sections together do not fit a small limit; the facts are not on this list.
DROP_ORDER = ("notes", "task", "inputs", "budget", "revision", "model")
MAX_REFUSED_PACKAGES = 10
TRUNCATION_MARK = "\n...[context truncated by context-policy/2]\n"
def _exception_of(err: dict[str, Any]) -> str:
    """The failing exception's name: the innermost one a traceback or message reports (a project subprocess's error
    is wrapped by the component), else the recorded exception type."""
    text = "\n".join(str(err.get(k) or "") for k in ("traceback_tail", "stderr_tail", "message"))
    hits = _EXC.findall(text)
    return (hits[-1] if hits else str(err.get("type") or "")).rsplit(".", 1)[-1]


def _error_view(err: Any) -> Any:
    """The failure's category, exception, message and a bounded stderr tail, redacted."""
    if not isinstance(err, dict):
        return _clip(err)
    tail = str(err.get("stderr_tail") or err.get("traceback_tail") or "")
    return {"category": err.get("category"), "exception": _exception_of(err) or None,
            "message": _redact(str(err.get("message", "")))[:1200],  # redacted before the cut: a cut secret would not match
            **({"stderr_tail": _redact(tail)[-STDERR_TAIL_CHARS:]} if tail else {})}


def _failure_key(job: dict[str, Any]) -> tuple[str, ...]:
    err: dict[str, Any] = job["error"] if isinstance(job.get("error"), dict) else {}
    exception = _exception_of(err) or str(err.get("category") or "failure")
    text = "\n".join(str(err.get(k) or "") for k in ("message", "stderr_tail", "traceback_tail"))
    module = _MODULE.search(text)
    message = str(err.get("message") or "").strip()
    detail = module.group(1) if module else (_redact(message.splitlines()[0])[:120] if message else "")
    return (str(job.get("component")), str(job.get("environment") or "unknown"), exception, detail)


def _known_failures(view: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Lines for each distinct failure across the whole run (not only the job window), the most repeated first."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for j in view.get("jobs") or []:
        if j.get("status") == "failed":
            groups.setdefault(_failure_key(j), []).append(j)
    lines, omitted = [], []
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for (component, env, exception, detail), js in ranked[:FAILURE_GROUPS]:
        lines.append(f"- {component} failed {len(js)}\u00d7 in environment {env} with {exception} {detail} (last: {js[-1]['id']})".rstrip())
    omitted = [f"{c}|{e}|{x}" for (c, e, x, _d), _j in ranked[FAILURE_GROUPS:]]
    refused: dict[tuple[str, str], int] = {}
    packages: dict[tuple[str, str], set[str]] = {}
    for r in view.get("admission_refusals") or []:
        key = (str(r.get("component")), str(r.get("environment")))
        refused[key] = refused.get(key, 0) + 1
        packages.setdefault(key, set()).update(str(p.get("package")) for p in r.get("problems") or [])
    ranked_refused = sorted(refused.items(), key=lambda kv: -kv[1])
    for (c, e), n in ranked_refused[:FAILURE_GROUPS]:
        names = sorted(packages[(c, e)])
        more = len(names) - MAX_REFUSED_PACKAGES
        lines.append(f"- {c} refused {n}\u00d7 before launch in environment {e}: missing or incompatible "
                     f"{', '.join(names[:MAX_REFUSED_PACKAGES])}" + (f" and {more} more" if more > 0 else ""))
    omitted += [f"refused|{c}|{e}" for (c, e), _n in ranked_refused[FAILURE_GROUPS:]]
    return lines, omitted


def _baseline_fact(view: dict[str, Any]) -> str | None:
    jobs = view.get("jobs") or []
    by_id = {j["id"]: j for j in jobs}
    named = [n for n in (view.get("pinned_notes") or []) if n.get("kind") == "baseline"]
    job = None
    for n in reversed(named):
        for ref in [*(n.get("evidence_refs") or []), *re.findall(r"job_[0-9a-f]+", str(n.get("text") or ""))]:
            job = job or by_id.get(ref if isinstance(ref, str) else (ref or {}).get("job_id"))
    if job is None:
        job = next((j for j in jobs if j.get("status") == "succeeded" and j.get("purpose") == "reproduction"), None)
    if job is None:
        return None
    metrics = (job.get("receipt") or {}).get("metrics") if isinstance(job.get("receipt"), dict) else None
    produced = {o["artifact_id"] for o in job.get("outputs", [])}
    scored = next((j for j in jobs if j.get("status") == "succeeded" and produced & set((j.get("inputs") or {}).values())
                   and (j.get("receipt") or {}).get("metrics")), None)
    out = f"- Baseline: job {job['id']} ({job.get('component')}, environment {job.get('environment')}), metrics {json.dumps(metrics, sort_keys=True)}"
    if scored:
        out += f"; scored by {scored['id']}: {json.dumps(scored['receipt']['metrics'], sort_keys=True)}"
    return out[:1200]


def _lock_fact(view: dict[str, Any]) -> list[str]:
    out = []
    locks = [n for n in (view.get("pinned_notes") or []) if n.get("kind") in ("candidate_lock", "lock")]
    if locks:
        out.append(f"- Current lock ({locks[-1]['id']}, {len(locks)} recorded): {_redact(str(locks[-1].get('text') or ''))[:600]}")
    active = [p for p in (view.get("evidence_plans") or []) if p.get("status") in ("frozen", "launched", "running", "assessed")]
    if active:
        pl = active[-1]
        out.append(f"- Latest frozen evidence plan: {pl['plan_id']} ({pl['status']}, claim {pl.get('claim')}, "
                   f"assessment {json.dumps(pl.get('assessment'), sort_keys=True)})"[:800])
    return out


def _environment_line(name: str, d: dict[str, Any]) -> str:
    if not d.get("probed"):
        return f"- Environment {name}: capabilities not probed (python {d.get('python_version')})"
    mods = ", ".join(f"{m} {v}" if v else f"{m} MISSING" for m, v in (d.get("modules") or {}).items())
    undecided = d.get("inconclusive_requirements") or []
    return (f"- Environment {name}: python {d.get('python_version')}, interpreter {str(d.get('interpreter_digest'))[:19]}, {mods}"
            + (f"; {len(undecided)} declared requirement(s) could not be checked" if undecided else ""))


def _wall_fact(view: dict[str, Any]) -> str | None:
    c = view.get("completion")
    if not c:
        return None
    if c.get("conclude_now"):
        return (f"- CONCLUDE NOW: {c['remaining_seconds']:.0f} s of wall time remain, reserved for your conclusion. New "
                "compute is refused and waiting is not allowed. Propose conclude with the best scoped result the recorded "
                "evidence supports, its limits, and what was not confirmed.")
    return (f"- Wall time: {c['remaining_seconds']:.0f} s remain; from {c['conclude_threshold_seconds']:.0f} s left the "
            "remainder is reserved for your conclusion and no new compute is admitted. Plan to be done by then.")


def _pinned_section(name: str, lines: list[str], char_limit: int, omitted: list[str], *, newest_first: bool = False) -> list[str]:
    """``lines`` within the section's budget. What did not fit is recorded in ``omitted``."""
    total, share, line_cap = PINNED_BUDGETS[name]
    budget = min(total, int(char_limit * share))
    line_cap = min(line_cap, max(budget - 1, 1))
    ordered = list(reversed(lines)) if newest_first else list(lines)
    kept: list[str] = []
    used = 0
    for i, line in enumerate(ordered):
        if len(line) > line_cap:
            omitted.append(f"{name}: last {len(line) - line_cap + 1} characters of an entry")
            line = line[:line_cap - 1] + "\u2026"
        if used + len(line) + 1 > budget:
            omitted.append(f"{name}: {len(ordered) - i} of {len(ordered)} entries")
            break
        kept.append(line)
        used += len(line) + 1
    return list(reversed(kept)) if newest_first else kept


def _pack_v2(view: dict[str, Any], catalog: list[dict[str, Any]], *, char_limit: int) -> dict[str, Any]:
    inv = view["investigation"]["body"]
    sel: dict[str, list] = {"jobs": [], "claims": [], "catalog": []}
    omitted: dict[str, list] = {"jobs": [], "claims": [], "catalog": [], "known_failures": [], "sections": []}
    question = str(inv.get("question"))
    if len(question) > QUESTION_CHARS:
        omitted["sections"].append(f"question: last {len(question) - QUESTION_CHARS} characters")
        question = question[:QUESTION_CHARS]
    head = [f"# Investigation {view['investigation']['id']} (status {view['investigation']['status']}, horizon {view['horizon']})",
            f"Question: {question}"]
    ch = view.get("charter")
    steer = inv.get("steering") or []
    if len(steer) > MAX_STEERING:
        omitted["sections"].append(f"steering: {len(steer) - MAX_STEERING} earlier instructions")
    raw: dict[str, list[str]] = {
        "charter": [json.dumps({k: ch.get(k) for k in ("study_id", "exploration_only", "population", "comparator", "metrics",
                                                       "resource_limits")}, sort_keys=True)] if ch else [],
        "steering": [f"- [{s['by']}] {s['instruction']}" for s in steer[-MAX_STEERING:]],
        "inputs": ["Declared input artifacts (port -> artifact id): " + json.dumps(inv["inputs"], sort_keys=True)] if inv.get("inputs") else [],
        "task": ["Task: " + json.dumps(inv["task"], sort_keys=True)] if inv.get("task") else [],
        "notes": [f"Notes: {inv['notes']}"] if inv.get("notes") else [],
        "budget": [json.dumps(view.get("budgets"), sort_keys=True)],
    }
    qh = inv.get("question_history") or []
    if len(qh) > 1:
        last = qh[-1]
        raw["revision"] = [f"(Question revision {last['question_revision']} by {last['by']}; previous question: "
                           f"{str(last.get('previous'))[:500]!r}; work admitted earlier: {last.get('running_job_policy')})"]
    if inv.get("model_artifact_id"):
        raw["model"] = [f"Model artifact: {inv['model_artifact_id']}  environment: {inv.get('environment')}"]

    wall = _wall_fact(view)
    baseline = _baseline_fact(view)
    raw["wall"] = [wall] if wall else []
    raw["baseline"] = [baseline or "- Baseline: none recorded yet (reproduce it first)."]
    raw["lock"] = _lock_fact(view)
    raw["success_rules"] = (["- Charter success rule(s): " + json.dumps(ch.get("success_rules"), sort_keys=True)]
                            if ch and ch.get("success_rules") is not None else [])
    env_details = view.get("environment_details") or {}
    raw["environments"] = ([_environment_line(n, d) for n, d in sorted(env_details.items())] if env_details else
                           [f"- Registered environments (names for `environment`): {', '.join(view['environments'])}"]
                           if view.get("environments") else [])
    failure_lines, omitted["known_failures"] = _known_failures(view)
    raw["failures"] = failure_lines
    pinned = {name: _pinned_section(name, lines, char_limit, omitted["sections"], newest_first=name == "steering")
              for name, lines in raw.items()}

    def assemble_head() -> list[str]:
        out = list(head)
        out += pinned.get("revision", []) + pinned.get("model", [])
        out.append("## Charter (fixed; read-only)" if ch else
                   "## Charter: NONE committed — exploratory runs will be refused until a researcher commits one.")
        out += pinned.get("charter", [])
        if pinned.get("steering"):
            out += ["## Operator steering (most recent last)", *pinned["steering"]]
        out += pinned.get("inputs", []) + pinned.get("task", []) + pinned.get("notes", [])
        if pinned.get("budget"):
            out += ["## Budget (exploration scope)", *pinned["budget"]]
        return out

    def assemble_facts() -> list[str]:
        out = ["## Pinned facts (always present; they survive truncation and the job window)"]
        for name in ("wall", "baseline", "lock", "success_rules", "environments"):
            out += pinned.get(name, [])
        if pinned.get("failures"):
            out += ["- Known failures so far (do not repeat one without changing what caused it):", *pinned["failures"]]
        return out

    tail = ["## Tools", TOOL_GUIDE]

    def pinned_chars() -> int:
        return len("\n".join(assemble_head() + assemble_facts() + tail))

    overflow = pinned_chars() + 400 - char_limit
    if overflow > 0 and len(question) > 2000:  # the pinned part must fit: the question gives way first, not the facts
        keep = max(2000, len(question) - overflow)
        omitted["sections"].append(f"question: last {len(question) - keep} characters")
        head[1] = f"Question: {question[:keep]}"
    for name in DROP_ORDER:  # still too large for a small limit: the least valuable sections, whole
        if pinned_chars() + 400 <= char_limit:
            break
        if pinned.get(name):
            omitted["sections"].append(f"{name}: dropped, the context limit is too small for it")
            pinned[name] = []
    head, facts = assemble_head(), assemble_facts()
    room = char_limit - len("\n".join(head + facts + tail)) - 400
    rendered: dict[str, list[str]] = {}

    def fill(section: str, header: str, items: list[tuple[str, str]], newest_first: bool = False,
             ids: list | None = None, skipped: list | None = None) -> None:
        nonlocal room
        order = list(reversed(items)) if newest_first else items
        kept: list[tuple[str, str]] = []
        for i, (ident, line) in enumerate(order):
            if room < len(line) + 1:
                (skipped if skipped is not None else omitted["sections"]).extend(x for x, _ in order[i:])
                break
            room -= len(line) + 1
            kept.append((ident, line))
        if newest_first:
            kept.reverse()
        if kept:
            room -= len(header) + 1
            if ids is not None:
                ids.extend(x for x, _ in kept)
            rendered[section] = [header] + [line for _, line in kept]

    jobs = view.get("jobs") or []
    omitted["jobs"] = [j["id"] for j in jobs[:-MAX_JOBS]] if len(jobs) > MAX_JOBS else []
    job_items = [(j["id"], json.dumps({
        "id": j["id"], "component": j["component"], "environment": j.get("environment"), "status": j["status"],
        "purpose": j["purpose"], "inputs": j.get("inputs"), "config": _clip(j.get("config")),
        "metrics": (j.get("receipt") or {}).get("metrics") if isinstance(j.get("receipt"), dict) else None,
        "error": _error_view(j.get("error")),
        "outputs": [{"slot": o["slot"], "artifact_id": o["artifact_id"], "role": o["role"], "schema": o["schema"]}
                    for o in j.get("outputs", [])]}, sort_keys=True)) for j in jobs[-MAX_JOBS:]]
    plans = view.get("evidence_plans") or []
    results = (view.get("call_results") or [])[-20:]
    created = (view.get("created_artifacts") or [])[-20:]
    claims = view.get("claims") or []
    approved = view.get("approved_protocols")
    # In order of value; the catalog comes last, so it is what a tight budget drops.
    fill("jobs", f"## Jobs ({len(jobs)} total; recent ones, oldest first)", job_items, newest_first=True,
         ids=sel["jobs"], skipped=omitted["jobs"])
    fill("plans", "## Your evidence plans", [(str(p.get("plan_id")), json.dumps(p, sort_keys=True, default=str)) for p in plans])
    fill("results", f"## Results of your recent calls ({len(results)}; ids here are what later calls take)",
         [(f"result:{i}", json.dumps(r, sort_keys=True, default=str)) for i, r in enumerate(results)], newest_first=True)
    fill("created", f"## Artifacts you created ({len(created)}; unused ones have used_by [])",
         [(str(c.get("artifact_id")), json.dumps(c, sort_keys=True)) for c in created], newest_first=True)
    fill("claims", "## Claims", [(c["id"], json.dumps(c, sort_keys=True, default=str)) for c in claims],
         ids=sel["claims"], skipped=omitted["claims"])
    if approved is not None:
        fill("approved", "## Approved (qualified) protocols: templates and assessors usable in a plan, by claim kind",
             [("approved", json.dumps(approved, sort_keys=True) if approved else "none: no plan can be frozen yet")])
    shown = [c for c in catalog[:MAX_CATALOG]]
    omitted["catalog"] = [c["ref"] for c in catalog[MAX_CATALOG:]]
    fill("catalog", f"## Component catalog ({len(catalog)} active)",
         [(c["ref"], json.dumps({k: c.get(k) for k in ("ref", "implements", "kind", "effects", "input", "optional_input",
                                                       "output", "summary", "config_schema") if c.get(k) is not None},
                                sort_keys=True)) for c in shown], ids=sel["catalog"], skipped=omitted["catalog"])

    body: list[str] = []
    for name in ("jobs", "created", "results", "plans", "approved", "claims", "catalog"):
        body += rendered.get(name, [])
    if len(catalog) > len(sel["catalog"]) and "catalog" in rendered:
        body.append("(more components exist: use search via the catalog query; they are data, not tools)")
    text = "\n".join(head + facts + body + tail)
    if len(text) > char_limit:  # the last guard: only a limit too small for the pinned part and the guide gets here
        guide = "\n".join(tail)
        keep = char_limit - len(TRUNCATION_MARK) - len(guide)
        omitted["sections"].append(f"text: last {len(text) - max(keep, 0)} characters before the tool guide")
        text = text[:keep] + TRUNCATION_MARK + guide if keep > 0 else text[:char_limit]
    budget_cut = any(omitted[k] for k in ("sections", "known_failures")) or len(sel["jobs"]) < len(job_items) \
        or len(sel["claims"]) < len(claims) or len(sel["catalog"]) < len(shown)
    return {"policy": POLICY_ID, "text": text, "selections": sel, "omissions": omitted,
            "truncations": {"context_truncated": budget_cut, "budget_left": max(room, 0)}, "chars": len(text),
            "source_manifest_digest": "sha256:" + sha256_bytes(canonical_json(view).encode()),
            "known_tools": sorted(EFFECT_TOOLS) + list(CONTROL_TOOLS)}
