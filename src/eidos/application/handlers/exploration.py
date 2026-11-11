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

import re
import time
from typing import Any

from eidos.application.service import Ctx
from eidos.domain import exploration as rules
from eidos.domain import turns as turn_rules
from eidos.domain.assessment import candidate_lock_job_id
from eidos.domain.errors import Invalid

NOTE_KINDS = ("candidate_lock", "selection_rule", "baseline")
BASELINE_KIND = "investigation_baseline"
FROZEN_PLAN_STATUSES_EXCLUDED = ("draft", "cancelled", None)


def view_job(j: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """A ledger job in the shape the investigation view and the domain rules use."""
    return {"id": j["id"], "component": (j["lock"].get("component") or {}).get("ref"), "purpose": j["purpose"],
            "created_at": j.get("created_at"),
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


MCP_BATCH_KIND = "mcp-batch"  # the prospective record ``run_batch`` writes before any of its calls (ADR 0025)
ADMISSION_KIND = "round_admission"  # a challenger admitted outside a decision, with its MCP batch if any (H2, R17 #1)


def _batch_notes(ctx: Ctx, inv_id: str) -> list[dict[str, Any]]:
    out = []
    for n in ctx.tx.list_records(ctx.project_id, "note", parent_id=inv_id, limit=100000):
        if n.body.get("kind") != MCP_BATCH_KIND:
            continue
        key = (rules.note_json({"text": n.body.get("text")}) or {}).get("command_key")
        out.append({"id": n.id, "key": str(key) if key else None, "created_at": n.created_at})
    return out


def _at(value: Any) -> tuple[int, Any]:
    return (0, value) if value is not None else (1, 0)


def turn_log(ctx: Ctx, inv_id: str, jobs: list[dict[str, Any]] | None = None
             ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The investigation's shared round history (H2, R17 #1): (per entry, in ledger order, the jobs it admitted; the
    entry of every lock or rule note). Investigator turns and work outside a decision share one history, so the same
    stopping rules (``max_rounds``, patience, ceiling) hold on both paths:

    * an investigator turn: the jobs and notes its accepted decisions recorded;
    * an MCP ``run_batch``: its ``mcp-batch`` record opens an entry, and the challengers its calls admitted (recorded
      at admission, ``round_admission``) belong to it (``batch``: its command key), however many calls or resumptions
      it takes;
    * a challenger admitted outside a decision and outside a batch (a direct call) is its own entry: reading an output
      of an earlier round never hides a new challenger in it;
    * a job that read an output of a then pending challenger when it was admitted (``finishes``, recorded at
      admission) belongs to that challenger's round (``finishing``) and is not one of its challengers: scoring finishes
      a round, it does not start one (R14 #6);
    * a ``candidate_lock`` or ``selection_rule`` note recorded outside a decision (``submit_candidate``,
      ``record_note``) is its own entry, so the rounds after the first lock count from it;
    * any other job admitted outside a decision (never a challenger checked at admission) joins the round of what it
      reads, else the entry before it.

    Entries are numbered from 1 in ledger order (``turn``); a decision's turn keeps its place among them."""
    events: list[tuple[tuple[int, Any], int, str, Any]] = []
    decided_jobs: set[str] = set()
    decided_notes: set[str] = set()
    for t in ctx.tx.list_turns(inv_id):
        t_jobs: list[str] = []
        t_notes: list[str] = []
        for d in ctx.tx.decisions(t["id"]):
            if not d["accepted"]:
                continue
            rec = ctx.tx.get_record("decision_receipts", d["id"])
            for r in (rec.body.get("receipts") if rec else None) or []:
                res = r.get("result") or {}
                if r.get("ok") and isinstance(res, dict):
                    if res.get("job_id"):
                        t_jobs.append(str(res["job_id"]))
                    if res.get("note_id"):
                        t_notes.append(str(res["note_id"]))
        decided_jobs |= set(t_jobs)
        decided_notes |= set(t_notes)
        events.append((_at(t.get("created_at")), 0, "turn", {"jobs": t_jobs, "notes": t_notes, "seq": int(t["seq"])}))
    for b in _batch_notes(ctx, inv_id):
        events.append((_at(b["created_at"]), 0, "batch", b))
    for n in _notes(ctx, inv_id):
        if n.get("kind") in ("candidate_lock", "selection_rule") and str(n["id"]) not in decided_notes:
            events.append((_at(n.get("created_at")), 1, "note", n))
    all_jobs = jobs if jobs is not None else _jobs(ctx, inv_id)
    for j in all_jobs:
        if str(j["id"]) not in decided_jobs:
            events.append((_at(j.get("created_at")), 2, "job", j))
    admitted = {r.id: r.body for r in ctx.tx.list_records(ctx.project_id, ADMISSION_KIND, parent_id=inv_id, limit=100000)}
    events.sort(key=lambda e: (e[0], e[1]))
    log: list[dict[str, Any]] = []
    note_turns: dict[str, int] = {}
    batches: dict[str, dict[str, Any]] = {}
    produced_in: dict[str, dict[str, Any]] = {}  # artifact id -> the entry whose job produced it
    current: dict[str, Any] | None = None
    by_id = {str(j["id"]): j for j in all_jobs}

    def place(entry: dict[str, Any], job_ids: list[str]) -> None:
        for jid in job_ids:
            for o in (by_id.get(jid) or {}).get("outputs") or []:
                produced_in.setdefault(str(o.get("artifact_id")), entry)

    def new_entry(**kw: Any) -> dict[str, Any]:
        e: dict[str, Any] = {"turn": len(log) + 1, "jobs": [], **kw}
        log.append(e)
        return e

    for _, _, what, item in events:
        if what == "job":
            jid = str(item["id"])
            parent = next((produced_in[str(a)] for a in (item.get("inputs") or {}).values() if str(a) in produced_in),
                          None)
            key = (admitted.get(jid) or {}).get("batch")
            if jid in admitted and admitted[jid].get("finishes") and parent is not None:
                # it read a pending challenger's output when admitted: it finishes that round, it is not one of its
                # challengers (R14 #6)
                parent.setdefault("finishing", []).append(jid)
                place(parent, [jid])
                continue
            if jid in admitted:  # a challenger admitted outside a decision (H2): its batch, or its own round
                entry = (batches.get(key) if key else None) or new_entry(source="direct")
            else:
                entry = parent or (current if current is not None and not current.get("note") else None) \
                    or new_entry(source="direct")
            entry["jobs"].append(jid)
            place(entry, [jid])
            continue
        if what == "turn":
            current = new_entry(source="turn", seq=item["seq"])
            current["jobs"].extend(item["jobs"])
            place(current, item["jobs"])
            for nid in item["notes"]:
                note_turns[nid] = current["turn"]
        elif what == "batch":
            current = new_entry(source="batch", batch=item["key"])
            if item["key"]:
                batches.setdefault(item["key"], current)
        else:
            current = new_entry(source="note", note=str(item["id"]))
            note_turns[str(item["id"])] = current["turn"]
    return log, note_turns


def record_admission(ctx: Ctx, inv_id: str | None, purpose: str, job_id: str, lock: dict[str, Any]) -> None:
    """Record a challenger admitted outside a decision in the shared round history (H2, R17 #1): the MCP batch whose
    call admitted it, if any, and whether it read an output of a challenger then pending (``finishes``: it scores or
    completes that round, R14 #6, and is not a new round). A decision's jobs are recorded by its receipts instead."""
    if not inv_id or ctx.admission or purpose not in rules.CHALLENGER_PURPOSES:
        return
    batch = _batch_key(ctx)
    if batch is not None and not any(b["key"] == batch for b in _batch_notes(ctx, inv_id)):
        batch = None
    inputs = {str(i.get("artifact_id")) for i in (lock.get("inputs") or {}).values() if isinstance(i, dict)}
    finishes = False
    if inputs:
        st = exploration_state(ctx, ctx.tx.require("investigation", inv_id))
        finishes = bool(inputs & set(st.get("pending_outputs") or []))
    ctx.tx.put_record(ADMISSION_KIND, job_id, ctx.project_id,
                      {"investigation_id": inv_id, "job_id": job_id, "purpose": purpose, "batch": batch,
                       "finishes": finishes, "command_key": ctx.command_key}, created_by=ctx.principal.id,
                      status="recorded", parent_id=inv_id)


def frozen(ctx: Ctx, inv_id: str) -> bool:
    """Whether the selection is frozen: an evidence plan of the investigation was frozen (or launched), or an
    assessment of its candidate was requested and not refused. Assessor feedback never selects."""
    plans = [r for r in ctx.tx.list_records(ctx.project_id, "evidence_plan", limit=100000)
             if r.body.get("investigation_id") == inv_id and r.status not in FROZEN_PLAN_STATUSES_EXCLUDED]
    if plans:
        return True
    return any(r.status != "refused" for r in ctx.tx.list_records(ctx.project_id, "assessment_request",
                                                                   parent_id=inv_id, limit=100000))


def held_out_artifacts(ctx: Ctx, inv: Any, jobs: list[dict[str, Any]]) -> set[str]:
    """The artifacts the investigation's jobs read that the charter attests as held-out data (by labels digest): never
    selection evidence (R14 #2)."""
    ref = inv.body.get("charter_ref") or {}
    charter = ctx.tx.get_record("charter", ref["id"], ref.get("revision")) if ref.get("id") else None
    digests = set((charter.body.get("held_out") or {}).get("labels") or []) if charter else set()
    if not digests:
        return set()
    out: set[str] = set()
    for aid in {str(v) for j in jobs for v in (j.get("inputs") or {}).values()}:
        art = ctx.tx.get_artifact(aid)
        if art and art.get("digest") in digests:
            out.add(aid)
    return out


def _attested_splits(ctx: Ctx, inv: Any) -> set[str]:
    ref = inv.body.get("charter_ref") or {}
    charter = ctx.tx.get_record("charter", ref["id"], ref.get("revision")) if ref.get("id") else None
    return set((charter.body.get("held_out") or {}).get("splits") or []) if charter else set()


def decision_tokens(inv: Any) -> float | None:
    """One decision's model-token envelope (the decide activity reserves it): an exploratory decision must fit before
    the hold-back (R14 #4)."""
    cfg = inv.body.get("investigator") or {}
    settings = cfg.get("settings") or {}
    default = 0 if cfg.get("runtime", "fixture") == "fixture" else 120_000
    return float(settings.get("max_tokens_per_decision", default)) or None


def exploration_state(ctx: Ctx, inv: Any, *, completion: dict[str, Any] | None = None,
                      budgets: dict[str, Any] | None = None, at_start_of_batch: str | None = None) -> dict[str, Any]:
    """The domain state from the ledger. ``at_start_of_batch`` (an MCP batch's command key): the state as the batch
    began, its own jobs not yet counted, as a decision is checked when it is accepted (H2, R17 #1)."""
    base = ctx.tx.get_record(BASELINE_KIND, inv.id)
    jobs = _jobs(ctx, inv.id)
    log, note_turns = turn_log(ctx, inv.id, jobs)
    if at_start_of_batch is not None:
        log = [r for r in log if r.get("batch") != at_start_of_batch]
    held = held_out_artifacts(ctx, inv, jobs)
    st = rules.state(baseline_job=base.body.get("job_id") if base else None, notes=_notes(ctx, inv.id), jobs=jobs,
                     round_log=log, note_turns=note_turns, lock_job=candidate_lock_job_id, completion=completion,
                     wall_seconds=(inv.body.get("wall") or {}).get("wall_seconds"), budgets=budgets,
                     frozen=frozen(ctx, inv.id), config=inv.body.get("investigator"), held_out=held,
                     decision_tokens=decision_tokens(inv))
    if st.get("pending_challengers"):
        st["pending_outputs"] = rules.pending_outputs(jobs, st["pending_challengers"])
    return st


def _batch_key(ctx: Ctx) -> str | None:
    """The command key of the MCP batch this command is a call of (``<batch key>:<index>``), if any."""
    key = ctx.command_key or ""
    head, _, index = key.rpartition(":")
    return head if head and index.isdigit() else None


def check_admission(ctx: Ctx, inv_id: str | None, purpose: str, lock: dict[str, Any]) -> float | None:
    """ADR 0023 for exploratory work admitted outside a decision: the MCP batch path and direct calls (ADR 0025, H1).
    The same rules as ``accept_decision``, from the shared round history (H2, R17 #1): once exploration has stopped
    (selection frozen, a ceiling, the search limit, the wall or token hold-back, before the first lock too: R17 #3) a
    new job with a challenger purpose (``open-search``, ``hypothesis-test``, ``measurement-development``: R17 #2) is
    refused, unless it reads an output of a pending challenger (it finishes an admitted round, R14 #6). A call of an MCP
    batch is judged by the state as its batch began, as a decision's effects are by the state when it was accepted, so
    a batch is one round and the round limits stop the next one. While exploring, returns when exploratory work must
    end (the start of the hold-back), so the job's wall can be capped there (R14 #4); otherwise None. A decision's
    effects are checked when the decision is accepted, not here."""
    if not inv_id or ctx.admission or purpose not in rules.REFUSED_WHEN_FINALIZING:
        return None
    inv = ctx.tx.require("investigation", inv_id)
    wall = inv.body.get("wall")
    completion = turn_rules.completion_state(wall["deadline_unix"], time.time(), wall["conclude_threshold_seconds"]) \
        if wall else None
    budgets = ctx.tx.budgets(f"inv:{inv.id}")
    batch = _batch_key(ctx)
    if batch is not None and not any(b["key"] == batch for b in _batch_notes(ctx, inv_id)):
        batch = None
    st = exploration_state(ctx, inv, completion=completion, budgets=budgets, at_start_of_batch=batch)
    if st["phase"] != "finalize":
        return rules.exploration_deadline_unix(st, completion)
    pending = set(st.get("pending_outputs") or [])
    if batch is not None:  # the batch's own challengers are pending too: scoring them finishes its round
        pending |= set(exploration_state(ctx, inv, completion=completion, budgets=budgets).get("pending_outputs") or [])
    inputs = {str(i.get("artifact_id")) for i in (lock.get("inputs") or {}).values() if isinstance(i, dict)}
    if pending and inputs & pending:
        return None
    inc = st.get("incumbent") or {}
    message, next_action = rules.stopped_message(st)
    raise Invalid(message,
                  details={"stop": st.get("stop"), "purpose": purpose, "allowance": st.get("allowance"),
                           "rounds": st.get("rounds"), "max_rounds": st.get("max_rounds"),
                           "rounds_without_improvement": st.get("rounds_without_improvement"),
                           "incumbent": {k: inc.get(k) for k in ("version", "job_id", "evidence", "reproduced_by")}
                           if inc else None, **({"candidate": "none"} if not inc else {})},
                  next_action=next_action)


def _check_rule_provenance(ctx: Ctx, inv: Any, rule: dict[str, Any]) -> None:
    """R14 #2: the rule's selection data is never attested held-out data (by split name or labels digest)."""
    data = rule.get("data") or {}
    splits = _attested_splits(ctx, inv)
    named = [v for v in (data.get("config") or {}).values() if isinstance(v, str) and v in splits]
    held = held_out_artifacts(ctx, inv, [{"inputs": data.get("inputs") or {}}])
    if named or held:
        raise Invalid("a selection rule's data is attested held-out data: selection never reads held-out data",
                      details={"rule": rule, "held_out_splits": named, "held_out_artifacts": sorted(held)})


def _check_rule_ports(ctx: Ctx, rule: dict[str, Any]) -> None:
    """Prospective preflight against published scorer manifests, before immutability."""
    scorer = rule["scorer"]
    exact = ctx.tx.get_record("component_release", scorer)
    releases = [exact] if exact else [r for r in ctx.tx.list_records(ctx.project_id, "component_release", limit=5000)
                                    if r.status == "active" and r.body["manifest"].get("id") == scorer]
    if not releases:
        raise Invalid(f"selection scorer {scorer!r} is not published; name a published scorer release")
    bound = set((rule.get("data") or {}).get("inputs") or {})
    for release in releases:
        manifest = release.body["manifest"]
        ports = set(manifest.get("input") or {}) | set(manifest.get("optional_inputs") or {})
        invalid = sorted(bound - ports)
        if invalid:
            raise Invalid(f"selection data binds unknown scorer ports {invalid}; {release.id} accepts {sorted(ports)}",
                          details={"scorer": release.id, "invalid_ports": invalid, "input_ports": sorted(ports)},
                          next_action="Use the scorer's actual input port names before recording the fixed rule.")


def check_note(ctx: Ctx, inv_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a ``selection_rule`` or ``candidate_lock`` note before it is recorded (ADR 0023, R14). Returns what the
    lab records with a lock that names a job (``incumbent``), or None.

    * The rule (metric, split, direction, scorer, selection data, reproduction tolerance) is recorded before any
      challenger round: a first lock without a rule is refused, and the rule is fixed once recorded.
    * Evidence is read only from receipts of the rule's scorer run on the rule's selection data; a lock cannot nominate
      another scorer after the results are known.
    * A re-lock needs recorded evidence for both the incumbent and the challenger under that rule, the challenger
      admitted after the rule was recorded, and the challenger strictly better.
    * The incumbent's reproduction is evaluated against the prospective criterion and recorded with the lock."""
    kind = body.get("kind")
    if kind in ("candidate_lock", "selection_rule") and ctx.tx.get_record("investigation_finalization", inv_id):
        raise Invalid("autonomous finalization fixes the incumbent and selection rule; no lock or rule changes")
    notes = _notes(ctx, inv_id)
    existing = rules.rule_from_notes(notes)
    inv = ctx.tx.require("investigation", inv_id)
    if kind == "selection_rule":
        try:
            new_rule = rules.parse_rule(body.get("text"))
        except rules.RuleError as exc:
            raise Invalid(str(exc), details={"kind": kind}) from None
        if existing is not None:
            raise Invalid("the selection rule is fixed once recorded: challengers are judged by the rule recorded "
                          "before their round", details={"rule": existing[0], "note_id": existing[1], "proposed": new_rule})
        _check_rule_provenance(ctx, inv, new_rule)
        _check_rule_ports(ctx, new_rule)
        return None
    if kind != "candidate_lock":
        return None
    job_id = candidate_lock_job_id(body)
    if job_id is None:  # identifies no job: recorded as before, and assessment refuses it (R13)
        return None
    text = rules.note_json(body) or {}
    selection = text.get("selection")
    rule: dict[str, Any] | None = existing[0] if existing else None
    if selection is not None:
        try:
            proposed = rules.parse_rule(selection)
        except rules.RuleError as exc:
            raise Invalid(str(exc), details={"kind": kind}) from None
        if rule is not None and proposed != rule:
            raise Invalid("the selection rule is fixed once recorded; a lock cannot change it",
                          details={"rule": rule, "note_id": existing[1] if existing else None, "proposed": proposed})
        if rule is None:
            _check_rule_provenance(ctx, inv, proposed)
            _check_rule_ports(ctx, proposed)
        rule = rule or proposed
    versions = rules.lock_versions(notes, candidate_lock_job_id)
    previous = versions[-1] if versions else None
    if (previous is None or previous["job_id"] != job_id) and frozen(ctx, inv_id):  # re-naming the incumbent is fine
        raise Invalid("the selection is frozen: an evidence plan was frozen or an assessment requested for this "
                      "investigation, so the incumbent cannot change (assessor feedback never selects)",
                      details={"job_id": job_id}, next_action="Assess or conclude with the locked candidate.")
    if existing is None and previous is not None and previous["job_id"] != job_id:
        raise Invalid("the selection rule must be recorded before the challenger round: a rule first given with a "
                      "re-lock was chosen after its results were visible",
                      details={"job_id": job_id, "incumbent": previous["job_id"]},
                      next_action="Record the selection rule, run a challenger round under it, then re-lock.")
    if rule is None:
        raise Invalid("a lock needs the selection rule recorded first: add \"selection\" {metric, split, direction, "
                      "scorer, data} to this lock or record a \"selection_rule\" note; challengers are judged by it",
                      details={"job_id": job_id},
                      next_action="Record the selection rule, then lock the candidate.")
    jobs = _jobs(ctx, inv_id)
    held = held_out_artifacts(ctx, inv, jobs)
    evidence = rules.selection_evidence(jobs, job_id, rule, held)
    nominated = text.get("scored_by")
    if nominated is not None and nominated != (evidence or {}).get("scored_by"):
        raise Invalid("a lock cannot nominate its scorer: selection evidence comes from the rule's scorer run on the "
                      "rule's selection data, fixed before the results",
                      details={"scored_by": nominated, "evidence": evidence, "rule": rule})
    pol = rules.policy(inv.body.get("investigator"))
    rep = rules.reproduction(jobs, job_id, rule, pol, held)
    if text.get("reproduced") and not (rep or {}).get("reproduced_by"):
        raise Invalid("the candidate is not reproduced: no later run of its recipe under another seed was scored within "
                      "the rule's reproduction criterion", details={"job_id": job_id, "reproduction": rep})
    common = {"job_id": job_id, "rule": rule, "reproduction": rep}
    if previous is None:
        return {"version": 1, "evidence": evidence, "basis": "first-lock", **common}
    if previous["job_id"] == job_id:
        return {"version": previous["version"], "evidence": evidence or previous.get("evidence"),
                "basis": "same-incumbent", **common}
    prior = {"note_id": previous["note_id"], "job_id": previous["job_id"],
             "evidence": rules.selection_evidence(jobs, previous["job_id"], rule, held)}
    out = {"version": previous["version"] + 1, "evidence": evidence, "previous": prior, **common}
    if evidence is None:
        raise Invalid(f"the challenger {job_id} has no recorded {rule['metric']!r} from the rule's scorer "
                      f"({rule['scorer']}) on its selection data: score it before it can replace the incumbent",
                      details={"job_id": job_id, "rule": rule, "incumbent": prior},
                      next_action="Run the rule's scorer on the challenger's outputs, then lock it if it beats the "
                                  "incumbent.")
    if prior["evidence"] is None:
        raise Invalid(f"the incumbent {previous['job_id']} has no recorded {rule['metric']!r} from the rule's scorer on "
                      "its selection data: score it before any challenger can replace it",
                      details={"job_id": job_id, "rule": rule, "incumbent": prior},
                      next_action="Run the rule's scorer on the incumbent's outputs first.")
    rule_at = _rule_recorded_at(ctx, existing[1] if existing else None)
    challenger = next((j for j in jobs if j["id"] == job_id), None)
    if rule_at is not None and challenger is not None and challenger.get("created_at") is not None \
            and challenger["created_at"] <= rule_at:
        raise Invalid(f"the challenger {job_id} was admitted before the selection rule was recorded: a challenger round "
                      "comes after its rule", details={"job_id": job_id, "rule_note": existing[1] if existing else None},
                      next_action="Run the challenger again in a round after the rule, then lock it if it is better.")
    if not rules.better(float(evidence["value"]), float(prior["evidence"]["value"]), rule["direction"]):
        raise Invalid(f"the challenger does not beat the incumbent on the selection rule: {rule['metric']} "
                      f"{evidence['value']} vs {prior['evidence']['value']} ({rule['direction']}); the incumbent stays",
                      details={"challenger": evidence, "incumbent": prior, "rule": rule},
                      next_action="Keep the incumbent; try another challenger or finalize.")
    return {**out, "basis": "beats-incumbent"}


def final_incumbent(ctx: Ctx, inv: Any) -> dict[str, Any] | None:
    """What a conclusion records of the incumbent it submits (R14 #3): its job, version and selection evidence, and
    whether it was reproduced, evaluated now against the rule's prospective reproduction criterion from recorded
    receipts. An incumbent is submitted as reproduced only when that evaluation passed."""
    st = exploration_state(ctx, inv)
    inc = st.get("incumbent")
    if not inc:
        return None
    rep = inc.get("reproduction")
    return {"job_id": inc["job_id"], "version": inc["version"], "evidence": inc.get("evidence"),
            "reproduced": bool(inc.get("reproduced_by")), "reproduced_by": inc.get("reproduced_by"),
            "reproduction": rep, "rule": st.get("rule")}


def _rule_recorded_at(ctx: Ctx, note_id: str | None) -> Any:
    rec = ctx.tx.get_record("note", note_id) if note_id else None
    return rec.created_at if rec else None


def recorded(ctx: Ctx, note_id: str, inv_id: str, incumbent: dict[str, Any]) -> None:
    """The event of an incumbent change (a new version)."""
    if incumbent.get("basis") == "same-incumbent":
        return
    ctx.event("incumbent.changed", note_id, {k: incumbent.get(k) for k in ("version", "job_id", "evidence", "basis",
                                                                           "previous")},
              investigation_id=inv_id)


# -- baseline identity (ADR 0023, recommendation 5) ---------------------------------------------------------------------

def is_baseline(job: dict[str, Any], designated: set[str] | None = None) -> bool:
    """A baseline run (R14 #7): the explicit baseline recipe (the registered project's own command with no authored
    source), or a job a ``baseline`` note designated. A job's purpose (e.g. ``reproduction``, which also reproduces
    candidates) never makes it the baseline."""
    lock = job.get("lock") or {}
    ref = str((lock.get("component") or {}).get("ref") or "")
    if ref.split("@", 1)[0] == rules.PROJECT_COMMAND and "source" not in (lock.get("inputs") or {}):
        return True
    return job.get("id") in (designated or set())


def designated_baselines(ctx: Ctx, inv_id: str) -> set[str]:
    """The jobs the investigation's ``baseline`` notes name (in their text or evidence_refs)."""
    out: set[str] = set()
    for n in _notes(ctx, inv_id):
        if n.get("kind") != "baseline":
            continue
        refs = [r if isinstance(r, str) else (r or {}).get("job_id") for r in (n.get("evidence_refs") or [])]
        out |= {str(r) for r in refs if r}
        out |= set(re.findall(r"job_[0-9a-f]+", str(n.get("text") or "")))
    return out


def designate_baseline(ctx: Ctx, inv_id: str, body: dict[str, Any]) -> None:
    """A ``baseline`` note naming an already succeeded job of the investigation records it as the baseline when none
    is recorded yet (R14 #7: an explicit designation)."""
    if ctx.tx.get_record(BASELINE_KIND, inv_id) is not None:
        return
    named = designated_baselines(ctx, inv_id) | set(re.findall(r"job_[0-9a-f]+", str(body.get("text") or ""))) | {
        str(r if isinstance(r, str) else (r or {}).get("job_id")) for r in (body.get("evidence_refs") or []) if r}
    for j in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv_id, limit=100000):
        if j["id"] in named and j["status"] == "succeeded":
            record_baseline(ctx, j, "succeeded", {"receipt": (j.get("terminal") or {}).get("receipt")},
                            designated=named)
            return


def record_baseline(ctx: Ctx, job: dict[str, Any], status: str, info: dict[str, Any],
                    designated: set[str] | None = None) -> None:
    """On a succeeded investigation job: record the investigation's first baseline job, or the first job that scored
    it (a job with metrics that consumed, directly or through others, the baseline's outputs). Once recorded, neither
    changes: later jobs and a shorter context window cannot move it."""
    inv_id = job.get("investigation_id")
    if status != "succeeded" or not inv_id or job.get("kind") in rules.NON_EXPERIMENT_KINDS \
            or (job.get("lock") or {}).get("plan_id"):
        return
    rec = ctx.tx.get_record(BASELINE_KIND, inv_id, lock=True)
    if rec is None:
        if not is_baseline(job, designated if designated is not None else designated_baselines(ctx, inv_id)):
            return
        body = {"investigation_id": inv_id, "job_id": job["id"], "component": (job["lock"].get("component") or {}).get("ref"),
                "environment": (job["lock"].get("environment") or {}).get("name"), "purpose": job.get("purpose"),
                "metrics": (info.get("receipt") or {}).get("metrics"),
                "basis": "automatic" if is_baseline(job) else "designated", "scored_by": None}
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
