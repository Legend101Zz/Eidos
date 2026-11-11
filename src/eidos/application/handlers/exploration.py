"""The lab side of ADR 0023: the incumbent's versions, challenger evidence, the selection freeze and baseline identity.

* ``check_note``: a ``selection_rule`` note is validated and fixed once recorded; a ``candidate_lock`` that changes the
  incumbent must beat it on the recorded selection rule (evidence from recorded job receipts, never from the note's
  text), and no lock changes once the selection is frozen (an evidence plan frozen, or an assessment requested). Every
  incumbent change is recorded on its note (``incumbent``: version, evidence, previous, basis) and as an event.
* ``exploration_state``: the domain state computed from the ledger (all notes, jobs and turns, not the context window),
  for the investigation view and for ``begin_turn``, which records its compact form in the turn spec.
* ``record_baseline``: the first baseline job of an investigation, and later its scorer, as a durable record
  (``investigation_baseline``) when the job succeeds: the context reads the record instead of guessing from recent jobs.
"""

from __future__ import annotations

from typing import Any

from eidos.application.service import Ctx
from eidos.domain import exploration as rules
from eidos.domain.assessment import candidate_lock_job_id
from eidos.domain.errors import Invalid

NOTE_KINDS = ("candidate_lock", "selection_rule")
BASELINE_KIND = "investigation_baseline"
FROZEN_PLAN_STATUSES_EXCLUDED = ("draft", "cancelled", None)


def view_job(j: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """A ledger job in the shape the investigation view and the domain rules use."""
    return {"id": j["id"], "component": (j["lock"].get("component") or {}).get("ref"), "purpose": j["purpose"],
            "kind": j.get("kind"), "status": j["status"], "config": j["lock"].get("config"),
            "inputs": {k: v["artifact_id"] for k, v in (j["lock"].get("inputs") or {}).items()},
            "receipt": (j.get("terminal") or {}).get("receipt"),
            "outputs": [{"slot": o["slot"], "artifact_id": o["artifact_id"]} for o in outputs]}


def _jobs(ctx: Ctx, inv_id: str) -> list[dict[str, Any]]:
    return [view_job(j, ctx.tx.publications(j["id"]))
            for j in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv_id, limit=100000)
            if j.get("kind") not in rules.NON_EXPERIMENT_KINDS and not (j.get("lock") or {}).get("plan_id")]


def _notes(ctx: Ctx, inv_id: str) -> list[dict[str, Any]]:
    return [{"id": n.id, "kind": n.body.get("kind"), "text": n.body.get("text"), "evidence_refs": n.body.get("evidence_refs"),
             **({"incumbent": n.body["incumbent"]} if isinstance(n.body.get("incumbent"), dict) else {}),
             "created_at": n.created_at}
            for n in ctx.tx.list_records(ctx.project_id, "note", parent_id=inv_id, limit=100000)
            if n.body.get("kind") in NOTE_KINDS]


def turn_log(ctx: Ctx, inv_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """(per turn, the jobs its accepted decisions admitted; the turn of every note a decision recorded)."""
    log: list[dict[str, Any]] = []
    note_turns: dict[str, int] = {}
    for t in ctx.tx.list_turns(inv_id):
        jobs: list[str] = []
        for d in ctx.tx.decisions(t["id"]):
            if not d["accepted"]:
                continue
            rec = ctx.tx.get_record("decision_receipts", d["id"])
            for r in (rec.body.get("receipts") if rec else None) or []:
                res = r.get("result") or {}
                if r.get("ok") and isinstance(res, dict):
                    if res.get("job_id"):
                        jobs.append(str(res["job_id"]))
                    if res.get("note_id"):
                        note_turns[str(res["note_id"])] = int(t["seq"])
        log.append({"turn": int(t["seq"]), "jobs": jobs})
    return log, note_turns


def frozen(ctx: Ctx, inv_id: str) -> bool:
    """Whether the selection is frozen: an evidence plan of the investigation was frozen (or launched), or an
    assessment of its candidate was requested and not refused. Assessor feedback never selects."""
    plans = [r for r in ctx.tx.list_records(ctx.project_id, "evidence_plan", limit=100000)
             if r.body.get("investigation_id") == inv_id and r.status not in FROZEN_PLAN_STATUSES_EXCLUDED]
    if plans:
        return True
    return any(r.status != "refused" for r in ctx.tx.list_records(ctx.project_id, "assessment_request",
                                                                   parent_id=inv_id, limit=100000))


def exploration_state(ctx: Ctx, inv: Any, *, completion: dict[str, Any] | None = None,
                      budgets: dict[str, Any] | None = None) -> dict[str, Any]:
    log, note_turns = turn_log(ctx, inv.id)
    base = ctx.tx.get_record(BASELINE_KIND, inv.id)
    return rules.state(baseline_job=base.body.get("job_id") if base else None, notes=_notes(ctx, inv.id), jobs=_jobs(ctx, inv.id), round_log=log, note_turns=note_turns,
                       lock_job=candidate_lock_job_id, completion=completion,
                       wall_seconds=(inv.body.get("wall") or {}).get("wall_seconds"), budgets=budgets,
                       frozen=frozen(ctx, inv.id), config=inv.body.get("investigator"))


def check_note(ctx: Ctx, inv_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a ``selection_rule`` or ``candidate_lock`` note before it is recorded (ADR 0023). Returns what the
    lab records with a lock that names a job (``incumbent``), or None."""
    kind = body.get("kind")
    notes = _notes(ctx, inv_id)
    existing = rules.rule_from_notes(notes)
    if kind == "selection_rule":
        try:
            rule = rules.parse_rule(body.get("text"))
        except rules.RuleError as exc:
            raise Invalid(str(exc), details={"kind": kind}) from None
        if existing is not None:
            raise Invalid("the selection rule is fixed once recorded: challengers are judged by the rule recorded "
                          "before their round", details={"rule": existing[0], "note_id": existing[1], "proposed": rule})
        return None
    if kind != "candidate_lock":
        return None
    job_id = candidate_lock_job_id(body)
    if job_id is None:  # identifies no job: recorded as before, and assessment refuses it (R13)
        return None
    selection = (rules.note_json(body) or {}).get("selection")
    rule = existing[0] if existing else None
    if selection is not None:
        try:
            proposed = rules.parse_rule(selection)
        except rules.RuleError as exc:
            raise Invalid(str(exc), details={"kind": kind}) from None
        if rule is not None and proposed != rule:
            raise Invalid("the selection rule is fixed once recorded; a lock cannot change it",
                          details={"rule": rule, "note_id": existing[1] if existing else None, "proposed": proposed})
        rule = rule or proposed
    if frozen(ctx, inv_id):
        raise Invalid("the selection is frozen: an evidence plan was frozen or an assessment requested for this "
                      "investigation, so the incumbent cannot change (assessor feedback never selects)",
                      details={"job_id": job_id},
                      next_action="Assess or conclude with the locked candidate.")
    jobs = _jobs(ctx, inv_id)
    versions = rules.lock_versions(notes, candidate_lock_job_id)
    previous = versions[-1] if versions else None
    scored_by = (rules.note_json(body) or {}).get("scored_by")
    evidence = rules.selection_evidence(jobs, job_id, rule, scored_by if isinstance(scored_by, str) else None) \
        if rule else None
    if previous is None:
        return {"version": 1, "job_id": job_id, "evidence": evidence, "basis": "first-lock", "rule": rule}
    if previous["job_id"] == job_id:
        return {"version": previous["version"], "job_id": job_id, "evidence": evidence or previous.get("evidence"),
                "basis": "same-incumbent", "rule": rule}
    prior = {"note_id": previous["note_id"], "job_id": previous["job_id"],
             "evidence": previous.get("evidence") or (rules.selection_evidence(jobs, previous["job_id"], rule)
                                                      if rule else None)}
    out = {"version": previous["version"] + 1, "job_id": job_id, "evidence": evidence, "previous": prior,
           "rule": rule}
    if rule is None:
        # No predefined selection evidence was recorded: the change is allowed and recorded as unverified; the context
        # asks for a rule, and with one every later change must beat the incumbent.
        return {**out, "basis": "unverified-no-selection-rule"}
    if evidence is None:
        raise Invalid(f"the challenger {job_id} has no recorded {rule['metric']!r} on {rule['split']} data: score it "
                      "(a job reading its outputs, or its own receipt) before it can replace the incumbent",
                      details={"job_id": job_id, "rule": rule, "incumbent": prior},
                      next_action="Run the scorer on the challenger's outputs, then lock it if it beats the incumbent.")
    if prior["evidence"] is None:
        return {**out, "basis": "incumbent-unscored"}
    if not rules.better(float(evidence["value"]), float(prior["evidence"]["value"]), rule["direction"]):
        raise Invalid(f"the challenger does not beat the incumbent on the selection rule: {rule['metric']} "
                      f"{evidence['value']} vs {prior['evidence']['value']} ({rule['direction']}); the incumbent stays",
                      details={"challenger": evidence, "incumbent": prior, "rule": rule},
                      next_action="Keep the incumbent; try another challenger or finalize.")
    return {**out, "basis": "beats-incumbent"}


def recorded(ctx: Ctx, note_id: str, inv_id: str, incumbent: dict[str, Any]) -> None:
    """The event of an incumbent change (a new version)."""
    if incumbent.get("basis") == "same-incumbent":
        return
    ctx.event("incumbent.changed", note_id, {k: incumbent.get(k) for k in ("version", "job_id", "evidence", "basis",
                                                                           "previous")},
              investigation_id=inv_id)


# -- baseline identity (ADR 0023, recommendation 5) ---------------------------------------------------------------------

def is_baseline(job: dict[str, Any]) -> bool:
    """A baseline run: the registered project's own command with no authored source, or a job run for reproduction
    before any baseline was recorded."""
    lock = job.get("lock") or {}
    ref = str((lock.get("component") or {}).get("ref") or "")
    if ref.split("@", 1)[0] == rules.PROJECT_COMMAND and "source" not in (lock.get("inputs") or {}):
        return True
    return job.get("purpose") == "reproduction"


def record_baseline(ctx: Ctx, job: dict[str, Any], status: str, info: dict[str, Any]) -> None:
    """On a succeeded investigation job: record the investigation's first baseline job, or the first job that scored
    it (a job with metrics that consumed, directly or through others, the baseline's outputs). Once recorded, neither
    changes: later jobs and a shorter context window cannot move it."""
    inv_id = job.get("investigation_id")
    if status != "succeeded" or not inv_id or job.get("kind") in rules.NON_EXPERIMENT_KINDS \
            or (job.get("lock") or {}).get("plan_id"):
        return
    rec = ctx.tx.get_record(BASELINE_KIND, inv_id, lock=True)
    if rec is None:
        if not is_baseline(job):
            return
        body = {"investigation_id": inv_id, "job_id": job["id"], "component": (job["lock"].get("component") or {}).get("ref"),
                "environment": (job["lock"].get("environment") or {}).get("name"), "purpose": job.get("purpose"),
                "metrics": (info.get("receipt") or {}).get("metrics"), "basis": "automatic", "scored_by": None}
        ctx.tx.put_record(BASELINE_KIND, inv_id, ctx.project_id, body, created_by=ctx.principal.id, status="recorded",
                          parent_id=inv_id)
        ctx.event("baseline.recorded", job["id"], {"job_id": job["id"]}, investigation_id=inv_id)
        return
    if rec.body.get("scored_by") or not (info.get("receipt") or {}).get("metrics"):
        return
    jobs = _jobs(ctx, inv_id)
    jobs = [{**j, "status": "succeeded", "receipt": info.get("receipt")} if j["id"] == job["id"] else j for j in jobs]
    if job["id"] not in {j["id"] for j in rules.lineage(jobs, rec.body["job_id"])}:
        return
    body = {**rec.body, "scored_by": {"job_id": job["id"], "metrics": (info.get("receipt") or {}).get("metrics")}}
    ctx.tx.put_record(BASELINE_KIND, inv_id, ctx.project_id, body, created_by=ctx.principal.id, status="scored",
                      parent_id=inv_id)
    ctx.event("baseline.scored", job["id"], {"baseline_job_id": rec.body["job_id"], "job_id": job["id"]},
              investigation_id=inv_id)
