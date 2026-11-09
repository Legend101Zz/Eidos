"""The five recovery cases of the design's interruption table that were `not-started` in v1.0.1
(ACCEPTANCE_MATRIX § C; ADR 0010), against real PostgreSQL and the real command service:

1. storage/database lag: staged bytes are verified again right before publication;
2. the human changes the question or withdraws a release mid-investigation;
3. the assessor restarts or the same evidence arrives twice;
4. an assessment method is withdrawn during confirmation;
5. a branch request loses its reply.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import pytest

from eidos.domain.errors import ContentUnverifiable, Invalid, QuestionRevised, WithdrawnMethod

pytestmark = pytest.mark.pg


def _events(lab, pid, etype):
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        return [e for e in tx.events_after(pid, 0, limit=100000) if e["type"] == etype]


def _op_component(lab, pid, cid="rc.op", output=None, inputs=None):
    from test_command_path import _publish, _register_lab_env
    if cid in ("rc.op", "rc.train"):
        _register_lab_env(lab, pid)
    man = {"id": cid, "release": "1.0.0", "implements": "demo/1", "kind": "artifact-op", "entry": "m:run",
           "scientific_effects": ["observe"], "output": output or {"report": "demo-report/1"}}
    if inputs:
        man["input"] = inputs
    return _publish(lab, pid, man, {"m.py": "def run(i, c):\n    return {}\n"})["component_ref"]


def _manifest(job, st, outputs, receipt=None):
    return {"wire": "eidos-wire/1", "job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
            "status": "succeeded", "outputs": outputs, "receipt": receipt or {}}


# ---------------------------------------------------------------------------------------------
# 1. Storage or database lag
# ---------------------------------------------------------------------------------------------

def test_publication_reverifies_staged_bytes_and_refuses_changed_or_vanished_content(lab):
    from test_command_path import project
    pid = project(lab)
    ref = _op_component(lab, pid, output={"a": "demo-report/1", "b": "demo-report/1"})
    job = lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": "lab"}, project=pid)["result"]["job_id"]
    launcher = lab.principal(f"l-{pid}", ["launcher"], [pid])
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    da, na = lab.store.put_bytes(b'{"a": 1}')
    db, nb = lab.store.put_bytes(b'{"b": 22}')
    outs = [{"slot": "a", "filename": "a.json", "digest": da, "size": na},
            {"slot": "b", "filename": "b.json", "digest": db, "size": nb}]
    args = {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"], "manifest": _manifest(job, st, outs)}
    pb = Path(lab.store.path_for(db))

    def damage(data: bytes | None):
        if pb.exists():
            os.chmod(pb, 0o640)
        if data is None:
            pb.unlink()
        else:
            pb.write_bytes(data)

    # Vanished (b), changed bytes at the same size, and a changed size: each refuses the WHOLE publication.
    for data, reason in ((None, "missing"), (b'{"b": 23}', "digest-changed"), (b'{"b": 2}', "size-changed")):
        damage(data)
        with pytest.raises(ContentUnverifiable) as ei:
            lab.cmd(launcher, "publish_outputs", args, project=pid, key="publish-1")
        assert ei.value.details["unverifiable"] == [{"slot": "b", "digest": db, "size": nb, "reason": reason}]
        assert lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)["outputs"] == []  # slot a not published either
    # Re-staging the original bytes repairs the blob (a damaged blob is replaced, not trusted).
    assert lab.store.put_bytes(b'{"b": 22}') == (db, nb)
    # Storage lag: the blob appears shortly after the publish began; the bounded re-check sees it.
    damage(None)
    t = threading.Timer(0.03, lambda: lab.store.put_bytes(b'{"b": 22}'))
    t.start()
    # The refused attempts above committed nothing, so the same command key is still unused.
    r = lab.cmd(launcher, "publish_outputs", args, project=pid, key="publish-1")
    t.join()
    assert not r["replayed"] and sorted(o["slot"] for o in r["result"]["published"]) == ["a", "b"]
    assert lab.store.check(db, nb) is None


def test_runner_restages_once_then_fails_the_attempt_explicitly(tmp_path):
    """The launcher side: a CONTENT_UNVERIFIABLE refusal is answered by re-staging the attempt's own staging file
    and retrying under the same key; if the refusal persists, nothing is published and the slots are reported."""
    from eidos.client import LabError
    from eidos.runtime.artifacts import LocalArtifactStore
    from eidos.runtime.runner import AttemptRunner

    store = LocalArtifactStore(tmp_path / "store", min_free_bytes=0)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "r.json").write_bytes(b'{"r": 1}')
    digest, size = store.put_file(staging / "r.json")
    Path(store.path_for(digest)).chmod(0o640)
    Path(store.path_for(digest)).unlink()  # the blob vanished after staging
    calls = []

    class Client:
        def __init__(self, persist):
            self.persist = persist

        def command(self, name, args, project, key):
            calls.append((name, key))
            if self.persist or len(calls) == 1 or store.check(digest, size) is not None:
                raise LabError(409, {"code": "CONTENT_UNVERIFIABLE", "message": "x", "details": {"unverifiable": [
                    {"slot": "r", "digest": digest, "size": size, "reason": "missing"}]}})
            return {"result": {"published": [{"slot": "r"}]}}

    r = AttemptRunner.__new__(AttemptRunner)
    r.store = store
    man = {"outputs": [{"slot": "r", "filename": "r.json", "digest": digest, "size": size}]}
    ctx = {"job_id": "job_x", "attempt_id": "att_x", "epoch": 1}
    r.client = Client(persist=False)
    assert r._publish_verified("p", ctx, man, staging) is None
    assert calls == [("publish_outputs", "publish:att_x")] * 2 and store.check(digest, size) is None
    calls.clear()
    r.client = Client(persist=True)
    bad = r._publish_verified("p", ctx, man, staging)
    assert bad and bad[0]["reason"] == "missing" and len(calls) == 2


# ---------------------------------------------------------------------------------------------
# 2. The human changes the question or withdraws a release mid-investigation
# ---------------------------------------------------------------------------------------------

def _investigation(lab, pid):
    from test_command_path import CHARTER
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "does the edit help?"}, project=pid)["result"]
    lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER, "investigation_id": inv["investigation_id"]},
            project=pid)
    return inv["investigation_id"]


def test_question_change_is_seen_at_the_next_turn_with_policy_and_exposure(lab):
    from test_command_path import project
    pid = project(lab)
    ref = _op_component(lab, pid)
    inv = _investigation(lab, pid)
    investigator = lab.principal(f"inv-{pid}", ["investigator"], [pid])
    human = lab.principal(f"human-{pid}", ["researcher"], [pid])
    t1 = lab.cmd(investigator, "begin_turn", {"investigation_id": inv}, project=pid)["result"]
    old_rev = t1["spec"]["study_revision"]
    j_drain = lab.cmd(investigator, "run_component", {"component_ref": ref, "environment": "lab",
                      "investigation_id": inv}, project=pid)["result"]["job_id"]
    # Revision 2 of the question, drain policy: admitted work keeps running, the turn in flight is not altered.
    r = lab.cmd(human, "revise_question", {"investigation_id": inv, "question": "does the edit help on long pairs?",
                "reason": "scope narrowed after the pilot"}, project=pid)["result"]
    assert r["question_revision"] == 2 and r["effective"] == "next-turn" and r["admitted_work"] == [j_drain]
    assert not lab.q(lab.admin, "get_job", {"job_id": j_drain}, project=pid)["cancel_requested"]
    # Exposure: what the human could have seen when changing the question is recorded and linked.
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        exp = [e for e in tx.exposures(pid) if e["subject"] == f"question:{inv}"]
    assert len(exp) == 1 and exp[0]["id"] == r["exposure_id"] and exp[0]["principal_id"] == human.id
    assert exp[0]["study_id"] == "s1" and exp[0]["disclosed"]["kind"] == "question-revision"
    # The turn in flight never sees the change: its view at its horizon keeps the old question, and a decision it
    # proposes now is recorded unapplied.
    view = lab.q(investigator, "investigation_view", {"investigation_id": inv, "horizon": t1["spec"]["horizon"]},
                 project=pid)
    assert view["investigation"]["body"]["question"] == "does the edit help?"
    d = lab.cmd(investigator, "accept_decision", {"turn_id": t1["turn_id"], "proposal": {
        "rationale": "r", "purpose": "open-search", "calls": [], "control": {"action": "wait_for_jobs"}}},
        project=pid)["result"]
    assert d == {**d, "accepted": False, "reason": "stale-revision"}
    # Drain policy: a call of the stale turn may still admit work (defined policy, labelled in the event).
    lab.cmd(investigator, "run_component", {"component_ref": ref, "environment": "lab", "investigation_id": inv},
            project=pid)
    # Revision 3 under a stopping policy: admitted work is cancelled, and the stale turn admits nothing more.
    r3 = lab.cmd(human, "revise_question", {"investigation_id": inv, "question": "is the effect specific?",
                 "running_job_policy": "cancel"}, project=pid)["result"]
    assert r3["question_revision"] == 3 and j_drain in r3["admitted_work"]
    assert lab.q(lab.admin, "get_job", {"job_id": j_drain}, project=pid)["cancel_requested"]
    with pytest.raises(QuestionRevised):
        lab.cmd(investigator, "run_component", {"component_ref": ref, "environment": "lab", "investigation_id": inv},
                project=pid)
    lab.cmd(human, "run_component", {"component_ref": ref, "environment": "lab", "investigation_id": inv},
            project=pid)  # the human, who made the change, is not blocked
    ev = _events(lab, pid, "investigation.question_revised")
    assert [e["body"]["question_revision"] for e in ev] == [2, 3]
    # The next turn starts at the new revision and sees the new question and its history.
    lab.cmd(investigator, "complete_turn", {"turn_id": t1["turn_id"], "stop_reason": "calls-exhausted"}, project=pid)
    t2 = lab.cmd(investigator, "begin_turn", {"investigation_id": inv}, project=pid)["result"]
    assert t2["spec"]["study_revision"] > old_rev
    view2 = lab.q(investigator, "investigation_view", {"investigation_id": inv, "horizon": t2["spec"]["horizon"]},
                  project=pid)
    body = view2["investigation"]["body"]
    assert body["question"] == "is the effect specific?"
    assert [h["question_revision"] for h in body["question_history"]] == [1, 2, 3]
    from eidos.investigator.context import pack
    text = pack(view2, [])["text"] if "text" in pack(view2, []) else json.dumps(pack(view2, []))
    assert "Question revision 3" in text
    lab.cmd(investigator, "run_component", {"component_ref": ref, "environment": "lab", "investigation_id": inv},
            project=pid)  # a fresh turn admits work again
    with pytest.raises(Invalid):
        lab.cmd(human, "revise_question", {"investigation_id": inv, "question": "is the effect specific?"}, project=pid)


def test_release_withdrawn_mid_investigation_drains_or_stops_and_wakes_the_next_turn(lab):
    from test_command_path import project
    pid = project(lab)
    ref = _op_component(lab, pid)
    inv = _investigation(lab, pid)
    investigator = lab.principal(f"inv-{pid}", ["investigator"], [pid])
    j1 = lab.cmd(investigator, "run_component", {"component_ref": ref, "environment": "lab", "investigation_id": inv},
                 project=pid)["result"]["job_id"]
    w = lab.cmd(lab.admin, "withdraw_component", {"component_ref": ref, "defect": "wrong denominator"},
                project=pid)["result"]
    assert w["investigations_notified"] == [inv] and w["stopped_jobs"] == []
    assert not lab.q(lab.admin, "get_job", {"job_id": j1}, project=pid)["cancel_requested"]  # scientific: drain
    ev = _events(lab, pid, "investigation.release_withdrawn")
    assert len(ev) == 1 and ev[0]["body"]["release"] == ref and ev[0]["body"]["severity"] == "scientific"
    with lab.ledger.transaction() as tx:  # the wake reaches the coordinator through the outbox
        tx.project_id = pid
        assert any(o["topic"] == "investigation.wake" and o["payload"]["event_seq"] == ev[0]["seq"]
                   for o in tx.c.execute(__import__("sqlalchemy").text(
                       "SELECT topic, payload FROM outbox WHERE payload->>'project_id'=:p"), {"p": pid}).mappings())
    with pytest.raises(WithdrawnMethod):  # a withdrawn release admits no new work
        lab.cmd(investigator, "run_component", {"component_ref": ref, "environment": "lab", "investigation_id": inv},
                project=pid)
    # Security withdrawal of another release: admitted work is stopped independently of any turn.
    ref2 = _op_component(lab, pid, cid="rc.op2")
    j2 = lab.cmd(investigator, "run_component", {"component_ref": ref2, "environment": "lab", "investigation_id": inv},
                 project=pid)["result"]["job_id"]
    w2 = lab.cmd(lab.admin, "withdraw_component", {"component_ref": ref2, "defect": "exfiltrates labels",
                 "severity": "security"}, project=pid)["result"]
    assert w2["stopped_jobs"] == [j2] and lab.q(lab.admin, "get_job", {"job_id": j2}, project=pid)["cancel_requested"]


# ---------------------------------------------------------------------------------------------
# 3 and 4. Assessment: restart / duplicate evidence, and a method withdrawn during confirmation
# ---------------------------------------------------------------------------------------------

def _executed_plan(lab, *, finish_all=True):
    from test_plan_pipelines import _finish, _setup
    pid, plan_id = _setup(lab)
    lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)
    launcher = lab.principal(f"l-{pid}", ["launcher"], [pid])

    def finish_ready():
        plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
        for key, jid in sorted(plan["body"]["launch"]["steps"].items()):
            if lab.q(lab.admin, "get_job", {"job_id": jid}, project=pid)["status"] == "admitted":
                _finish(lab, pid, launcher, jid, slot="checkpoint" if key.endswith("train") else "predictions")
                return True
        return False

    if finish_all:
        while finish_ready():
            pass
    return pid, plan_id, finish_ready


def _evidence(lab, pid, plan_id):
    inputs = lab.q(lab.admin, "assessment_inputs", {"plan_id": plan_id}, project=pid)
    es = {arm: [o["artifact_id"] for o in a["outputs"] if o["slot"] == "predictions"]
          for arm, a in inputs["arms"].items()}
    return inputs, es


def test_assessor_restart_or_duplicate_evidence_returns_the_same_assessment_receipt(lab):
    pid, plan_id, _ = _executed_plan(lab)
    inputs, es = _evidence(lab, pid, plan_id)
    assert all(len(v) == 2 for v in es.values())
    assessor = lab.principal(f"a-{pid}", ["assessor"], [pid])
    payload = {"plan_id": plan_id, "plan_digest": inputs["plan_digest"], "assessor_ref": "p.assess@1.0.0",
               "evidence_set": es, "evaluation": {"outcome": "inconclusive"}}
    first = lab.cmd(assessor, "submit_assessment", payload, project=pid, key="assess-run-1")
    # A restarted assessor resubmits under a NEW key, with the same evidence in another order and a re-run
    # evaluation; and the same submission is also replayed under its original key.
    again = lab.cmd(assessor, "submit_assessment", {**payload, "evidence_set": {k: list(reversed(v)) for k, v in
                    reversed(list(es.items()))}, "evaluation": {"outcome": "inconclusive", "note": "re-run"}},
                    project=pid, key="assess-run-2")
    replay = lab.cmd(assessor, "submit_assessment", payload, project=pid, key="assess-run-1")
    strip = lambda r: {k: v for k, v in r.items() if k != "deduplicated"}  # noqa: E731
    assert first["result"]["deduplicated"] is False and again["result"]["deduplicated"] is True
    assert strip(again["result"]) == strip(first["result"]) and replay["replayed"] and replay["result"] == first["result"]
    assert first["result"]["conformance"] and first["result"]["evidence_set_digest"]
    records = lab.q(lab.admin, "list_records", {"kind": "assessment"}, project=pid)
    assert [r["id"] for r in (records.get("records") if isinstance(records, dict) else records)] == \
        [first["result"]["assessment_id"]]
    assert len(_events(lab, pid, "assessment.committed")) == 1
    assert len(_events(lab, pid, "assessment.deduplicated")) == 1
    # The assessor activity's key covers the whole payload, so a re-run can never collide with an earlier key.
    import inspect

    from eidos.orchestration import activities
    assert "canonical_json(payload)" in inspect.getsource(activities.AssessorActivities.assess_plan)


def _promote_alternate_assessor(lab, pid, cid="p.assess2"):
    from test_command_path import _publish
    _publish(lab, pid, {"id": cid, "release": "1.0.0", "implements": "protocol-assessment/1", "kind": "assessment",
                        "entry": "m:run", "scientific_effects": ["assess"]}, {"m.py": "x=2\n"})
    suite = {"target": {"kind": "assessor"}, "scope": ["improves"], "controls": [
        {"id": "p", "type": "planted", "input": "f1"}, {"id": "n", "type": "null", "input": "f2"}]}
    s = lab.cmd(lab.admin, "register_control_suite", {"suite": suite, "suite_id": f"suite:{cid}"}, project=pid)["result"]
    rel = lab.q(lab.admin, "describe_component", {"component_ref": f"{cid}@1.0.0"}, project=pid)
    lab.cmd(lab.admin, "submit_qualification", {"suite_id": s["suite_id"], "suite_digest": s["suite_digest"],
            "release_ref": f"{cid}@1.0.0", "release_digest": rel["body"]["digest"],
            "results": {"p": [{"detected": True}], "n": [{"detected": False}]}}, project=pid)
    lab.cmd(lab.admin, "promote_protocol", {"release_ref": f"{cid}@1.0.0", "scope": ["improves"]}, project=pid)
    return f"{cid}@1.0.0"


def test_assessor_withdrawn_during_confirmation_fails_closed_then_reassesses_under_a_corrected_release(lab):
    pid, plan_id, finish_ready = _executed_plan(lab, finish_all=False)
    assert finish_ready()  # confirmation under way: one job finished, the rest still admitted
    frozen = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    w = lab.cmd(lab.admin, "withdraw_component", {"component_ref": "p.assess@1.0.0",
                "defect": "pairs counted twice"}, project=pid)["result"]
    assert w["blocked_plans"] == [plan_id] and w["suspended_approvals"] == ["approval:p.assess@1.0.0"]
    while finish_ready():  # admitted work drains: receipts are kept
        pass
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    assert plan["status"] == "assessment-blocked" and plan["labels"]["blocked_by"] == w["withdrawal_id"]
    assert plan["digest"] == frozen["digest"] or plan["body"]["arms"] == frozen["body"]["arms"]  # frozen plan kept
    with lab.ledger.transaction() as tx:  # no assessment is requested under the withdrawn method
        tx.project_id = pid
        kinds = [o["topic"] for o in tx.c.execute(__import__("sqlalchemy").text(
            "SELECT topic FROM outbox WHERE payload->>'project_id'=:p"), {"p": pid}).mappings()]
    assert "assessment.request" not in kinds
    inputs, es = _evidence(lab, pid, plan_id)
    assessor = lab.principal(f"a-{pid}", ["assessor"], [pid])
    with pytest.raises(WithdrawnMethod):  # fail closed, even with a valid digest and evidence
        lab.cmd(assessor, "submit_assessment", {"plan_id": plan_id, "plan_digest": inputs["plan_digest"],
                "assessor_ref": "p.assess@1.0.0", "evidence_set": es, "evaluation": {"outcome": "protocol-passed"}},
                project=pid)
    researcher = lab.principal(f"r-{pid}", ["researcher"], [pid])
    with pytest.raises(Invalid):  # the withdrawn release cannot be the corrected one
        lab.cmd(researcher, "reassess_evidence_plan", {"plan_id": plan_id, "assessor_ref": "p.assess@1.0.0"},
                project=pid)
    alt = _promote_alternate_assessor(lab, pid)
    r = lab.cmd(researcher, "reassess_evidence_plan", {"plan_id": plan_id, "assessor_ref": alt,
                "reason": "corrected pair counting"}, project=pid)["result"]
    new = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": r["plan_id"]}, project=pid)
    assert new["status"] == "awaiting-assessment" and new["body"]["assessor"]["ref"] == alt
    assert new["body"]["reassessment_of"]["plan_id"] == plan_id and new["body"]["frozen_by"] == researcher.id
    assert new["body"]["launch"] == plan["body"]["launch"]  # the executed receipts are reused, not re-run
    inputs2, es2 = _evidence(lab, pid, r["plan_id"])
    a = lab.cmd(assessor, "submit_assessment", {"plan_id": r["plan_id"], "plan_digest": inputs2["plan_digest"],
                "assessor_ref": alt, "evidence_set": es2, "evaluation": {"outcome": "inconclusive"}},
                project=pid)["result"]
    assert a["plan_id"] == r["plan_id"]
    old = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    assert old["status"] == "assessment-blocked" and old["labels"]["reassessed_as"] == r["plan_id"]
    with pytest.raises(Invalid):  # one reassessment per blocked plan
        lab.cmd(researcher, "reassess_evidence_plan", {"plan_id": plan_id, "assessor_ref": alt}, project=pid)


# ---------------------------------------------------------------------------------------------
# 5. A branch request loses its reply
# ---------------------------------------------------------------------------------------------

def test_branch_request_retried_with_the_same_key_returns_the_same_branch(lab):
    from test_command_path import project
    pid = project(lab)
    ref = _op_component(lab, pid, cid="rc.train", output={"checkpoint": "eidos-checkpoint/1"})
    job = lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": "lab"}, project=pid)["result"]["job_id"]
    launcher = lab.principal(f"l-{pid}", ["launcher"], [pid])
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    d, n = lab.store.put_bytes(uuid.uuid4().hex.encode())
    pub = lab.cmd(launcher, "publish_outputs", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                  "manifest": _manifest(job, st, [{"slot": "checkpoint", "filename": "c.pt", "digest": d, "size": n,
                                                   "role": "checkpoint", "schema": "eidos-checkpoint/1"}],
                                        {"checkpoints": {"checkpoint": {"step": 45, "complete": True,
                                                                        "state_entries": ["model", "optimizer"]}}})},
                  project=pid)["result"]
    lab.cmd(launcher, "finish_attempt", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                                         "status": "succeeded"}, project=pid)
    parent = pub["published"][0]["artifact_id"]
    cont = _op_component(lab, pid, cid="rc.continue", output={"checkpoint": "eidos-checkpoint/1"},
                         inputs={"parent_checkpoint": "eidos-checkpoint/1"})
    req = {"parent_checkpoint_id": parent, "component_ref": cont, "environment": "lab",
           "migration": {"model": "copy", "optimizer": "copy"}, "stream_policy": "paired",
           "intervention": {"kind": "none"}}
    first = lab.cmd(lab.admin, "plan_branch", req, project=pid, key="branch-key-1")
    lost = lab.cmd(lab.admin, "plan_branch", req, project=pid, key="branch-key-1")  # the reply was lost: retry
    assert lost["replayed"] and lost["result"] == first["result"]
    branches = lab.q(lab.admin, "list_records", {"kind": "branch"}, project=pid)
    branches = branches.get("records") if isinstance(branches, dict) else branches
    assert [b["id"] for b in branches] == [first["result"]["branch_id"]]
    tid = first["result"]["trial_id"]
    l1 = lab.cmd(lab.admin, "launch_branch", {"trial_id": tid}, project=pid, key="launch-key-1")
    l2 = lab.cmd(lab.admin, "launch_branch", {"trial_id": tid}, project=pid, key="launch-key-1")
    assert l2["replayed"] and l2["result"]["job_id"] == l1["result"]["job_id"]
    with pytest.raises(Invalid):  # even a retry under a new key never creates a second child job
        lab.cmd(lab.admin, "launch_branch", {"trial_id": tid}, project=pid, key="launch-key-2")
    jobs = lab.q(lab.admin, "list_jobs", {}, project=pid)
    jobs = jobs.get("jobs") if isinstance(jobs, dict) else jobs
    assert [j["id"] for j in jobs if j.get("trial_id") == tid] == [l1["result"]["job_id"]]
