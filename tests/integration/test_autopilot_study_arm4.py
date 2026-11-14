"""Model-free real-ledger arm-4 export and public grading rules.

Temporal CLI is unavailable on the build host: activities are driven in-process over
PostgreSQL with the real runner. smoke_autopilot.sh covers the installed full path.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment
from test_command_path import _publish
from test_diagnose_and_template_sweep_lab import NUMPY_PY, ROOT, _bundle
from test_prepare_candidate_lab import client_view, run_queued
from test_recipe_portfolio_lab import scene  # noqa: F401 - shared real-lab fixture
from test_turn_recovery import ShimClient

from eidos.cli.main import bundle_dir
from eidos.domain.promotion import suggested_lock
from eidos.investigator.activities import InvestigatorActivities

pytestmark = [pytest.mark.pg, pytest.mark.skipif(not NUMPY_PY, reason="set EIDOS_NUMPY_PYTHON")]

BASELINE = '''
import argparse, json
from pathlib import Path
import numpy as np
p=argparse.ArgumentParser()
p.add_argument("command")
p.add_argument("--inputs",type=Path)
p.add_argument("--out",type=Path)
p.add_argument("--model",type=Path)
p.add_argument("--seed")
a=p.parse_args()
kind = KIND
if a.command == "train":
    a.out.mkdir(parents=True,exist_ok=True)
    with np.load(a.inputs/"train.npz") as t:
        y=t["y"]
        value=float(np.bincount(y.astype(int)).argmax()) if kind=="classification" else float(y.mean())
    (a.out/"model.pt").write_text(json.dumps(value))
    with np.load(a.inputs/"validation.npz") as v:
        pred=np.full(len(v["ids"]),value)
        metric="validation_accuracy" if kind=="classification" else "validation_rmse"
        score=float(np.mean(pred==v["y"])) if kind=="classification" else float(np.sqrt(np.mean((pred-v["y"])**2)))
        np.savez(a.out/"validation_predictions.npz",ids=v["ids"],predictions=pred)
    (a.out/"receipt.json").write_text(json.dumps({metric:score}))
else:
    with np.load(a.inputs) as v:
        np.savez(a.out,ids=v["ids"],predictions=np.full(len(v["ids"]),json.loads(a.model.read_text())))
'''

GENERATE = '''
import numpy as np,sys
from pathlib import Path
root,kind,signal=Path(sys.argv[1]),sys.argv[2],sys.argv[3]=="True"
rng=np.random.default_rng(734)
for split,n in [("train",240),("validation",180),("test",180)]:
    x=rng.normal(size=(n,6))
    if kind=="classification":
        y=(4*x[:,0]+x[:,1]>0).astype(int) if signal else rng.integers(0,2,n)
    else:
        y=4*x[:,0]+x[:,1]+rng.normal(scale=.1,size=n) if signal else rng.normal(size=n)
    np.savez(root/(split+".npz"),x=x,y=y,ids=np.arange(n))
'''


def drive(lab_scene, inv, client):
    acts = InvestigatorActivities(client, client)
    acts.batch_wait_slice, acts.batch_poll = .01, .001
    env, decisions = ActivityEnvironment(), []
    for index in range(1, 6):
        base = dict(project_id=lab_scene["pid"], investigation_id=inv, index=index)
        base["turn_id"] = env.run(acts.begin_turn, base)["turn_id"]
        for slot in range(2):  # the lock and its conclusion may share a turn
            env.run(acts.build_context, base)
            accepted = env.run(acts.decide, dict(base, slot=slot))
            assert accepted["accepted"], accepted
            turn = client.query("get_turn", dict(turn_id=base["turn_id"]), project=lab_scene["pid"])
            body = next(d["body"] for d in turn["decisions"] if d["id"] == accepted["decision_id"])
            decisions.append(body)
            for continuation in range(12):
                result = env.run(acts.execute_decision, dict(base, decision_id=accepted["decision_id"], continuation=continuation))
                run_queued(lab_scene, inv)
                if not result.get("pending"):
                    assert all(r["ok"] for r in result["summary"]), result
                    break
            else:
                pytest.fail("mechanical batch did not finish")
            control = body.get("control") or {}
            if control.get("action"):
                break
        concluded = control.get("action") == "conclude"
        env.run(acts.complete_turn, dict(base, stop_reason="concluded" if concluded else "waiting", control=control))
        if concluded:
            return decisions
    pytest.fail("autopilot exceeded its bounded sequence")


def grade_export(root, kind, signal, study_arm4):
    """Use the same pure success rule, artifact checks and fresh recipe contract as grade_block."""
    from study.grade_block import success_rule, verify_artifacts

    assert study_arm4.validate(root / "out/answer.json")
    answer = json.loads((root / "out/answer.json").read_text())
    verify_artifacts(root / "out", answer)
    immutable_inputs = {p: study_arm4.digest(root / (p + ".npz")) for p in ("train", "validation", "test")}
    gain, fresh_equal, seed_gains = None, True, []
    candidate = answer["candidate"]
    if candidate:
        code = root / "out" / candidate["code_path"]
        argv = candidate["reproduce_command"]
        subprocess.run([NUMPY_PY, str(code), "infer", "--model", str(root / "out" / candidate["model_path"]),
                        "--inputs", str(root / "validation.npz"), "--out", str(root / "locked-inference.npz")],
                       check=True, timeout=120)
        for seed in (37, 53):
            out = root / ("fresh-" + str(seed))
            out.mkdir()
            replacements = {"python": NUMPY_PY, "candidate/source/project.py": str(code),
                            "{inputs}": str(root / "task"), "{out}": str(out), "{seed}": str(seed)}
            subprocess.run([replacements.get(arg, arg) for arg in argv], check=True, timeout=120)
            for split in ("validation", "test"):
                subprocess.run([NUMPY_PY, str(code), "infer", "--model", str(out / "model.pt"),
                                "--inputs", str(root / (split + ".npz")), "--out", str(out / (split + ".npz"))],
                               check=True, timeout=120)
            result = subprocess.check_output([NUMPY_PY, "-c", '''
import numpy as np,json,sys
from pathlib import Path
r,o=map(Path,sys.argv[1:3])
kind=sys.argv[3]
with np.load(r/"out/candidate/validation_predictions.npz") as a,np.load(r/"locked-inference.npz") as b:
    equal=bool(np.array_equal(a["ids"],b["ids"]) and np.allclose(a["predictions"],b["predictions"],rtol=1e-5,atol=1e-6))
with np.load(r/"test.npz") as t,np.load(r/"train.npz") as train,np.load(o/"test.npz") as p:
    truth=t["y"]; pred=p["predictions"]
    base=np.full(len(truth),np.bincount(train["y"].astype(int)).argmax() if kind=="classification" else train["y"].mean())
    gain=float(np.mean(pred==truth)-np.mean(base==truth)) if kind=="classification" else float(np.sqrt(np.mean((base-truth)**2))-np.sqrt(np.mean((pred-truth)**2)))
print(json.dumps(dict(equal=equal,gain=gain)))
''', str(root), str(out), kind], text=True)
            measured = json.loads(result)
            fresh_equal &= measured["equal"]
            seed_gains.append(measured["gain"])
        gain = min(seed_gains)
    card = dict(id="D02" if signal else "D12", min_reference_gain=.02 if kind == "classification" else .5)
    grade = success_rule(card, answer, numeric=dict(gain=gain) if gain is not None else None,
                         reproduces=fresh_equal and all(g >= card["min_reference_gain"] for g in seed_gains),
                         guardrails=all(study_arm4.digest(root / (p + ".npz")) == sha for p, sha in immutable_inputs.items()),
                         scope_ok=answer["claim_type"] == "recipe_improvement")
    return answer, dict(status="valid", fresh_process_equal=fresh_equal, fresh_gains=seed_gains, **grade)


@pytest.mark.parametrize("kind,signal", [("classification", True), ("regression", True),
                                       ("classification", False), ("regression", False)])
def test_autopilot_real_lab_export_and_honesty(scene, kind, signal, tmp_path, study_arm4, monkeypatch):  # noqa: F811
    from study.exp.treatment import resolve

    from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
    monkeypatch.setattr(PydanticAIRuntime, "decide", lambda *a, **k: pytest.fail("model runtime forbidden"))
    forbidden = []
    popen = subprocess.Popen
    def guarded_popen(args, *a, **kw):
        if isinstance(args, (list, tuple)) and (Path(str(args[0])).name == "security" or "study.eidos_arms.provider_bridge" in args):
            forbidden.append(args)
            raise AssertionError("autopilot attempted credentials or a provider bridge")
        return popen(args, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr("study.eidos_arms.provider_bridge.start", lambda *a, **k: pytest.fail("provider bridge started"))
    subprocess.run([NUMPY_PY, "-c", GENERATE, str(tmp_path), kind, str(signal)], check=True, timeout=120)
    (tmp_path / "project.py").write_text(BASELINE.replace("KIND", repr(kind)))
    task = tmp_path / "task"
    task.mkdir()
    (task / "requirements.lock").write_text("numpy\nscikit-learn\n")
    lab, pid = scene["lab"], scene["pid"]
    for split in ("train", "validation"):
        raw = (tmp_path / (split + ".npz")).read_bytes()
        (task / (split + ".npz")).write_bytes(raw)
        digest, size = lab.store.put_bytes(raw)
        scene["arts"][split] = lab.cmd(lab.admin, "register_artifact", dict(digest=digest, size=size, schema="task-array/1",
                    media_type="application/x-npz"), project=pid)["result"]["artifact_id"]
    name = "eidos.search.recipe-portfolio"
    _publish(lab, pid, *_bundle(name), bundle_bytes=bundle_dir(ROOT / "components" / name))
    command = ROOT / "study/eidos_arms/components/eidos.project.command@1.0.9"
    raw = bundle_dir(command)
    files = json.loads(raw)["files"]
    _publish(lab, pid, json.loads(files.pop("eidos-component.json")), files, bundle_bytes=raw)
    charter = lab.cmd(lab.admin, "commit_study_charter", dict(charter=dict(study_id=pid, question="fixture", exploration_only=True)),
                      project=pid)["result"]["charter_id"]
    treatment = resolve("autopilot-v2")
    assert treatment.investigator["runtime"] == "fixture"
    assert treatment.investigator["settings"]["policy"] == "autopilot-v2"
    config = dict(treatment.investigator, wall_seconds=1800, conclusion_scope_schema=study_arm4.CONCLUSION_SCOPE_SCHEMA)
    inv = lab.cmd(lab.admin, "start_investigation", dict(question="model-free public fabricated card", charter_id=charter,
                  environment="numpy", inputs=scene["arts"], task=dict(task_type=kind, practical_bar=.02 if kind == "classification" else .5),
                  limits=dict(model_tokens=1000000, trials=1000, compute_seconds=100000), investigator=config), project=pid)["result"]["investigation_id"]
    client = ShimClient(lab, lab.admin)
    decisions = drive(scene, inv, client)
    view = client_view(scene, inv)
    shown, _, records = study_arm4.ledger(client, pid, inv, tmp_path / "transcript.jsonl")
    selected, reason = study_arm4.select_job(client, pid, shown, records)
    conclusion = shown["body"]["conclusion"]
    tokens = dict(input=0, output=0, cache_read=0, cache_write=0)
    study_arm4.export_answer(client, pid, inv, tmp_path, dict(card="D02" if signal else "D12"), selected, reason,
                            conclusion["summary"], [], tokens, [], answer_metadata=conclusion["scope"]["answer"],
                            claim_state=conclusion["claim_state"], task_artifacts=scene["arts"])
    answer, grade = grade_export(tmp_path, kind, signal, study_arm4)
    assert grade["fresh_process_equal"], grade
    locks = [n for n in view["pinned_notes"] if n["kind"] == "candidate_lock"]
    assert shown["status"] == "concluded"
    run_status, incident = study_arm4.classify(shown, [], ["fixture:autopilot-v2"], answer_valid=True)
    assert run_status == "ok" and incident is None
    assert bool(locks) is signal
    assert (answer["status"] == "improvement_claimed") is signal
    assert grade["status"] == "valid" and grade["verified_success"] is signal, grade
    assert grade["false_positive_claim"] is False
    assert not forbidden
    assert all(d["usage"]["fixture"] for d in decisions)
    record = dict(kind=kind, signal=signal, run_status=run_status, decisions=[
        dict(proposed_calls=d.get("proposed_calls", d["calls"]), calls=d["calls"], control=d.get("control"), usage=d["usage"]) for d in decisions],
        lock_text_source="json.dumps(domain.promotion.suggested_lock(view)['lock'], sort_keys=True)",
        prepared=suggested_lock(view), locks=locks, conclusion=conclusion,
        exported_answer_status=answer["status"], grade=grade, provider_calls=0, keychain_reads=0)
    if os.environ.get("EIDOS_AUTOPILOT_TRACE"):
        dest = Path(os.environ["EIDOS_AUTOPILOT_TRACE"])
        dest.mkdir(exist_ok=True, parents=True)
        (dest / f"{kind}-{'signal' if signal else 'noise'}.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
