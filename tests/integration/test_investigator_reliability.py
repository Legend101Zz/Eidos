"""ADR 0020 end to end against the real command service and PostgreSQL: admission preflight against measured
environment capabilities, what the investigator's context and job views show of them, and the wall-aware
conclusion (turn spec, decision acceptance, admission). Fixture investigator policy; no model is involved."""

from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment
from test_command_path import _publish
from test_turn_recovery import setup as _setup_turn_lab

from eidos.domain.errors import ConcludeRequired, PreflightRefused
from eidos.investigator.context import pack

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

DIGEST = "sha256:" + "a" * 64
NOTE = {"tool": "record_note", "args": {"kind": "hypothesis", "text": "a note"}}


# -- helpers ----------------------------------------------------------------------------------------------------------

def _caps(has_numpy: bool) -> dict:
    numpy = {"provided": True, "distribution": "numpy", "version": "1.26.4"} if has_numpy else {"provided": False}
    return {"schema": "environment-capabilities/2", "status": "ok", "interpreter_digest": DIGEST, "python_version": "3.12",
            "marker_environment": {"python_version": "3.12", "sys_platform": "linux"}, "import_names": {},
            "modules": {"numpy": numpy, "torch": {"provided": True, "distribution": "torch", "version": "2.1"}}}


def _register(lab, pid: str, name: str, **extra) -> None:
    modules = (extra.get("capabilities") or {}).get("modules") or {}
    installed = {m["distribution"]: m["version"] for m in modules.values() if m.get("provided")}
    lab.cmd(lab.admin, "register_environment", {"name": name, "python_executable": sys.executable, "project_root": "/tmp",
                                                "python_version": "3.12", "installed": installed, **extra}, project=pid)


def _component(lab, pid: str, cid: str, *, dependencies: list[str] | None = None, implements: str | None = None) -> str:
    man = {"id": cid, "release": "1.0.0", "implements": implements or f"{cid.split('.')[-1]}/1", "kind": "artifact-op",
           "entry": "m:run", "scientific_effects": ["observe"], "output": {"report": "reliability-report/1"},
           **({"dependencies": dependencies} if dependencies else {})}
    return _publish(lab, pid, man, {"m.py": "def run(i, c):\n    return {}\n"})["component_ref"]


@pytest.fixture()
def world(lab):
    """The turn-recovery lab (project, charter, investigation, activities, environment ``lab`` without capabilities)
    plus a probed environment lacking numpy (``nonum``), one with it (``hasnum``) and a component that needs numpy."""
    pid, inv, acts = _setup_turn_lab(lab)
    _register(lab, pid, "nonum", capabilities=_caps(False))
    _register(lab, pid, "hasnum", capabilities=_caps(True))
    needs = _component(lab, pid, "test.needs-numpy", dependencies=["numpy"])
    return SimpleNamespace(pid=pid, inv=inv, acts=acts, needs=needs, wf=lab.principal(f"wf-{pid}", ["workflow"], [pid]))


def _admit(lab, w, ref: str, environment: str, **extra) -> dict:
    return lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": environment,
                                                "investigation_id": extra.pop("investigation_id", w.inv), **extra},
                   project=w.pid)["result"]


def _jobs(lab, w, inv: str | None = None) -> list[dict]:
    return lab.q(lab.admin, "list_jobs", {"investigation_id": inv or w.inv}, project=w.pid)


def _view(lab, pid: str, inv: str) -> dict:
    h = lab.q(lab.admin, "project_summary", {}, project=pid)["horizon"]
    return lab.q(lab.admin, "investigation_view", {"investigation_id": inv, "horizon": h}, project=pid)


def _artifact_text(lab, pid: str, artifact_id: str) -> str:
    art = lab.q(lab.admin, "get_artifact", {"artifact_id": artifact_id}, project=pid)
    with open(lab.store.path_for(art["digest"]), encoding="utf-8") as fh:
        return fh.read()


def _start(lab, w, **investigator) -> str:
    return lab.cmd(lab.admin, "start_investigation", {
        "question": "q", "charter_id": f"charter:s{w.pid}",
        "limits": {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100},
        "investigator": {"runtime": "fixture", "settings": {"policy": "test-run-echo-then-wait"}, **investigator}},
        project=w.pid)["result"]["investigation_id"]


def _begin(w, inv: str, index: int = 1) -> dict:
    return ActivityEnvironment().run(w.acts.begin_turn, {"investigation_id": inv, "project_id": w.pid, "index": index})


def _set_deadline(lab, pid: str, inv: str, deadline_unix: float) -> None:
    """Move an investigation's wall deadline (the only way to be inside the window without waiting for it)."""
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        rec = tx.require("investigation", inv, lock=True)
        tx.put_record("investigation", inv, pid, {**rec.body, "wall": {**rec.body["wall"], "deadline_unix": deadline_unix}},
                      created_by=lab.admin.id, status=rec.status)


def _propose(lab, w, turn_id: str, slot: int, control: dict, calls: list | None = None) -> dict:
    return lab.cmd(w.wf, "accept_decision", {"turn_id": turn_id, "slot": slot, "proposal": {
        "rationale": "r", "calls": calls or [], "control": control}}, project=w.pid)["result"]


# -- 1. preflight rejection -------------------------------------------------------------------------------------------

def test_preflight_refuses_a_component_whose_dependency_the_environment_lacks_and_records_why(lab, world):
    w = world
    with pytest.raises(PreflightRefused) as caught:
        _admit(lab, w, w.needs, "nonum")
    exc = caught.value
    assert exc.code == "ENVIRONMENT_PREFLIGHT_REFUSED" and exc.to_dict()["code"] == "ENVIRONMENT_PREFLIGHT_REFUSED"
    assert "numpy" in exc.message and "'nonum'" in exc.message and w.needs in exc.message
    d = exc.details
    assert d["component"] == w.needs and d["environment"] == "nonum" and d["investigation_id"] == w.inv
    assert d["interpreter_digest"] == DIGEST
    assert [(x["owner"], x["package"], x["problem"]) for x in d["problems"]] == [(w.needs, "numpy", "not-installed")]
    assert {"lab", "nonum", "hasnum"} <= set(d["available_environments"])

    # The command rolled back: no job, no reservation, no trial...
    assert _jobs(lab, w) == []
    assert lab.q(lab.admin, "budgets", {"investigation_id": w.inv}, project=w.pid)["budgets"]["trials"]["reserved"] == 0
    # ... but the refusal is in the ledger for this investigation.
    events = lab.q(lab.admin, "list_events", {"investigation_id": w.inv, "types": ["admission.refused"]},
                   project=w.pid)["events"]
    assert len(events) == 1 and events[0]["investigation_id"] == w.inv and events[0]["subject"] == w.needs
    body = events[0]["body"]
    assert body["command"] == "run_component" and body["environment"] == "nonum" and body["by"] == lab.admin.id
    assert "numpy" in body["reason"] and [p["package"] for p in body["problems"]] == ["numpy"]
    # The next view and its packed context carry it as a known failure.
    view = _view(lab, w.pid, w.inv)
    assert [(r["component"], r["environment"]) for r in view["admission_refusals"]] == [(w.needs, "nonum")]
    assert f"{w.needs} refused 1× before launch in environment nonum: missing or incompatible numpy" \
        in pack(view, [])["text"]


def test_preflight_never_refuses_without_a_measurement_or_where_the_module_imports(lab, world):
    w = world
    unprobed = _admit(lab, w, w.needs, "lab")      # environment registered without capabilities
    probed = _admit(lab, w, w.needs, "hasnum")     # numpy in the measured inventory
    assert unprobed["job_id"] != probed["job_id"]
    assert len(_jobs(lab, w)) == 2
    _register(lab, w.pid, "failed-probe", capabilities={"schema": "environment-capabilities/2", "status": "failed",
                                                        "error": "probe timed out"})
    assert _admit(lab, w, w.needs, "failed-probe")["job_id"]   # a failed probe is not evidence of absence either
    assert lab.q(lab.admin, "list_events", {"investigation_id": w.inv, "types": ["admission.refused"]},
                 project=w.pid)["events"] == []


def test_what_preflight_cannot_decide_is_admitted_and_recorded_in_the_lock(lab, world):
    w = world
    _register(lab, w.pid, "undecided", capabilities=_caps(True),
              requirements=["foo @ https://example.com/foo.whl", "-r missing.txt"])
    proj = _component(lab, w.pid, "test.project-undecided", implements="project-command/1")
    job = _admit(lab, w, proj, "undecided")["job_id"]
    lock = lab.q(lab.admin, "get_job", {"job_id": job, "include_lock": True}, project=w.pid)["lock"]
    assert {(x["requirement"], x["inconclusive"]) for x in lock["preflight_inconclusive"]} == {
        ("foo @ https://example.com/foo.whl", "url-requirement"), ("-r missing.txt", "not-a-parsed-requirement")}


# -- 2. project-declared requirements ---------------------------------------------------------------------------------

def test_environment_requirements_apply_to_project_command_components_only(lab, world):
    w = world
    _register(lab, w.pid, "projreq", capabilities=_caps(False), requirements=["numpy"])
    proj = _component(lab, w.pid, "test.project-cmd", implements="project-command/1")
    with pytest.raises(PreflightRefused) as caught:
        _admit(lab, w, proj, "projreq")
    d = caught.value.details
    assert "numpy" in caught.value.message and "'projreq'" in caught.value.message
    assert [(x["owner"], x["package"], x["problem"]) for x in d["problems"]] == [("the project", "numpy", "not-installed")]
    assert _jobs(lab, w) == []
    # An ordinary component declaring nothing is not refused for the project's requirements, in the same environment.
    assert _admit(lab, w, "test.echo@1.0.0", "projreq")["job_id"]
    # Neither is the project component where the requirement is measured satisfied, or where nothing was measured.
    _register(lab, w.pid, "projok", capabilities=_caps(True), requirements=["numpy>=1.0"])
    _register(lab, w.pid, "projunprobed", requirements=["numpy"])
    assert _admit(lab, w, proj, "projok")["job_id"] and _admit(lab, w, proj, "projunprobed")["job_id"]


def test_requirement_with_a_version_the_probe_does_not_meet_is_refused_with_the_version_found(lab, world):
    w = world
    _register(lab, w.pid, "projnew", capabilities=_caps(True), requirements=["numpy>=2.0"])
    proj = _component(lab, w.pid, "test.project-cmd", implements="project-command/1")
    with pytest.raises(PreflightRefused) as caught:
        _admit(lab, w, proj, "projnew")
    assert "numpy>=2.0 (found 1.26.4)" in caught.value.message
    assert caught.value.details["problems"][0]["problem"] == "version-mismatch"


# -- 3. capability context --------------------------------------------------------------------------------------------

def test_view_and_packed_context_state_what_each_environment_can_import(lab, world):
    w = world
    view = _view(lab, w.pid, w.inv)
    assert view["environments"] == ["hasnum", "lab", "nonum"]
    det = view["environment_details"]
    assert det["nonum"]["probed"] is True and det["nonum"]["interpreter_digest"] == DIGEST
    assert det["nonum"]["python_version"] == "3.12"
    assert det["nonum"]["modules"] == {"numpy": None, "torch": "2.1"}
    assert det["hasnum"]["modules"] == {"numpy": "1.26.4", "torch": "2.1"}
    assert det["lab"]["probed"] is False and "modules" not in det["lab"]

    def line(text: str, name: str) -> str:
        return next(x for x in text.splitlines() if x.startswith(f"- Environment {name}:"))
    text = pack(view, [])["text"]
    assert "numpy MISSING" in line(text, "nonum") and "torch 2.1" in line(text, "nonum")
    assert "numpy 1.26.4" in line(text, "hasnum") and "MISSING" not in line(text, "hasnum")
    assert "capabilities not probed" in line(text, "lab")

    # The same facts reach the model through the turn's recorded context (build_context activity).
    env = ActivityEnvironment()
    b = _begin(w, w.inv)
    ctx = env.run(w.acts.build_context, {"turn_id": b["turn_id"], "project_id": w.pid, "investigation_id": w.inv})
    assert "numpy MISSING" in line(_artifact_text(lab, w.pid, ctx["context_artifact_id"]), "nonum")


# -- 4. job-environment view ------------------------------------------------------------------------------------------

def test_admitted_jobs_carry_their_environment_in_the_view_get_job_and_list_jobs(lab, world):
    w = world
    probed = _admit(lab, w, w.needs, "hasnum")["job_id"]
    unprobed = _admit(lab, w, "test.echo@1.0.0", "lab")["job_id"]
    by_id = {j["id"]: j for j in _view(lab, w.pid, w.inv)["jobs"]}
    assert by_id[probed]["environment"] == "hasnum" and by_id[unprobed]["environment"] == "lab"
    assert {j["id"]: j["environment"] for j in _jobs(lab, w)} == {probed: "hasnum", unprobed: "lab"}

    job = lab.q(lab.admin, "get_job", {"job_id": probed, "include_lock": False}, project=w.pid)
    assert "lock" not in job
    assert job["environment"] == {"name": "hasnum", "id": f"env:{w.pid}:hasnum", "revision": job["environment"]["revision"],
                                  "interpreter_digest": DIGEST}
    assert isinstance(job["environment"]["revision"], int)
    plain = lab.q(lab.admin, "get_job", {"job_id": unprobed, "include_lock": False}, project=w.pid)
    assert plain["environment"]["name"] == "lab" and plain["environment"]["interpreter_digest"] is None
    with_lock = lab.q(lab.admin, "get_job", {"job_id": probed}, project=w.pid)
    assert with_lock["lock"]["environment"]["capabilities"]["interpreter_digest"] == DIGEST
    assert with_lock["environment"]["name"] == "hasnum"


# -- 5. stderr tail end to end ----------------------------------------------------------------------------------------

def test_failed_job_error_shows_exception_and_stderr_tail_redacted_and_bounded(lab, world):
    w = world
    job = _admit(lab, w, w.needs, "hasnum")["job_id"]
    launcher = lab.principal(f"L-{w.pid}", ["launcher"], [w.pid])
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=w.pid)["result"]
    stderr = ("HEAD-MARKER\n" + "noise line\n" * 400
              + "Traceback (most recent call last):\n  File \"project.py\", line 1, in <module>\n    import numpy\n"
              "ModuleNotFoundError: No module named 'numpy'\nsetting token=hunter2secret\n"
              '{"password": "json-sensitive-value"}\n')
    lab.cmd(launcher, "finish_attempt", {
        "job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"], "status": "failed", "final": True,
        "error": {"category": "user-code", "type": "builtins.RuntimeError",
                  "message": "project command exited with status 1", "stderr_tail": stderr}}, project=w.pid)

    view = _view(lab, w.pid, w.inv)
    vj = next(j for j in view["jobs"] if j["id"] == job)
    assert vj["status"] == "failed" and vj["error"]["type"] == "builtins.RuntimeError"
    assert "hunter2secret" not in vj["error"]["stderr_tail"]  # stored redacted, whichever path reported it
    assert "token=[redacted]" in vj["error"]["stderr_tail"] and "HEAD-MARKER" in vj["error"]["stderr_tail"]
    text = pack(view, [])["text"]
    line = next(json.loads(x) for x in text.splitlines() if x.startswith("{") and json.loads(x).get("id") == job)
    err = line["error"]
    assert err["category"] == "user-code" and err["message"] == "project command exited with status 1"
    assert err["exception"] == "ModuleNotFoundError"  # the innermost exception, not the wrapper's type
    assert "ModuleNotFoundError: No module named 'numpy'" in err["stderr_tail"]
    assert "HEAD-MARKER" not in err["stderr_tail"] and len(err["stderr_tail"]) <= 800  # the tail, bounded
    assert "hunter2secret" not in text and "token=[redacted]" in err["stderr_tail"]
    assert "json-sensitive-value" not in text and "json-sensitive-value" not in json.dumps(vj)
    assert f"- {w.needs} failed 1× in environment hasnum with ModuleNotFoundError numpy (last: {job})" in text


# -- 6. deadline-aware conclusion -------------------------------------------------------------------------------------

def test_wall_limit_is_recorded_and_the_turn_spec_keeps_the_conclusion_reserve(lab, world):
    w = world
    assert "wall" not in lab.q(lab.admin, "get_record", {"kind": "investigation", "id": w.inv}, project=w.pid)["body"]
    t0 = time.time()
    inv = _start(lab, w, wall_seconds=1000, turn_deadline_seconds=5000)
    wall = lab.q(lab.admin, "get_record", {"kind": "investigation", "id": inv}, project=w.pid)["body"]["wall"]
    assert wall["wall_seconds"] == 1000 and wall["conclude_threshold_seconds"] == 100  # 10% of the wall
    assert t0 + 1000 <= wall["deadline_unix"] <= time.time() + 1000

    b = _begin(w, inv)
    spec = lab.q(lab.admin, "get_turn", {"turn_id": b["turn_id"]}, project=w.pid)["spec"]
    c = spec["completion"]
    assert c["conclude_now"] is False and c["conclude_threshold_seconds"] == 100
    assert c["deadline_unix"] == wall["deadline_unix"] and 990 < c["remaining_seconds"] <= 1000
    assert b["completion"] == c
    # An earlier turn stops where the reserve begins: 5000 s were configured, about 900 s remain before it.
    assert 890 <= spec["limits"]["deadline_seconds"] <= 900 and b["model_timeout"] == spec["limits"]["deadline_seconds"]
    # Outside the window a decision that does not conclude is fine.
    assert _propose(lab, w, b["turn_id"], 0, {}, [NOTE])["accepted"]


def test_conclude_window_turn_is_told_and_may_only_conclude(lab, world):
    w = world
    inv = _start(lab, w, wall_seconds=30, conclude_threshold_seconds=120)
    b = _begin(w, inv)
    assert b["started"] and b["completion"]["conclude_now"] is True and b["completion"]["remaining_seconds"] <= 30
    spec = lab.q(lab.admin, "get_turn", {"turn_id": b["turn_id"]}, project=w.pid)["spec"]
    assert 10 <= spec["limits"]["deadline_seconds"] <= 30  # the concluding turn may use what is left
    ctx = ActivityEnvironment().run(w.acts.build_context, {"turn_id": b["turn_id"], "project_id": w.pid,
                                                           "investigation_id": inv})
    text = _artifact_text(lab, w.pid, ctx["context_artifact_id"])
    assert "CONCLUDE NOW" in text and "- Wall time:" not in text

    # No conclusion (nothing to wait for, so the wait itself is refused first), a wait, a request for input, and
    # effects without a conclusion are all refused as invalid proposals; the last two say why.
    for slot, (control, calls) in enumerate(((None, []), ({"action": "wait_for_jobs"}, []), ({"action": "request_input",
                                              "summary": "?"}, []), ({}, [NOTE])), 0):
        r = _propose(lab, w, b["turn_id"], slot, control or {}, calls)
        assert not r["accepted"] and r["reason"] == "invalid", (control, calls, r)
        if slot >= 2:
            assert "must conclude" in r["error"]["message"] and r["error"]["details"]["remaining_seconds"] <= 30
    done = _propose(lab, w, b["turn_id"], 9, {"action": "conclude", "summary": "best scoped result",
                                              "scope": {"what": "nothing was run"}})
    assert done["accepted"] and done["control"]["action"] == "conclude"


def test_begin_turn_refuses_once_the_wall_deadline_has_passed(lab, world):
    w = world
    inv = _start(lab, w, wall_seconds=1000)
    _set_deadline(lab, w.pid, inv, time.time() - 5)
    assert _begin(w, inv) == {"started": False, "reason": "wall-deadline-reached"}
    r = lab.cmd(w.wf, "begin_turn", {"investigation_id": inv}, project=w.pid)["result"]
    assert r == {"started": False, "reason": "wall-deadline-reached"}
    assert lab.q(lab.admin, "get_record", {"kind": "investigation", "id": inv}, project=w.pid)["status"] == "active"


def test_compute_inside_the_conclude_window_is_refused_and_not_admitted(lab, world):
    w = world
    inv = _start(lab, w, wall_seconds=30, conclude_threshold_seconds=120)
    with pytest.raises(ConcludeRequired) as caught:
        _admit(lab, w, w.needs, "hasnum", investigation_id=inv)
    assert caught.value.code == "CONCLUDE_REQUIRED"
    assert caught.value.details["conclude_threshold_seconds"] == 120 and caught.value.details["remaining_seconds"] <= 30
    assert "conclusion" in caught.value.message and "Conclude" in caught.value.next_action
    assert _jobs(lab, w, inv) == []
    assert lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=w.pid)["budgets"]["compute_seconds"]["reserved"] == 0
    # Work outside the investigation is not subject to its wall.
    assert lab.cmd(lab.admin, "run_component", {"component_ref": w.needs, "environment": "hasnum"},
                   project=w.pid)["result"]["job_id"]


def test_wall_seconds_of_a_job_is_reduced_to_end_before_the_reserve(lab, world):
    w = world
    inv = _start(lab, w, wall_seconds=1000)  # threshold 100: about 900 s may still be used for compute
    long = _admit(lab, w, w.needs, "hasnum", investigation_id=inv, wall_seconds=5000)["job_id"]
    short = _admit(lab, w, w.needs, "hasnum", investigation_id=inv, wall_seconds=60)["job_id"]
    got = {j: lab.q(lab.admin, "get_job", {"job_id": j}, project=w.pid)["lock"]["limits"]["wall_seconds"]
           for j in (long, short)}
    assert 880 <= got[long] <= 900 and got[short] == 60
    reserved = lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=w.pid)["budgets"]["compute_seconds"]["reserved"]
    assert reserved == got[long] + got[short]  # the budget holds what the job may actually use
    # Without a wall limit the requested limit stands.
    free = _admit(lab, w, w.needs, "hasnum", wall_seconds=5000)["job_id"]
    assert lab.q(lab.admin, "get_job", {"job_id": free}, project=w.pid)["lock"]["limits"]["wall_seconds"] == 5000


def test_assessment_compute_is_exempt_from_the_reserve(lab, world):
    w = world
    inv = _start(lab, w, wall_seconds=30, conclude_threshold_seconds=120)
    job = _admit(lab, w, w.needs, "hasnum", investigation_id=inv, purpose="assessment", wall_seconds=20)["job_id"]
    assert lab.q(lab.admin, "get_job", {"job_id": job}, project=w.pid)["lock"]["limits"]["wall_seconds"] == 20
