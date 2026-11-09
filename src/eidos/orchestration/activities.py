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
            # Start keys are unique per workflow run AND activity attempt: a re-dispatched JobWorkflow
            # restarts activity attempt numbering at 1 and must not replay an old start receipt.
            return self.runner.run_job(a["job_id"], a["project_id"], activity_attempt=f"{info.workflow_run_id}:{info.attempt}",
                                       heartbeat=hb, cancelled=cancelled)
        except RetryableAttemptError as exc:
            raise ApplicationError(str(exc), type="RetryableAttempt") from exc
        except LabError as exc:
            raise ApplicationError(str(exc), exc.error, type=exc.code or "LAB_ERROR",
                                   non_retryable=exc.status in (400, 403, 404, 422)) from exc

    @activity.defn(name="fail_job")
    def fail_job(self, a: dict) -> dict:
        """Retries exhausted: close the current attempt as a final failure (fenced if superseded)."""
        job = self.runner.client.query("get_job", {"job_id": a["job_id"]}, project=a["project_id"])
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return {"job_status": job["status"]}
        if not job["current_attempt"]:
            return self.runner.client.command("abandon_unstarted_job", {"job_id": job["id"], "reason": a.get("error", "")},
                                              project=a["project_id"], key=f"abandon:{job['id']}")["result"]
        r = self.runner.client.command("finish_attempt", {"job_id": job["id"], "attempt_id": job["current_attempt"],
                                       "epoch": job["epoch"], "status": "failed", "final": True,
                                       "error": {"category": "infrastructure", "message": a.get("error", "")[:1000]}},
                                       project=a["project_id"], key=f"finalfail:{job['current_attempt']}")
        return r["result"]


def _read_staged_json(staging: Path, man, slot: str, max_bytes: int) -> dict:
    """Read an output written by a sandboxed process from the unsandboxed side: the same checks as the
    runner (regular file contained in staging, declared digest and size), plus a size cap."""
    from eidos_wire import sha256_file
    entry = next((e for e in man.outputs if e.slot == slot), None)
    if entry is None:
        raise ApplicationError(f"assessor produced no {slot!r} output", type="ASSESSOR_FAILED", non_retryable=True)
    src = staging / entry.filename
    if src.is_symlink() or not src.is_file() or staging.resolve() not in src.resolve().parents:
        raise ApplicationError(f"assessor output {slot} is not a regular file inside staging", type="ASSESSOR_FAILED",
                               non_retryable=True)
    digest, size = sha256_file(src)
    if size > max_bytes or digest != str(entry.digest).replace("sha256:", "") or size != entry.size:
        raise ApplicationError(f"assessor output {slot} exceeds the cap or does not match its declared digest/size",
                               type="ASSESSOR_FAILED", non_retryable=True)
    out = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(out, dict):
        raise ApplicationError(f"assessor output {slot} is not a JSON object", type="ASSESSOR_FAILED", non_retryable=True)
    return out


class AssessorActivities:
    """Runs the plan's pinned, approved assessor release on evidence gathered through authorized
    reads, then submits through the command service. Holds ``assess`` and ``qualify`` grants
    (two principals), never ledger credentials or candidate code."""

    def __init__(self, assessor: LabClient, qualifier: LabClient, runner_factory, lab_python: str, *,
                 credential_path: str = "/nonexistent", api_port: int = 0, max_evidence_bytes: int = 256 << 20,
                 lab_root: str | None = None):
        self.assessor = assessor
        self.qualifier = qualifier
        self.runner_factory = runner_factory
        self.lab_python = lab_python
        self.credential_path = credential_path
        self.api_port = api_port
        self.max_evidence_bytes = max_evidence_bytes
        self.lab_root = lab_root

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
        payload = {"plan_id": a["plan_id"], "plan_digest": inputs["plan_digest"],
                   "assessor_ref": plan["assessor"]["ref"], "evidence_set": evidence_set, "evaluation": evaluation}
        # The key covers the whole payload: an assessor that restarts and re-evaluates the same evidence can never
        # hit a command-key conflict; the service deduplicates by (frozen plan, evidence-set digest, assessor
        # release) and returns the same assessment receipt (design: "Assessor restarts or evidence arrives twice").
        from eidos_wire import canonical_json, sha256_bytes
        key = f"assess:{a['plan_id']}:" + sha256_bytes(canonical_json(payload).encode())[:32]
        try:
            r = self.assessor.command("submit_assessment", payload, project=p, key=key)
        except LabError as exc:
            raise ApplicationError(str(exc), exc.error, type=exc.code or "LAB_ERROR",
                                   non_retryable=exc.status in (400, 403, 404, 409, 422)) from exc
        return {"assessment_id": r["result"]["assessment_id"], "outcome": r["result"]["outcome"]}

    def _run_assessor(self, p: str, ref: str, evidence: dict) -> dict:
        """Run the pinned assessment release in its own sandbox: read-only release, evidence and lab
        interpreter; writable staging/scratch only; no lab secrets or environment. Candidate code is
        never involved; predictions arrive as parsed rows inside evidence.json."""
        import os
        import time as _time

        from eidos.boundary.wire import validate_wire
        from eidos.runtime.launcher import (
            LaunchSpec,
            detect_sealed_profile,
            launch,
            profile_config_digest,
            run_probes,
            sealed_deny_read,
        )
        from eidos.runtime.runner import SDK_PATHS, materialize_bundle, python_prefixes
        from eidos_wire import OutputManifest
        from eidos_worker.testing import make_invocation
        desc = self.assessor.query("describe_component", {"component_ref": ref}, project=p)
        entry = desc["body"]["manifest"]["entry"]
        base = Path(tempfile.mkdtemp(prefix="eidos-assess-"))
        os.chmod(base, 0o755)
        try:
            bundle = base / "bundle.json"
            self.assessor.download(p, desc["body"]["bundle_artifact_id"], bundle)
            src = materialize_bundle(str(bundle), base / "src")
            ev = base / "evidence.json"
            # Review 2: evidence was 0644 inside a 0755 directory; only the lab user (the assessor's
            # identity) needs to read it.
            fd = os.open(ev, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(evidence))
            if ev.stat().st_size > int(self.max_evidence_bytes):
                raise ApplicationError("evidence exceeds the assessor input cap", type="ASSESSOR_INPUT_TOO_LARGE",
                                       non_retryable=True)
            inv = make_invocation(base / "run", entry, inputs={"evidence": (ev.read_bytes(), "application/json")},
                                  config={}, source_dir=str(src))
            for staged in inv.inputs.values():
                os.chmod(staged.path, 0o600)  # the staged copy of the evidence, likewise owner-only
            inv_path = base / "run" / "invocation.json"
            if not inv_path.exists():
                inv.write(inv_path)
            import eidos as _eidos
            lab_src = str(Path(_eidos.__file__).resolve().parent.parent)
            profile = detect_sealed_profile() or "dev:same-user"
            # The assessor role reads only its release, the evidence and the lab interpreter; on Seatbelt
            # the lab home, its own credential directory and the user's home directory are unreadable.
            deny = sealed_deny_read(self.lab_root, os.path.dirname(self.credential_path))
            readonly = sorted({*SDK_PATHS, lab_src, str(src), str(inv_path), *[i.path for i in inv.inputs.values()]})
            spec = LaunchSpec(argv=[self.lab_python, "-m", "eidos_worker.run", str(inv_path)], cwd=str(src),
                              attempt_dir=str(base / "proc"), profile=profile, readonly=readonly,
                              writable=[inv.staging_dir, inv.scratch_dir],
                              env={"PYTHONPATH": os.pathsep.join([*SDK_PATHS, lab_src, str(src)])},
                              python_prefixes=python_prefixes(self.lab_python) if profile != "dev:same-user" else [],
                              deny_read=deny if profile != "dev:same-user" else [],
                              # Trusted role: the lab identity, never the candidate user (the evidence is
                              # readable only by the lab identity; a candidate-identity assessor failed closed).
                              identity="lab")
            from eidos.domain.isolation import profile_traits
            # same_os_user is reported so a macOS (Seatbelt) or development assessment says plainly that
            # the assessor ran as the lab's own OS user (docs/release/SUPPORTED.md).
            receipt = {"profile": profile, "config_digest": profile_config_digest(spec), "role": "assessor",
                       "release": ref, **{k: v for k, v in profile_traits(profile).items() if k != "profile"}}
            if profile != "dev:same-user":
                # A planted label where lab-held labels live (inside a denied root), never in the
                # assessor's own working directory, which it may read.
                canary_dir = Path(self.lab_root, "config") if self.lab_root else Path(os.path.dirname(self.credential_path))
                canary = canary_dir / "assessor-label-canary"  # not under secrets/: doctor checks those are 0600
                if not canary.exists():
                    canary.write_text("x")
                receipt.update(run_probes(spec, label_path=str(canary), credential_path=self.credential_path,
                                          host_write_path=str(base / "host-write"), api_port=self.api_port))
            h = launch(spec)
            deadline = _time.time() + 600
            while h.exit_code() is None and _time.time() < deadline:
                _time.sleep(0.2)
                activity.heartbeat()
            if h.exit_code() is None:
                h.terminate()
                raise ApplicationError("assessor timed out", type="ASSESSOR_FAILED", non_retryable=True)
            staging = Path(inv.staging_dir)
            if (staging / OutputManifest.FILENAME).is_symlink():
                raise ApplicationError("assessor manifest is not a regular file", type="ASSESSOR_FAILED",
                                       non_retryable=True)
            try:
                raw = json.loads((staging / OutputManifest.FILENAME).read_text())
                validate_wire("output-manifest", raw)
                man = OutputManifest.from_dict(raw)
            except Exception as exc:  # noqa: BLE001 - no manifest means the assessor process itself failed
                err = (base / "proc" / "stderr.log").read_text()[-800:] if (base / "proc" / "stderr.log").exists() else ""
                raise ApplicationError(f"assessor produced no output manifest ({exc}): {err}",
                                       type="ASSESSOR_FAILED", non_retryable=True) from exc
            if man.status != "succeeded" or h.exit_code() != 0:
                raise ApplicationError(f"assessor failed: {man.error}", type="ASSESSOR_FAILED", non_retryable=True)
            out = _read_staged_json(staging, man, "evaluation", self.max_evidence_bytes)
            out["assessor_isolation"] = receipt
            return out
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

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
