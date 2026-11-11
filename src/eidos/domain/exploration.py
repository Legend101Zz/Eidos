"""Incumbent and challengers until the exploration deadline (ADR 0023). Pure rules, no I/O.

An investigation keeps a versioned **incumbent**: its current ``candidate_lock``. After the first lock, the investigator
spends the exploration allowance on **challenger rounds** and re-locks only when a challenger beats the incumbent on the
**selection rule** recorded before the round (a metric on validation or training cross-validation, with its direction).
The allowance is the wall time and model tokens left after a **hold-back** for reproduction and export, which itself
comes before the conclusion's reserve (ADR 0020). Exploration stops at a genuine ceiling (no improvement in ``patience``
rounds, or a perfect score) or at a prospectively declared search limit; then the investigator finalizes: it reproduces
the incumbent, exports and concludes. The selection is frozen before any confirmation, and assessor feedback is never
selection evidence: once an evidence plan is frozen or an assessment requested, the incumbent cannot change.

Jobs are given in the shape of the investigation view (``id``, ``component``, ``purpose``, ``status``, ``config``,
``inputs`` {port: artifact id}, ``outputs`` [{slot, artifact_id}], ``receipt`` {metrics}).
"""

from __future__ import annotations

import json
import math
from typing import Any

POLICY_ID = "exploration-policy/2"
SELECTION_SPLITS = ("validation", "training-cv")
DIRECTIONS = ("higher_is_better", "lower_is_better")
# Purposes a challenger round uses. Reproduction and confirmation work is finalization, never a challenger.
CHALLENGER_PURPOSES = ("open-search", "hypothesis-test", "measurement-development")
# Decisions with these purposes that start compute are refused once exploration has stopped (finalize phase).
REFUSED_WHEN_FINALIZING = ("open-search", "hypothesis-test")
FINALIZE_PURPOSES = ("reproduction", "confirmation")
# Job kinds that are never investigation experiments (they run for plans, qualification or a component's build).
NON_EXPERIMENT_KINDS = ("evidence-arm", "qualification", "validation", "component-validation")
PROJECT_COMMAND = "eidos.project.command"

DEFAULT_POLICY: dict[str, Any] = {
    "patience_rounds": 3,  # stop after this many challenger rounds without a new incumbent
    "max_rounds": None,  # an optional prospective search limit (rounds after the first lock)
    "holdback_fraction": 0.15,  # of the wall limit, kept for reproduction and export ...
    "min_holdback_seconds": 120.0,  # ... at least this ...
    "max_holdback_seconds": 900.0,  # ... and at most this
    "holdback_token_fraction": 0.15,  # of the model-token limit
    # R14 #3: a reproduction reproduces the incumbent when its selection evidence is no worse than the incumbent's by
    # more than this (absolute, in the metric's units); a rule may declare its own ``reproduce_within``.
    "reproduction_tolerance": 0.02,
}
# Job states after which a job will report nothing more (R14 #6: a round is evaluated only when its challengers are).
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "pruned")
# The perfect value of common bounded metrics, by direction. A selection rule may declare its own ``ceiling``.
BOUNDED_METRICS = {
    "higher_is_better": {"accuracy": 1.0, "balanced_accuracy": 1.0, "f1": 1.0, "macro_f1": 1.0, "micro_f1": 1.0,
                         "weighted_f1": 1.0, "auc": 1.0, "roc_auc": 1.0, "precision": 1.0, "recall": 1.0, "r2": 1.0},
    "lower_is_better": {"error": 0.0, "error_rate": 0.0, "mse": 0.0, "rmse": 0.0, "mae": 0.0},
}


class RuleError(ValueError):
    """A selection rule that cannot be used, with a message for the investigator."""


def policy(config: dict[str, Any] | None) -> dict[str, Any]:
    """The exploration policy: the investigation's ``investigator.exploration`` settings over the defaults. Fixed when
    the investigation starts, so it is prospective."""
    out = dict(DEFAULT_POLICY)
    for k, v in ((config or {}).get("exploration") or {}).items():
        if k in DEFAULT_POLICY:
            out[k] = v
    return out


def parse_rule(raw: Any) -> dict[str, Any]:
    """A selection rule from a ``selection_rule`` note's text (JSON) or a lock's ``selection`` object.

    ``{"metric": "<receipt metric>", "split": "validation"|"training-cv", "direction": "higher_is_better"|
    "lower_is_better", "ceiling"?: number, "max_rounds"?: int}``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise RuleError('a selection rule is a JSON object: {"metric": "<receipt metric>", "split": "validation" or '
                            '"training-cv", "direction": "higher_is_better" or "lower_is_better"}') from None
    if not isinstance(raw, dict):
        raise RuleError("a selection rule is a JSON object with metric, split and direction")
    metric, split = raw.get("metric"), raw.get("split", "validation")
    direction = raw.get("direction", "higher_is_better")
    if not isinstance(metric, str) or not metric.strip():
        raise RuleError("a selection rule names the receipt metric it selects on (\"metric\")")
    if split not in SELECTION_SPLITS:
        raise RuleError(f"a selection rule's split is one of {SELECTION_SPLITS}: selection never uses held-out data")
    if direction not in DIRECTIONS:
        raise RuleError(f"a selection rule's direction is one of {DIRECTIONS}")
    rule: dict[str, Any] = {"metric": metric.strip(), "split": split, "direction": direction}
    if raw.get("ceiling") is not None:
        if not _number(raw["ceiling"]):
            raise RuleError("a selection rule's ceiling is a number")
        rule["ceiling"] = float(raw["ceiling"])
    if raw.get("max_rounds") is not None:
        if not isinstance(raw["max_rounds"], int) or isinstance(raw["max_rounds"], bool) or raw["max_rounds"] < 0:
            raise RuleError("a selection rule's max_rounds is a non-negative integer")
        rule["max_rounds"] = int(raw["max_rounds"])
    # R14 #2: the evidence's provenance is fixed with the rule, before any result is known: the component whose receipt
    # scores a candidate, and the selection data that scoring job reads (its inputs and/or configuration).
    scorer = raw.get("scorer")
    if not isinstance(scorer, str) or not scorer.strip():
        raise RuleError('a selection rule names the component whose receipt scores a candidate ("scorer": "<component '
                        'ref>"), fixed before any result is known')
    data = raw.get("data")
    if not isinstance(data, dict) or not set(data) <= {"inputs", "config"} \
            or not (data.get("inputs") or data.get("config")):
        raise RuleError('a selection rule names the selection data its scorer reads ("data": {"inputs": {"<port>": '
                        '"<artifact id>"}, "config": {"<key>": <value>}}, at least one), fixed before any result is known')
    inputs, cfg = data.get("inputs") or {}, data.get("config") or {}
    if not isinstance(inputs, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in inputs.items()):
        raise RuleError("a selection rule's data.inputs maps input ports to artifact ids")
    if not isinstance(cfg, dict):
        raise RuleError("a selection rule's data.config is an object of configuration values")
    rule["scorer"] = scorer.strip()
    rule["data"] = {**({"inputs": dict(inputs)} if inputs else {}), **({"config": dict(cfg)} if cfg else {})}
    if raw.get("reproduce_within") is not None:
        if not _number(raw["reproduce_within"]) or float(raw["reproduce_within"]) < 0:
            raise RuleError("a selection rule's reproduce_within is a non-negative number")
        rule["reproduce_within"] = float(raw["reproduce_within"])
    return rule


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def note_json(note: dict[str, Any]) -> dict[str, Any] | None:
    try:
        obj = json.loads(note.get("text") or "")
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def rule_from_notes(notes: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    """The investigation's selection rule and the note that recorded it: the first ``selection_rule`` note, or the
    ``selection`` of the first lock that carried one, whichever came first. It is fixed once recorded."""
    for n in notes:
        if n.get("kind") == "selection_rule":
            try:
                return parse_rule(n.get("text")), str(n.get("id"))
            except RuleError:
                continue
        if n.get("kind") == "candidate_lock":
            sel = (note_json(n) or {}).get("selection")
            if sel is not None:
                try:
                    return parse_rule(sel), str(n.get("id"))
                except RuleError:
                    continue
    return None


def metric_value(metrics: Any, name: str) -> float | None:
    """``name`` in a receipt's metrics: the exact key, else the one key whose last segment (after ``:``, ``/`` or
    ``.``) is ``name``'s last segment (``classification-report/1:accuracy`` matches ``accuracy``)."""
    if not isinstance(metrics, dict):
        return None
    if name in metrics:
        return float(metrics[name]) if _number(metrics[name]) else None

    def last(k: str) -> str:
        for sep in (":", "/", "."):
            k = k.rsplit(sep, 1)[-1]
        return k
    hits = [v for k, v in metrics.items() if isinstance(k, str) and last(k) == last(name) and _number(v)]
    return float(hits[0]) if len(hits) == 1 else None


def _metrics(job: dict[str, Any]) -> Any:
    rec = job.get("receipt")
    return rec.get("metrics") if isinstance(rec, dict) else None


def lineage(jobs: list[dict[str, Any]], job_id: str) -> list[dict[str, Any]]:
    """The succeeded jobs that consumed, directly or through other such jobs, an output of ``job_id``, in job order
    (e.g. an inference job reading its model, then a scorer reading the predictions)."""
    produced: set[str] = set()
    root = next((j for j in jobs if j.get("id") == job_id), None)
    if root is None:
        return []
    produced |= {str(o.get("artifact_id")) for o in root.get("outputs") or []}
    out: list[dict[str, Any]] = []
    for j in jobs:  # jobs are in admission order: a consumer comes after what it consumes
        if j.get("id") == job_id or j.get("status") != "succeeded":
            continue
        if produced & {str(v) for v in (j.get("inputs") or {}).values()}:
            out.append(j)
            produced |= {str(o.get("artifact_id")) for o in j.get("outputs") or []}
    return out


def scorer(jobs: list[dict[str, Any]], job_id: str, metric: str | None = None) -> dict[str, Any] | None:
    """The first job of ``job_id``'s lineage that reported metrics (``metric`` among them when given)."""
    for j in lineage(jobs, job_id):
        m = _metrics(j)
        if m and (metric is None or metric_value(m, metric) is not None):
            return j
    return None


def ref_matches(ref: Any, wanted: str) -> bool:
    """A component ref is the rule's scorer: the same ref, or the same component when the rule names no release."""
    if not isinstance(ref, str) or not ref:
        return False
    return ref == wanted or ("@" not in wanted and ref.split("@", 1)[0] == wanted)


def provenance_matches(job: dict[str, Any], rule: dict[str, Any], held_out: set[str] | None = None) -> bool:
    """Whether ``job``'s receipt is selection evidence under ``rule`` (R14 #2): it ran the rule's scorer on the rule's
    selection data (every named input and configuration value), and it read no attested held-out data."""
    if not ref_matches(job.get("component"), str(rule.get("scorer") or "")):
        return False
    inputs = {str(k): str(v) for k, v in (job.get("inputs") or {}).items()}
    if held_out and held_out & set(inputs.values()):
        return False
    data = rule.get("data") or {}
    if any(inputs.get(port) != str(aid) for port, aid in (data.get("inputs") or {}).items()):
        return False
    raw = job.get("config")
    cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return all(k in cfg and cfg[k] == v for k, v in (data.get("config") or {}).items())


def selection_evidence(jobs: list[dict[str, Any]], job_id: str, rule: dict[str, Any],
                       held_out: set[str] | None = None) -> dict[str, Any] | None:
    """The recorded value of the rule's metric for the recipe of ``job_id``: from the first succeeded job of its
    lineage, else the job itself, that ran the rule's scorer on the rule's selection data (R14 #2). Never a number the
    model typed, never a scorer or data chosen after the results, never held-out data."""
    by_id = {j.get("id"): j for j in jobs}
    candidates = lineage(jobs, job_id) + ([by_id[job_id]] if job_id in by_id else [])
    for j in candidates:
        if j.get("status") != "succeeded" or not provenance_matches(j, rule, held_out):
            continue
        v = metric_value(_metrics(j), rule["metric"])
        if v is not None:
            return {"metric": rule["metric"], "split": rule["split"], "value": v, "scored_by": j["id"]}
    return None


def better(challenger: float, incumbent: float, direction: str) -> bool:
    """Strictly better in the rule's direction (a tie keeps the incumbent)."""
    return challenger > incumbent if direction == "higher_is_better" else challenger < incumbent


def ceiling_of(rule: dict[str, Any]) -> float | None:
    if rule.get("ceiling") is not None:
        return float(rule["ceiling"])
    name = rule["metric"]
    for sep in (":", "/", "."):
        name = name.rsplit(sep, 1)[-1]
    return BOUNDED_METRICS.get(rule["direction"], {}).get(name.lower())


def at_ceiling(value: float | None, rule: dict[str, Any]) -> bool:
    top = ceiling_of(rule)
    if value is None or top is None:
        return False
    return value >= top if rule["direction"] == "higher_is_better" else value <= top


def lock_versions(notes: list[dict[str, Any]], lock_job: Any) -> list[dict[str, Any]]:
    """Every incumbent version, oldest first: the locks that name a job, each with what the lab recorded for it
    (``incumbent``: version, evidence, basis) when it did. A lock naming the current incumbent's job again is not a
    new version. ``lock_job`` is ``candidate_lock_job_id``."""
    out: list[dict[str, Any]] = []
    for n in notes:
        if n.get("kind") != "candidate_lock":
            continue
        job = lock_job(n)
        if job is None or (out and out[-1]["job_id"] == job):
            continue
        rec: dict[str, Any] = n["incumbent"] if isinstance(n.get("incumbent"), dict) else {}
        out.append({"version": len(out) + 1, "note_id": n.get("id"), "job_id": job,
                    "evidence": rec.get("evidence"), "basis": rec.get("basis")})
    return out


def recipe_key(job: dict[str, Any]) -> tuple[Any, ...]:
    """A job's recipe: component, inputs and configuration, seed and timeout aside."""
    cfg = job.get("config")
    if isinstance(cfg, dict):
        cfg = {k: v for k, v in cfg.items() if k not in ("seed", "timeout_seconds")}
    return (job.get("component"), json.dumps(job.get("inputs") or {}, sort_keys=True),
            json.dumps(cfg, sort_keys=True, default=str))


def _seed(job: dict[str, Any]) -> Any:
    return job["config"].get("seed") if isinstance(job.get("config"), dict) else None


def tolerance(rule: dict[str, Any], pol: dict[str, Any]) -> float:
    return float(rule["reproduce_within"]) if rule.get("reproduce_within") is not None \
        else float(pol.get("reproduction_tolerance", DEFAULT_POLICY["reproduction_tolerance"]))


def reproduction(jobs: list[dict[str, Any]], job_id: str, rule: dict[str, Any] | None, pol: dict[str, Any] | None = None,
                 held_out: set[str] | None = None) -> dict[str, Any] | None:
    """The evaluation of the incumbent's reproduction against the prospective criterion (R14 #3): later succeeded jobs
    with its recipe under another seed, each scored under the rule. The incumbent is reproduced (``reproduced_by``) by
    the first whose selection evidence is no worse than the incumbent's by more than the rule's tolerance; an attempt
    that is unscored, or that falls short, does not reproduce it. None without a rule or the incumbent."""
    inc = next((j for j in jobs if j.get("id") == job_id), None)
    if inc is None or rule is None:
        return None
    tol = tolerance(rule, pol or DEFAULT_POLICY)
    base = selection_evidence(jobs, job_id, rule, held_out)
    out: dict[str, Any] = {"criterion": {"metric": rule["metric"], "split": rule["split"], "direction": rule["direction"],
                                         "within": tol},
                           "incumbent_value": base["value"] if base else None, "reproduced_by": None, "attempts": []}
    after = False
    for j in jobs:
        if j.get("id") == job_id:
            after = True
            continue
        if not (after and j.get("status") == "succeeded" and recipe_key(j) == recipe_key(inc) and _seed(j) != _seed(inc)):
            continue
        ev = selection_evidence(jobs, str(j["id"]), rule, held_out)
        if ev is None or base is None:
            out["attempts"].append({"job_id": j["id"], "status": "unscored" if ev is None else "incumbent-unscored"})
            continue
        ok = ev["value"] >= base["value"] - tol if rule["direction"] == "higher_is_better" \
            else ev["value"] <= base["value"] + tol
        out["attempts"].append({"job_id": j["id"], "value": ev["value"], "scored_by": ev["scored_by"],
                                "status": "reproduced" if ok else "not-reproduced"})
        if ok:
            out["reproduced_by"] = str(j["id"])
            break
    return out


def reproduced(jobs: list[dict[str, Any]], job_id: str, rule: dict[str, Any] | None, pol: dict[str, Any] | None = None,
               held_out: set[str] | None = None) -> str | None:
    """The job that reproduced the incumbent under the prospective criterion, or None (``reproduction``)."""
    ev = reproduction(jobs, job_id, rule, pol, held_out)
    return ev["reproduced_by"] if ev else None


def reproduction_jobs(jobs: list[dict[str, Any]], recipes_of: list[str]) -> set[str]:
    """Jobs that re-run the recipe of one of ``recipes_of`` (the baseline, every incumbent version), with the jobs that
    read their outputs or those of ``recipes_of`` themselves (inference, scoring): reproduction work, never a
    challenger."""
    by_id = {j.get("id"): j for j in jobs}
    keys = {recipe_key(by_id[i]) for i in recipes_of if i in by_id}
    out = {str(j["id"]) for j in jobs if recipe_key(j) in keys and j.get("id") not in recipes_of}
    for jid in list(out) + [i for i in recipes_of if i in by_id]:  # scoring the incumbent is not a challenger either
        out |= {str(d["id"]) for d in lineage(jobs, jid)}
    return out


def rounds(round_log: list[dict[str, Any]], jobs: list[dict[str, Any]], first_lock_turn: int | None,
           last_change_turn: int | None, excluded: set[str] | None = None, rule: dict[str, Any] | None = None,
           held_out: set[str] | None = None) -> tuple[list[int], int, list[str]]:
    """(the challenger rounds: turns after the first lock that admitted at least one experiment with a challenger
    purpose that is not reproduction work (``excluded``); the number of those after the incumbent last changed that
    are **evaluated**; the challenger jobs of rounds still pending). R14 #6: a round counts as unsuccessful only once
    every challenger it admitted is terminal and, when it succeeded, has selection evidence under ``rule`` (a failed
    challenger is evaluated: it reports nothing more); a pending round may finish.
    ``round_log`` lists, per turn, the job ids its decisions admitted: ``[{"turn": seq, "jobs": [ids]}]``."""
    if first_lock_turn is None:
        return [], 0, []
    by_id = {j.get("id"): j for j in jobs}
    skip = excluded or set()
    per_turn: dict[int, list[str]] = {}
    for r in round_log:
        if int(r["turn"]) <= first_lock_turn:
            continue
        ids = [str(j) for j in r.get("jobs") or [] if (by_id.get(j) or {}).get("purpose") in CHALLENGER_PURPOSES
               and j not in skip]
        if ids:
            per_turn.setdefault(int(r["turn"]), []).extend(ids)
    turns = sorted(per_turn)

    def evaluated(jid: str) -> bool:
        j = by_id.get(jid) or {}
        if j.get("status") not in TERMINAL_STATUSES:
            return False
        return j.get("status") != "succeeded" or (rule is not None and selection_evidence(jobs, jid, rule, held_out)
                                                  is not None)
    pending = [jid for t in turns for jid in per_turn[t] if not evaluated(jid)]
    since = [t for t in turns if (last_change_turn is None or t > last_change_turn)
             and all(evaluated(jid) for jid in per_turn[t])]
    return turns, len(since), pending


def holdback_seconds(pol: dict[str, Any], wall_seconds: float | None) -> float:
    if not wall_seconds:
        return 0.0
    share = float(pol["holdback_fraction"]) * float(wall_seconds)
    return min(float(pol["max_holdback_seconds"]), max(float(pol["min_holdback_seconds"]), share))


def allowance(pol: dict[str, Any], completion: dict[str, Any] | None, wall_seconds: float | None,
              budgets: dict[str, Any] | None) -> dict[str, Any]:
    """What is left for exploration: wall seconds after the conclusion's reserve and the hold-back, and model tokens
    after the token hold-back (None for a dimension without a limit)."""
    out: dict[str, Any] = {"seconds_left": None, "tokens_left": None, "holdback_seconds": None, "holdback_tokens": None}
    if completion:
        hold = holdback_seconds(pol, wall_seconds)
        out["holdback_seconds"] = round(hold, 1)
        out["seconds_left"] = round(float(completion["remaining_seconds"])
                                    - float(completion["conclude_threshold_seconds"]) - hold, 1)
    tok = (budgets or {}).get("model_tokens")
    if isinstance(tok, dict) and tok.get("limit_amount") is not None:
        limit = float(tok["limit_amount"])
        hold_t = float(pol["holdback_token_fraction"]) * limit
        out["holdback_tokens"] = int(hold_t)
        out["tokens_left"] = int(limit - float(tok.get("used") or 0) - float(tok.get("reserved") or 0) - hold_t)
    return out


def state(*, notes: list[dict[str, Any]], jobs: list[dict[str, Any]], round_log: list[dict[str, Any]],
          note_turns: dict[str, int], lock_job: Any, completion: dict[str, Any] | None = None,
          wall_seconds: float | None = None, budgets: dict[str, Any] | None = None, frozen: bool = False,
          config: dict[str, Any] | None = None, baseline_job: str | None = None, held_out: set[str] | None = None,
          decision_tokens: float | None = None) -> dict[str, Any]:
    """Where the investigation stands under the exploration policy (ADR 0023): the selection rule, the incumbent and its
    versions, the challenger rounds, the allowance left, and the phase:

    * ``baseline``: no incumbent yet; find a candidate that beats the baseline and lock it (with a selection rule);
    * ``explore``: run challenger rounds while the allowance remains;
    * ``finalize``: exploration has stopped (``stop`` says why): reproduce the incumbent, export, conclude.

    ``note_turns`` maps a note id to the turn that recorded it; ``held_out`` is the attested held-out artifacts (never
    selection evidence); ``decision_tokens`` is one decision's model-token envelope: an exploratory decision must fit
    in what is left before the hold-back (R14 #4)."""
    pol = policy(config)
    found = rule_from_notes(notes)
    rule = found[0] if found else None
    versions = lock_versions(notes, lock_job)
    incumbent = versions[-1] if versions else None
    first_turn = note_turns.get(str(versions[0]["note_id"])) if versions else None
    change_turn = note_turns.get(str(incumbent["note_id"])) if incumbent else None
    reproductions = reproduction_jobs(jobs, [v["job_id"] for v in versions] + ([baseline_job] if baseline_job else []))
    done, since, pending = rounds(round_log, jobs, first_turn, change_turn, reproductions, rule, held_out)
    left = allowance(pol, completion, wall_seconds, budgets)
    out: dict[str, Any] = {"policy": POLICY_ID, "rule": rule, "rule_note": found[1] if found else None,
                           "versions": versions, "incumbent": incumbent, "rounds": len(done),
                           "rounds_without_improvement": since, "pending_challengers": pending,
                           "allowance": left, "frozen": frozen,
                           "patience_rounds": int(pol["patience_rounds"]), "stop": None, "phase": "baseline"}
    if incumbent is None:
        return out
    if rule is not None and incumbent.get("evidence") is None:
        incumbent["evidence"] = selection_evidence(jobs, incumbent["job_id"], rule, held_out)
    rep = reproduction(jobs, incumbent["job_id"], rule, pol, held_out)
    incumbent["reproduction"] = rep
    incumbent["reproduced_by"] = rep["reproduced_by"] if rep else None
    limit = rule.get("max_rounds") if rule and rule.get("max_rounds") is not None else pol.get("max_rounds")
    out["max_rounds"] = limit
    value = (incumbent.get("evidence") or {}).get("value")
    tokens_left = left["tokens_left"]
    if frozen:
        out["stop"] = "selection-frozen"
    elif rule is not None and at_ceiling(value, rule):
        out["stop"] = "ceiling-perfect-score"
    elif since >= int(pol["patience_rounds"]) > 0:
        out["stop"] = "ceiling-no-improvement"
    elif limit is not None and len(done) >= int(limit):
        out["stop"] = "search-limit"
    elif left["seconds_left"] is not None and left["seconds_left"] <= 0:
        out["stop"] = "wall-holdback"
    elif tokens_left is not None and (tokens_left <= 0 or (decision_tokens and tokens_left < float(decision_tokens))):
        out["stop"] = "token-holdback"
    out["phase"] = "finalize" if out["stop"] else "explore"
    return out


def pending_outputs(jobs: list[dict[str, Any]], pending: list[str]) -> list[str]:
    """The artifacts of pending challengers and of what already read them: scoring these finishes a pending round."""
    out: set[str] = set()
    by_id = {j.get("id"): j for j in jobs}
    for jid in pending:
        out |= {str(o.get("artifact_id")) for o in (by_id.get(jid) or {}).get("outputs") or []}
        for d in lineage(jobs, jid):
            out |= {str(o.get("artifact_id")) for o in d.get("outputs") or []}
    return sorted(out)


def exploration_deadline_unix(st: dict[str, Any], completion: dict[str, Any] | None) -> float | None:
    """When exploratory work must be over (R14 #4, #5): the start of the hold-back, before the conclusion's reserve.
    None when the turn is not exploratory (it may then use the hold-back) or there is no wall limit."""
    if not completion or st.get("phase") != "explore":
        return None
    hold = float((st.get("allowance") or {}).get("holdback_seconds") or 0.0)
    return float(completion["deadline_unix"]) - float(completion["conclude_threshold_seconds"]) - hold


def compact(st: dict[str, Any]) -> dict[str, Any]:
    """What a turn spec records of the state: enough to enforce the finalize phase and to audit it."""
    inc = st.get("incumbent") or {}
    return {"policy": st["policy"], "phase": st["phase"], "stop": st["stop"], "rounds": st["rounds"],
            "versions": len(st.get("versions") or []), "max_rounds": st.get("max_rounds"),
            "patience_rounds": st.get("patience_rounds"),
            "rounds_without_improvement": st["rounds_without_improvement"],
            "pending_challengers": list(st.get("pending_challengers") or []),
            **({"pending_outputs": st["pending_outputs"]} if st.get("pending_outputs") else {}),
            "allowance": st["allowance"],
            "incumbent": {k: inc.get(k) for k in ("version", "job_id", "note_id", "evidence", "basis", "reproduced_by",
                                                  "reproduction")}
            if inc else None,
            "rule": st["rule"]}
