#!/usr/bin/env python
"""Reference smoke adapters for the 14 public capability cards (research-public-v0.1.0), driven through a
RUNNING Eidos lab.

Execution (training/inference commands, lifecycle workers, instrument) runs as ``eidos.project.command`` jobs in a
registered environment rooted at a COPY of research/public/development. Scoring with the release's grade.py,
fresh_check.py and the lifecycle equality checks are **reference-assisted builder checks**: they use labelled
development data and reference code, run in subprocesses outside any investigator input path, and write only
under the --out directory. Card-specific knowledge lives here, never in src/eidos or components/.

    EIDOS_HOME=<started lab> python scripts/research/cards.py --out OUT [--cards D01,D02] [--seeds 11,23,37]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))

from eidos.client import LabClient  # noqa: E402
from eidos.settings import LabConfig  # noqa: E402
from eidos.tasks.cards import card_charter, command_config, load_card  # noqa: E402

RESEARCH = Path(os.environ.get("EIDOS_RESEARCH_ROOT", "/workspace/.eidos-dev/h2/research"))
DEVCOPY = RESEARCH / "devcopy"
DEV_PY = os.environ.get("EIDOS_RESEARCH_PYTHON", "/workspace/.eidos-dev/lead/envs/research-dev/bin/python")
REGRESSION = {"conditioning", "forecast"}
CHEAP_SUBSET = ["D01", "D07", "D12", "D04"]
TERMINAL = ("succeeded", "failed", "cancelled", "pruned")
LABEL = "reference-assisted builder check"


def setup(project: str) -> None:
    """Copy the immutable release development directory (once) and prepare the lab project: environment rooted at the
    copy plus the three components. Idempotent; uses the `lab` CLI of the current venv."""
    if not DEVCOPY.exists():
        shutil.copytree(REPO / "research/public/development", DEVCOPY, ignore=shutil.ignore_patterns("__pycache__"))
    lab_cli = str(Path(sys.executable).parent / "lab")
    for args in (["project", "create", project, "--budget", "compute_seconds=100000", "--budget", "trials=2000"],
                 ["project", "register-env", project, "--python", DEV_PY, "--root", str(DEVCOPY)],
                 *(["component", "install", project, str(REPO / "components" / c)]
                   for c in ("eidos.project.command", "eidos.task.score-classification", "eidos.task.score-regression"))):
        r = subprocess.run([lab_cli, *args], capture_output=True, text=True, timeout=900)
        if r.returncode != 0 and "already" not in (r.stdout + r.stderr):
            raise RuntimeError(f"lab {' '.join(args[:2])} failed: {(r.stderr or r.stdout)[-500:]}")


def sh(args: list[str], cwd: Path = DEVCOPY, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def dev(*args: str) -> Any:
    r = sh([DEV_PY, str(HERE / "builder_checks.py"), *args])
    if r.returncode != 0:
        raise RuntimeError(f"builder check {args[0]} failed: {r.stderr[-800:]}")
    return json.loads(r.stdout)


class Lab:
    def __init__(self, project: str):
        cfg = LabConfig.load()
        self.client = LabClient(cfg.api_url, cfg.token("admin"), client_name="research-cards", timeout=120)
        self.project = project
        self.jobs: dict[str, dict] = {}
        self.refs = self._refs()

    def _refs(self) -> dict[str, str]:
        b = self.client.query("get_profile", {}, project=self.project)["body"]["bindings"]
        return {"command": b["project-command/1"][-1], "cls": b["classification-score/1"][-1],
                "reg": b["regression-score/1"][-1]}

    def upload(self, path: Path, schema: str, name: str) -> str:
        key = f"cards:{name}:{hashlib.sha256(Path(path).read_bytes()).hexdigest()}"
        return self.client.upload(self.project, path, schema=schema, name=name, key=key,
                                  media_type="application/octet-stream")["result"]["artifact_id"]

    def run(self, label: str, ref: str, inputs: dict, config: dict, wall: int = 600) -> dict:
        t0 = time.time()
        r = self.client.command("run_component", {"component_ref": ref, "inputs": inputs, "config": config,
                                                  "wall_seconds": wall, "environment": None},
                                project=self.project)["result"]
        while True:
            job = self.client.query("get_job", {"job_id": r["job_id"], "include_lock": False}, project=self.project)
            if job["status"] in TERMINAL:
                break
            time.sleep(0.4)
        term = job.get("terminal") or {}
        rec = {"label": label, "job_id": r["job_id"], "status": job["status"], "seconds": round(time.time() - t0, 2),
               "outputs": {o["slot"]: o["artifact_id"] for o in job.get("outputs", [])},
               "metrics": (term.get("receipt") or {}).get("metrics")}
        if job["status"] != "succeeded":
            rec["error"] = term.get("error")
        self.jobs[label] = rec
        return rec

    def ok(self, rec: dict) -> dict:
        if rec["status"] != "succeeded":
            raise RuntimeError(f"job {rec['label']} {rec['job_id']} {rec['status']}: {str(rec.get('error'))[:600]}")
        return rec

    def fetch(self, rec: dict, slot: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return self.client.download(self.project, rec["outputs"][slot], dest)


class Runner:
    def __init__(self, lab: Lab, out: Path, seeds: list[int]):
        self.lab, self.out, self.seeds = lab, out, seeds
        self.cards = {p.stem: load_card(p) for p in sorted((DEVCOPY / "tasks").glob("D*.json"))}
        self.cache: dict[tuple, dict] = {}
        self.labels: dict[tuple, str] = {}
        self.bundle_art: dict[str, tuple[str, str]] = {}

    # -- Eidos jobs --------------------------------------------------------------------------------
    def project_run(self, project: str, variant: str, seed: int) -> dict:
        key = (project, variant, seed)
        if key not in self.cache:
            cfg = command_config(["python", "project.py", "--project", project, "--variant", variant,
                                  "--seed", "{seed}", "--out", "{out}"], seed=seed)
            rec = self.lab.ok(self.lab.run(f"{project}/{variant}/{seed}", self.lab.refs["command"], {}, cfg))
            d = self.out / "artifacts" / project / variant / str(seed)
            for slot in ("model", "test_predictions", "validation_predictions", "train_predictions"):
                self.lab.fetch(rec, slot, d / {"model": "model.pt"}.get(slot, slot + ".npz"))
            rec["dir"] = str(d)
            self.cache[key] = rec
        return self.cache[key]

    def label_art(self, project: str) -> str:
        if (project, "validation") not in self.labels:
            f = self.out / "labels" / f"{project}-validation.npz"
            f.parent.mkdir(parents=True, exist_ok=True)
            dev("labels", project, "validation", str(f))
            self.labels[(project, "validation")] = self.lab.upload(f, "task-array/1", f"{project}/validation-labels.npz")
        return self.labels[(project, "validation")]

    def eidos_score(self, project: str, rec: dict) -> dict:
        reg = project in REGRESSION
        ref = self.lab.refs["reg" if reg else "cls"]
        s = self.lab.ok(self.lab.run(f"score:{rec['label']}", ref,
                        {"predictions": rec["outputs"]["validation_predictions"], "reference": self.label_art(project)},
                        {"split": "validation"}))
        return {"job_id": s["job_id"], "metrics": s["metrics"], "seconds": s["seconds"]}

    def source_bundle(self, adapter: str) -> str:
        if adapter not in self.bundle_art:
            files = {p.name: p.read_text() for p in DEVCOPY.glob("*.py")}
            files[adapter] = (HERE / "adapters" / adapter).read_text()
            f = self.out / "bundles" / f"{adapter}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"files": files}, sort_keys=True))
            self.bundle_art[adapter] = (self.lab.upload(f, "source-bundle/1", f"bundle/{adapter}"),
                                        hashlib.sha256(files["instrument.py"].encode()).hexdigest())
        return self.bundle_art[adapter][0]

    # -- reference-assisted builder checks ---------------------------------------------------------
    def grade(self, project: str, base: Path, cand: Path, split: str = "test") -> dict:
        r = sh([DEV_PY, "grade.py", "--project", project, "--baseline", str(base), "--candidate", str(cand), "--split", split])
        if r.returncode != 0:
            raise RuntimeError("grade.py failed: " + r.stderr[-600:])
        return json.loads(r.stdout)

    def fresh(self, directory: Path) -> dict:
        r = sh([DEV_PY, "fresh_check.py", str(directory)])
        return {"passed": r.returncode == 0, "detail": (r.stdout.strip() or r.stderr.strip())[-300:]}

    # -- card adapters -----------------------------------------------------------------------------
    def pair_card(self, cid: str) -> dict:
        card = self.cards[cid]
        project, bar = card["project"], card["min_reference_gain"]
        reg = project in REGRESSION
        rows = []
        for seed in self.seeds:
            base, cand = self.project_run(project, "baseline", seed), self.project_run(project, "reference", seed)
            grade = self.grade(project, Path(base["dir"]) / "test_predictions.npz", Path(cand["dir"]) / "test_predictions.npz")
            vgrade = self.grade(project, Path(base["dir"]) / "validation_predictions.npz",
                                Path(cand["dir"]) / "validation_predictions.npz", "validation")
            es_b, es_c = self.eidos_score(project, base), self.eidos_score(project, cand)
            key = "rmse" if reg else "accuracy"
            agree = (abs(es_b["metrics"][key] - vgrade["baseline"]) < 1e-9 and abs(es_c["metrics"][key] - vgrade["candidate"]) < 1e-9)
            fresh = self.fresh(Path(cand["dir"]))
            if project == "null":
                passed = abs(grade["gain"]) < 1e-12 and grade["candidate"] == .5
            else:
                passed = grade["gain"] >= bar - 1e-12
            rows.append({"seed": seed, "baseline_job": base["job_id"], "reference_job": cand["job_id"],
                         "job_seconds": [base["seconds"], cand["seconds"]], "grade_test": grade, "grade_validation": vgrade,
                         "eidos_validation_scores": {"baseline": es_b, "reference": es_c},
                         "eidos_score_matches_grade_validation": agree, "fresh_check": fresh, "meets_bar": passed,
                         "bar": bar})
        extra = {}
        if project == "synthetic":
            d = self.cache[(project, "reference", self.seeds[0])]
            tmp = self.out / "artifacts" / "synthetic_receipt.json"
            self.lab.client.download(self.lab.project, self.lab.jobs[d["label"]]["outputs"]["receipt"], tmp)
            cfg = json.loads(tmp.read_text())["project_receipt"]["config"]
            extra = {"synthetic_generated": cfg["synthetic_generated"], "examples_seen": cfg["examples_seen"]}
        ok = all(r["meets_bar"] and r["fresh_check"]["passed"] and r["eidos_score_matches_grade_validation"] for r in rows)
        return {"kind": "baseline/reference pair", "rows": rows, "extra": extra, "passed": ok}

    def instrument_card(self, cid: str) -> dict:
        project = self.cards[cid]["project"]
        rows = []
        for seed in self.seeds[:1]:
            cand = self.project_run(project, "reference", seed)
            cfg = command_config(["python", "instrument_adapter.py", "--project", project,
                                  "--predictions", "validation_predictions.npz", "--out", "{out}"], seed=seed,
                                 files={"validation": "validation_predictions.npz"})
            art = self.source_bundle("instrument_adapter.py")
            # eidos.project.command declares no prediction-array input port: the identical bytes are re-registered as
            # task-array/1 (content hash unchanged) so the 'validation' port accepts them. Reported as a product gap.
            pred = Path(cand["dir"]) / "validation_predictions.npz"
            pred_art = self.lab.upload(pred, "task-array/1", f"{project}/{seed}/validation_predictions-as-task-array.npz")
            rec = self.lab.run(f"instrument:{project}/{seed}", self.lab.refs["command"],
                               {"source": art, "validation": pred_art}, cfg)
            self.lab.ok(rec)
            rows.append({"reference_job": cand["job_id"], "instrument_job": rec["job_id"], "seconds": rec["seconds"],
                         "metrics": rec["metrics"], "instrument_sha256": self.bundle_art["instrument_adapter.py"][1]})
        return {"kind": "shared instrument on validation predictions", "rows": rows,
                "passed": all(r["metrics"]["aggregation_checked"] == 1 for r in rows)}

    def worker(self, label: str, seed: int, steps: int, name: str, resume: dict | None = None,
               adapter: str | None = None, extra: list[str] | None = None) -> dict:
        if resume is None and adapter is None:
            argv = ["python", "lifecycle.py", "worker", "--seed", str(seed), "--steps", str(steps), "--output", f"{{out}}/{name}.pt"]
        elif adapter is None:
            argv = ["python", "lifecycle.py", "worker", "--seed", str(seed), "--steps", str(steps),
                    "--resume", "{work}/parent.pt", "--output", f"{{out}}/{name}.pt"]
        elif adapter == "lifecycle_branch.py":
            argv = ["python", adapter, "--parent", "{work}/parent.pt", "--steps", str(steps), "--output", f"{{out}}/{name}.pt"]
        else:
            argv = ["python", adapter, "--seed", str(seed), "--steps", str(steps), "--output", f"{{out}}/{name}.pt"]
        inputs: dict[str, str] = {}
        files = None
        if resume:
            inputs["model"], files = resume["outputs"][resume["slot"]], {"model": "parent.pt"}
        if adapter:
            inputs["source"] = self.source_bundle(adapter)
        rec = self.lab.ok(self.lab.run(label, self.lab.refs["command"], inputs, command_config(argv, seed=seed, files=files)))
        rec["slot"] = name
        d = self.out / "artifacts" / "lifecycle" / str(seed)
        self.lab.fetch(rec, name, d / f"{name}.pt")
        rec["path"] = str(d / f"{name}.pt")
        return rec

    def lifecycle_cards(self, cids: list[str]) -> dict:
        out = {}
        for seed in self.seeds:
            full = self.worker(f"lc/full/{seed}", seed, 40, "full")
            parent = self.worker(f"lc/parent/{seed}", seed, 17, "parent")
            if "D09" in cids:
                res = self.worker(f"lc/resume/{seed}", seed, 40, "resume", resume=parent)
                cmp_ = dev("ckpt-compare", full["path"], res["path"])
                out.setdefault("D09", []).append({"seed": seed, "jobs": {"full": full["job_id"], "parent": parent["job_id"], "resume": res["job_id"]},
                                                  "compare": cmp_, "passed": cmp_["state_exact_equal"]})
            if "D10" in cids:
                sha_before = dev("sha", parent["path"])["sha256"]
                child = self.worker(f"lc/branch/{seed}", seed, 40, "branch", resume=parent, adapter="lifecycle_branch.py")
                art = self.out / "artifacts" / "lifecycle" / str(seed) / "parent-refetched.pt"
                self.lab.client.download(self.lab.project, parent["outputs"]["parent"], art)
                sha_after = dev("sha", str(art))["sha256"]
                cmp_ = dev("ckpt-compare", full["path"], child["path"])
                out.setdefault("D10", []).append({"seed": seed, "jobs": {"parent": parent["job_id"], "branch": child["job_id"]},
                                                  "parent_sha_before": sha_before, "parent_sha_after_branch": sha_after,
                                                  "child_differs_from_unedited_full": not cmp_["model_equal"],
                                                  "branch_receipt_parent_unchanged": child["metrics"].get("parent_unchanged") == 1,
                                                  "passed": sha_before == sha_after and not cmp_["model_equal"] and child["metrics"].get("parent_unchanged") == 1})
            if "D11" in cids:
                crash = self.worker(f"lc/crash/{seed}", seed, 17, "crash", adapter="lifecycle_crash.py")
                res = self.worker(f"lc/crash-resume/{seed}", seed, 40, "crash_resume", resume=crash)
                cmp_ = dev("ckpt-compare", full["path"], res["path"])
                same_parent = dev("ckpt-compare", parent["path"], crash["path"])
                out.setdefault("D11", []).append({"seed": seed, "jobs": {"crash_worker": crash["job_id"], "new_attempt": res["job_id"]},
                                                  "worker_returncode": crash["metrics"].get("worker_returncode"),
                                                  "checkpoint_equals_clean_step17": same_parent["state_exact_equal"],
                                                  "compare": cmp_, "passed": crash["metrics"].get("worker_returncode") == 17 and cmp_["state_exact_equal"]})
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cards", default="all", help="comma list of D01..D14, 'cheap' or 'all'")
    ap.add_argument("--seeds", default="11,23,37")
    ap.add_argument("--project", default="cards")
    ap.add_argument("--setup", action="store_true", help="copy the release, create the project/environment, install components")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cids = ([f"D{i:02d}" for i in range(1, 15)] if a.cards == "all" else CHEAP_SUBSET if a.cards == "cheap"
            else a.cards.split(","))
    if a.setup:
        setup(a.project)
    lab = Lab(a.project)
    run = Runner(lab, out, [int(s) for s in a.seeds.split(",")])
    results: dict[str, Any] = {"label": f"hosted-Linux development run; {LABEL}", "started": time.time(), "cards": {}}
    lifecycle_ids = [c for c in cids if c in ("D09", "D10", "D11")]
    for cid in cids:
        if cid in lifecycle_ids:
            continue
        card = run.cards[cid]
        reg = card["project"] in REGRESSION
        try:
            lab.client.command("commit_study_charter", {"charter": card_charter(card, metric="rmse" if reg else "accuracy",
                               direction="decrease" if reg else "increase")}, project=a.project, key=f"cards:charter:{cid}")
        except Exception as exc:  # noqa: BLE001
            if "already exists" not in str(exc):
                raise
        t0 = time.time()
        try:
            res = run.instrument_card(cid) if card["capabilities"] == "instruments" or card["capabilities"].endswith("instruments") else run.pair_card(cid)
        except Exception as exc:  # noqa: BLE001
            res = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        res["seconds"] = round(time.time() - t0, 1)
        results["cards"][cid] = res
        print(cid, "PASS" if res["passed"] else "FAIL", res["seconds"], "s", flush=True)
        (out / "results.partial.json").write_text(json.dumps(results, indent=2, default=str))
    if lifecycle_ids:
        t0 = time.time()
        try:
            lc = run.lifecycle_cards(lifecycle_ids)
            for cid, rows in lc.items():
                results["cards"][cid] = {"kind": "lifecycle", "rows": rows, "passed": all(r["passed"] for r in rows),
                                         "seconds": round(time.time() - t0, 1)}
        except Exception as exc:  # noqa: BLE001
            for cid in lifecycle_ids:
                results["cards"][cid] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        for cid in lifecycle_ids:
            print(cid, "PASS" if results["cards"][cid]["passed"] else "FAIL", flush=True)
    results["jobs"] = lab.jobs
    results["finished"] = time.time()
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    return 0 if all(c["passed"] for c in results["cards"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
