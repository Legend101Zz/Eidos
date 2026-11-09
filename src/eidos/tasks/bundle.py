"""Adapter for the public research task bundles (``eidos-task-bundle/0.1``, ``eidos-task/0.2``)
and the common answer record (``eidos-answer/0.2``).

Import treats the bundle as immutable experiment input: hashes are verified, the files are
registered as artifacts, the bundle directory is registered as the project environment root
(read-only use), and an exploration-only charter records that final study margins are not set
by the bundle. Nothing here encodes task IDs, reference methods or answers; the investigator
sees only the bundle content and the common contracts.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

BUNDLE_SCHEMA = "eidos-task-bundle/0.1"
ANSWER_SCHEMA = "eidos-answer/0.2"
ARRAY_FILES = {"train": "train.npz", "validation": "validation.npz"}


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_bundle(bundle: Path) -> dict[str, Any]:
    task = json.loads((bundle / "task.json").read_text())
    if task.get("schema_version") != BUNDLE_SCHEMA:
        raise ValueError(f"unsupported bundle schema {task.get('schema_version')!r}")
    problems = []
    for name, digest in (task.get("hashes") or {}).items():
        p = bundle / name
        if not p.is_file() or p.is_symlink():
            problems.append(f"missing {name}")
        elif sha256(p) != digest:
            problems.append(f"hash mismatch {name}")
    if problems:
        raise ValueError("bundle failed verification: " + "; ".join(problems))
    leaked = [p.name for p in bundle.iterdir() if p.name.startswith("test") and "label" in p.name]
    if leaked:
        raise ValueError(f"bundle unexpectedly contains label-like files {leaked}; stopping import")
    return task


def import_bundle(client: Any, project: str, bundle: Path, *, python: str, investigation_id: str | None = None,
                  key_prefix: str = "task") -> dict[str, Any]:
    bundle = Path(bundle).resolve()
    task = verify_bundle(bundle)
    tid = task["task_id"]
    arts: dict[str, str] = {}
    for port, fname in ARRAY_FILES.items():
        r = client.upload(project, bundle / fname, media_type="application/x-npz", schema="task-array/1", role="input",
                          name=f"{tid}/{fname}", key=f"{key_prefix}:{tid}:{port}:{sha256(bundle / fname)}")
        arts[port] = r["result"]["artifact_id"]
    r = client.upload(project, bundle / "task.json", media_type="application/json", schema="task-manifest/1",
                      role="input", name=f"{tid}/task.json", key=f"{key_prefix}:{tid}:task:{sha256(bundle / 'task.json')}")
    arts["task"] = r["result"]["artifact_id"]
    env_payload = _inspect_env(python, bundle)
    client.command("register_environment", {"name": "default", "python_executable": python,
                   "project_root": str(bundle), "notes": f"{BUNDLE_SCHEMA} {tid}; immutable task input", **env_payload},
                   project=project, key=f"{key_prefix}:{tid}:env:{env_payload['lock_digest']}")
    brief = (bundle / "brief.md").read_text()
    study = f"{tid.lower()}-dev"
    charter = {"study_id": study, "question": brief.strip()[:2000], "exploration_only": True,
               "resource_limits": task.get("resource_profile", {}),
               "notes": ("Public development task; the bundle does not fix a final practical margin or investigator "
                         f"budget (investigator_budget={task.get('investigator_budget')!r}). Exploration only; no "
                         "prospective success rule is claimed."),
               "known_exposures": [{"description": "public development task; references are public elsewhere",
                                    "ref": f"{tid}@{task.get('schema_version')}"}]}
    try:
        ch = client.command("commit_study_charter", {"charter": charter}, project=project,
                            key=f"{key_prefix}:{tid}:charter")["result"]
    except Exception as exc:  # already committed in this project
        if "already exists" not in str(exc):
            raise
        ch = {"charter_id": f"charter:{study}"}
    return {"task_id": tid, "task": task, "artifacts": arts, "charter_id": ch["charter_id"], "bundle": str(bundle),
            "bundle_hashes": task.get("hashes"), "environment": env_payload}


def _inspect_env(python: str, root: Path) -> dict[str, Any]:
    code = ("import json,platform\nfrom importlib import metadata\n"
            "inst={d.metadata['Name'].lower().replace('_','-'):d.version for d in metadata.distributions() if d.metadata['Name']}\n"
            "print(json.dumps({'python_version':platform.python_version(),'installed':inst,'platform':platform.platform()}))")
    r = subprocess.run(env=_job_env(), args=[python, "-c", code], capture_output=True, text=True, cwd=root, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"cannot inspect project interpreter: {r.stderr[-500:]}")
    info = json.loads(r.stdout)
    info["lock_digest"] = "sha256:" + hashlib.sha256(json.dumps(info["installed"], sort_keys=True).encode()).hexdigest()
    return info


# --------------------------------------------------------------------------------------------
# Answer export
# --------------------------------------------------------------------------------------------

def _job(client, project, jid):
    return client.query("get_job", {"job_id": jid}, project=project)


def _output(job, slot):
    for o in job["outputs"]:
        if o["slot"] == slot:
            return o
    raise KeyError(f"job {job['id']} has no output {slot!r}")


def _paired(base_rows: list[dict], cand_rows: list[dict]) -> dict[str, Any]:
    b = {int(r["id"]): int(r["correct"]) for r in base_rows}
    c = {int(r["id"]): int(r["correct"]) for r in cand_rows}
    ids = sorted(set(b) & set(c))
    diffs = [c[i] - b[i] for i in ids]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    se = (var / n) ** 0.5
    return {"n": n, "difference": mean, "ci95": [mean - 1.959964 * se, mean + 1.959964 * se],
            "method": "paired per-example difference, normal approximation over validation examples "
                      "(single training seed; ignores training variation)"}


def export_answer(client: Any, project: str, *, task_id: str, run_id: str, out: Path, python: str,
                  test_inputs: Path | None, candidate_job: str | None, baseline_score_job: str | None = None,
                  candidate_score_job: str | None = None, status: str, claim_type: str, claim: str, scope: str,
                  investigation_id: str | None = None, run_label: str, alternatives: list[str] | None = None,
                  unresolved: list[str] | None = None, next_test: str | None = None,
                  validator: Path | None = None, environment_lock: Path | None = None) -> dict[str, Any]:
    """Write an ``eidos-answer/0.2`` submission directory. Candidate inference on test inputs runs
    in a separate process with the project interpreter; the scorer is never Eidos."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    started = time.time()
    evidence, measurements, deviations = [], [], []
    candidate = None
    if candidate_job:
        job = _job(client, project, candidate_job)
        cdir = out / "candidate"
        cdir.mkdir()
        src_art = (job["lock"]["inputs"].get("source") or {}).get("artifact_id") or _output(job, "source_snapshot")["artifact_id"]
        client.download(project, src_art, cdir / "source-bundle.json")
        files = json.loads((cdir / "source-bundle.json").read_text())["files"]
        (cdir / "source-bundle.json").unlink()
        for rel, text in files.items():
            p = (cdir / rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, dict) and "base64" in text:
                import base64
                p.write_bytes(base64.b64decode(text["base64"]))
            else:
                p.write_text(text)
        model = next(o for o in job["outputs"] if o["role"] == "model-export")
        client.download(project, model["artifact_id"], cdir / "model.pt")
        if test_inputs is not None:
            r = subprocess.run(env=_job_env(), args=[python, str(cdir / "project.py"), "infer", "--model", str(cdir / "model.pt"),
                                "--inputs", str(test_inputs), "--out", str(cdir / "test_predictions.npz")],
                               capture_output=True, text=True, timeout=300, cwd=cdir)
            if r.returncode != 0:
                raise RuntimeError(f"candidate inference failed: {r.stderr[-1000:]}")
            deviations.append("candidate test inference ran as a same-user host subprocess (development profile), "
                              "not in a qualified sealed profile")
        val_in = (job["lock"]["inputs"].get("validation") or {}).get("artifact_id")
        val_pred = next((o["artifact_id"] for o in job["outputs"] if o["slot"] == "validation_predictions"), None)
        if val_in and val_pred:
            _reload_check(client, project, python, cdir, out, val_in, val_pred)
            evidence.append({"path": "reload_check.json", "sha256": sha256(out / "reload_check.json"),
                             "role": "export-reload", "supports": "exported model reloads in a fresh process with "
                             "identical validation predictions", "limitations": "same environment and host; CPU only"})
        lock = environment_lock or (Path(job["lock"]["environment"]["project_root"]) / "requirements.lock")
        lock_dst = out / "requirements.lock"
        lock_dst.write_bytes(Path(lock).read_bytes())
        cmd = list(job["lock"]["config"]["argv"])
        cmd = ["python" if i == 0 else a.replace("{work}", ".").replace("{out}", "out").replace("{seed}", str(job["lock"]["config"].get("seed")))
               for i, a in enumerate(cmd)]
        candidate = {"code_path": "candidate/project.py", "code_sha256": sha256(cdir / "project.py"),
                     "model_path": "candidate/model.pt", "model_sha256": sha256(cdir / "model.pt"),
                     "predictions_path": "candidate/test_predictions.npz",
                     "predictions_sha256": sha256(cdir / "test_predictions.npz") if (cdir / "test_predictions.npz").exists() else None,
                     "reproduce_command": cmd, "environment_lock": "requirements.lock",
                     "environment_lock_sha256": sha256(lock_dst), "parent_checkpoint": None,
                     "restore_class": "fresh_initialization"}
        rec = {"job_id": job["id"], "lock_digest": job["lock_digest"], "receipt": job["terminal"]["receipt"],
               "attempts": [{"id": a["id"], "epoch": a["epoch"], "status": a["status"]} for a in job["attempts"]]}
        (out / "training_receipt.json").write_text(json.dumps(rec, indent=2, sort_keys=True, default=str))
        evidence.append({"path": "training_receipt.json", "sha256": sha256(out / "training_receipt.json"),
                         "role": "training", "supports": "candidate provenance (Eidos job receipt)",
                         "limitations": "self-reported by the project command; one training seed"})
    if baseline_score_job and candidate_score_job:
        rows: dict[str, Any] = {}
        for name, jid in (("baseline", baseline_score_job), ("candidate", candidate_score_job)):
            j = _job(client, project, jid)
            tmp = out / f".{name}.jsonl"
            client.download(project, _output(j, "per_example")["artifact_id"], tmp)
            rows[name] = [json.loads(line) for line in tmp.read_text().splitlines() if line.strip()]
            rows[name + "_report"] = j["terminal"]["receipt"]
            tmp.unlink()
        paired = _paired(rows["baseline"], rows["candidate"])
        vr = {"split": "validation", "baseline_score_job": baseline_score_job, "candidate_score_job": candidate_score_job,
              "baseline": rows["baseline_report"]["metrics"], "candidate": rows["candidate_report"]["metrics"],
              "paired": paired}
        (out / "validation_receipt.json").write_text(json.dumps(vr, indent=2, sort_keys=True))
        evidence.append({"path": "validation_receipt.json", "sha256": sha256(out / "validation_receipt.json"),
                         "role": "validation", "supports": "candidate selection on the visible validation split",
                         "limitations": "validation data guided selection; not independent confirmation"})
        measurements.append({"metric": "accuracy", "direction": "higher_is_better", "split": "validation",
                             "denominator": paired["n"], "units": "fraction",
                             "baseline": rows["baseline_report"]["metrics"]["accuracy"],
                             "candidate": rows["candidate_report"]["metrics"]["accuracy"],
                             "uncertainty_method": paired["method"] + f"; 95% CI {paired['ci95']}",
                             "evidence_receipt": "validation_receipt.json"})
    cost = _cost(client, project, investigation_id)
    if cost["receipt"]:
        (out / "cost_receipt.json").write_text(json.dumps(cost["receipt"], indent=2, sort_keys=True))
        cost["record"]["receipt_paths"] = ["cost_receipt.json"]
    abst = status == "abstained"
    answer = {
        "schema_version": ANSWER_SCHEMA, "task_id": task_id, "run_id": run_id, "status": status,
        "claim_type": claim_type, "claim": claim, "scope": scope,
        "candidate": None if abst else candidate, "measurements": measurements, "evidence": evidence,
        "alternative_explanations": alternatives or ["training-seed variation", "selection on validation data"],
        "abstention": {"abstained": abst, "reason": claim if abst else None,
                       "unresolved": unresolved or ["independent confirmation on held-out data with fresh seeds"],
                       "next_discriminating_test": next_test or "paired fresh-seed training runs scored by the independent evaluator"},
        "cost": cost["record"],
        "exposures": ["public development task", run_label],
        "deviations": deviations + ["hosted-Linux development run; this execution profile is unqualified by the research release"],
        "confidence": None,
    }
    (out / "answer.json").write_text(json.dumps(answer, indent=2, sort_keys=True))
    result = {"answer": str(out / "answer.json"), "seconds": round(time.time() - started, 2)}
    if validator is not None:
        r = subprocess.run(env=_job_env(), args=[python, str(validator), str(out / "answer.json")], capture_output=True, text=True, timeout=120)
        result["validator"] = {"returncode": r.returncode, "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:]}
    return result


def _reload_check(client, project, python, cdir: Path, out: Path, val_in: str, val_pred: str) -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        client.download(project, val_in, d / "validation.npz")
        client.download(project, val_pred, d / "recorded.npz")
        r = subprocess.run(env=_job_env(), args=[python, str(cdir / "project.py"), "infer", "--model", str(cdir / "model.pt"),
                            "--inputs", str(d / "validation.npz"), "--out", str(d / "fresh.npz")],
                           capture_output=True, text=True, timeout=300, cwd=cdir)
        cmp = subprocess.run(env=_job_env(), args=[python, "-c", (
            "import numpy as np,json,sys\n"
            "a=np.load(sys.argv[1],allow_pickle=False);b=np.load(sys.argv[2],allow_pickle=False)\n"
            "ia={int(i):int(p) for i,p in zip(a['ids'],a['predictions'])};ib={int(i):int(p) for i,p in zip(b['ids'],b['predictions'])}\n"
            "print(json.dumps({'ids_equal':set(ia)==set(ib),'n':len(ia),'mismatches':sum(ia[k]!=ib.get(k) for k in ia)}))"),
            str(d / "recorded.npz"), str(d / "fresh.npz")], capture_output=True, text=True, timeout=120) if r.returncode == 0 else r
        result = json.loads(cmp.stdout) if r.returncode == 0 and cmp.returncode == 0 else {"error": (r.stderr or cmp.stderr)[-1000:]}
    result["behavior_equal"] = bool(result.get("ids_equal") and result.get("mismatches") == 0)
    result["method"] = "fresh-process `project.py infer` on validation inputs vs predictions recorded by the training job"
    (out / "reload_check.json").write_text(json.dumps(result, indent=2, sort_keys=True))


def _cost(client, project, investigation_id) -> dict[str, Any]:
    fields = ["elapsed_seconds", "cpu_core_seconds", "gpu_device_seconds", "gpu_type", "model_input_tokens",
              "model_output_tokens", "model_reasoning_tokens", "model_calls", "billed_usd"]
    rec: dict[str, Any] = {f: None for f in fields}
    receipt = None
    if investigation_id:
        inv = client.query("get_investigation", {"investigation_id": investigation_id}, project=project)
        b = inv["budgets"]["exploration"]
        receipt = {"budgets": inv["budgets"], "jobs": [j["id"] for j in inv["jobs"]], "turns": len(inv["turns"])}
        if "compute_seconds" in b:
            # Wall-clock seconds of single-threaded jobs; recorded as elapsed job time, not CPU core-seconds.
            rec["elapsed_seconds"] = round(b["compute_seconds"]["used"], 3)
        if "model_tokens" in b:
            receipt["model_tokens_note"] = "fixture runtime usage is synthetic (character-count based), not billed"
        rec["model_calls"] = None
    rec["unknown_fields"] = [f for f in fields if rec[f] is None]
    rec["receipt_paths"] = []
    return {"record": rec, "receipt": receipt}


def _job_env() -> dict:
    """Project/candidate code never inherits provider keys or lab secrets from the operator's shell."""
    import os

    from eidos.runtime.launcher import job_environment
    return job_environment(os.environ)
