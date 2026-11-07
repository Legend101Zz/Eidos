"""Runner and assessor activities (training-runner, sealed-runner and assessor-worker processes)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from eidos.client import LabClient, LabError
from eidos.runtime.runner import AttemptRunner, RetryableAttemptError


class RunnerActivities:
    def __init__(self, runner: AttemptRunner):
        self.runner = runner

    @activity.defn(name="run_attempt")
    def run_attempt(self, a: dict) -> dict:
        info = activity.info()
        cancel = threading.Event()

        def hb(d: dict) -> None:
            try:
                activity.heartbeat(d)
            except Exception:
                cancel.set()

        def cancelled() -> bool:
            return cancel.is_set() or activity.is_cancelled()

        try:
            return self.runner.run_job(a["job_id"], a["project_id"], activity_attempt=info.attempt, heartbeat=hb,
                                       cancelled=cancelled)
        except RetryableAttemptError as exc:
            raise ApplicationError(str(exc), type="RetryableAttempt") from exc
        except LabError as exc:
            raise ApplicationError(str(exc), exc.error, type=exc.code or "LAB_ERROR",
                                   non_retryable=exc.status in (400, 403, 404, 422)) from exc

    @activity.defn(name="fail_job")
    def fail_job(self, a: dict) -> dict:
        """Retries exhausted: close the current attempt as a final failure (fenced if superseded)."""
        job = self.runner.client.query("get_job", {"job_id": a["job_id"]}, project=a["project_id"])
        if job["status"] in ("succeeded", "failed", "cancelled") or not job["current_attempt"]:
            return {"job_status": job["status"]}
        r = self.runner.client.command("finish_attempt", {"job_id": job["id"], "attempt_id": job["current_attempt"],
                                       "epoch": job["epoch"], "status": "failed", "final": True,
                                       "error": {"category": "infrastructure", "message": a.get("error", "")[:1000]}},
                                       project=a["project_id"], key=f"finalfail:{job['current_attempt']}")
        return r["result"]


class AssessorActivities:
    """Runs the plan's pinned, approved assessor release on evidence gathered through authorized
    reads, then submits through the command service. Holds ``assess`` and ``qualify`` grants
    (two principals), never ledger credentials or candidate code."""

    def __init__(self, assessor: LabClient, qualifier: LabClient, runner_factory, lab_python: str):
        self.assessor = assessor
        self.qualifier = qualifier
        self.runner_factory = runner_factory
        self.lab_python = lab_python

    @activity.defn(name="assess_plan")
    def assess_plan(self, a: dict) -> dict:
        p = a["project_id"]
        inputs = self.assessor.query("assessment_inputs", {"plan_id": a["plan_id"]}, project=p)
        plan = inputs["plan"]
        evidence = {"plan": plan, "arms": {}}
        evidence_set: dict[str, list[str]] = {}
        role_of = {arm["id"]: arm["role"] for arm in plan["arms"]}
        for arm_id, arm in inputs["arms"].items():
            rows: list[dict] = []
            evidence_set[arm_id] = []
            for out in arm["outputs"]:
                if out["slot"] != "predictions":
                    continue
                evidence_set[arm_id].append(out["artifact_id"])
                with tempfile.NamedTemporaryFile(suffix=".data") as tmp:
                    self.assessor.download(p, out["artifact_id"], tmp.name)
                    rows.extend(_read_rows(tmp.name, out["media_type"]))
            evidence["arms"][arm_id] = {"role": role_of.get(arm_id), "rows": rows, "status": arm["status"]}
        evaluation = self._run_assessor(p, plan["assessor"]["ref"], evidence)
        try:
            r = self.assessor.command("submit_assessment", {"plan_id": a["plan_id"], "plan_digest": inputs["plan_digest"],
                                      "assessor_ref": plan["assessor"]["ref"], "evidence_set": evidence_set,
                                      "evaluation": evaluation}, project=p,
                                      key=f"assess:{a['plan_id']}:{inputs['plan_digest']}")
        except LabError as exc:
            raise ApplicationError(str(exc), exc.error, type=exc.code or "LAB_ERROR",
                                   non_retryable=exc.status in (400, 403, 404, 409, 422)) from exc
        return {"assessment_id": r["result"]["assessment_id"], "outcome": r["result"]["outcome"]}

    def _run_assessor(self, p: str, ref: str, evidence: dict) -> dict:
        desc = self.assessor.query("describe_component", {"component_ref": ref}, project=p)
        entry = desc["body"]["manifest"]["entry"]
        with tempfile.TemporaryDirectory() as d:
            bundle = Path(d) / "bundle.json"
            self.assessor.download(p, desc["body"]["bundle_artifact_id"], bundle)
            from eidos.runtime.runner import materialize_bundle
            src = materialize_bundle(str(bundle), Path(d) / "src")
            ev = Path(d) / "evidence.json"
            ev.write_text(json.dumps(evidence))
            from eidos_worker.testing import make_invocation, read_output_json, run_subprocess
            inv = make_invocation(Path(d) / "run", entry, inputs={"evidence": (ev.read_bytes(), "application/json")},
                                  config={}, source_dir=str(src))
            man = run_subprocess(inv)
            if man.status != "succeeded":
                raise ApplicationError(f"assessor failed: {man.error}", type="ASSESSOR_FAILED", non_retryable=True)
            return read_output_json(inv, man, "evaluation")

    @activity.defn(name="submit_qualification_results")
    def submit_qualification_results(self, a: dict) -> dict:
        p = a["project_id"]
        job = self.qualifier.query("get_job", {"job_id": a["job_id"]}, project=p)
        q = job["lock"]["qualification"]
        out = next(o for o in job["outputs"] if o["slot"] == "qualification_results")
        with tempfile.NamedTemporaryFile() as tmp:
            self.qualifier.download(p, out["artifact_id"], tmp.name)
            results = json.load(open(tmp.name))
        r = self.qualifier.command("submit_qualification", {**q, "results": results.get("results", results),
                                   "job_id": job["id"], "environment": job["lock"]["environment"].get("lock_digest")},
                                   project=p, key=f"qual:{job['id']}")
        return {"qualification_id": r["result"]["qualification_id"], "status": r["result"]["status"]}


def _read_rows(path: str, media_type: str) -> list[dict[str, Any]]:
    import csv
    if media_type in ("text/csv",):
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))
    text = Path(path).read_text()
    if media_type == "application/json":
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("rows", [])
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wait_until(pred, timeout: float, interval: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False
