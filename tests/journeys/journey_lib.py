"""Shared plumbing for the switch-order journey: a recording client wrapper and step bookkeeping.

Everything goes through the lab's HTTP API (``LabClient``) or the ``lab`` CLI in a fresh process; the
journey has no database or orchestration access of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from eidos.client import LabClient, LabError
from eidos.settings import LabConfig

TERMINAL = ("succeeded", "failed", "cancelled", "pruned")


class Blocked(Exception):
    """A step cannot run (a prerequisite failed or a lab defect prevents it)."""


class Journey:
    def __init__(self, project: str, out_path: Path):
        cfg = LabConfig.load()
        self.cfg = cfg
        self.client = LabClient(cfg.api_url, cfg.token("admin"), client_name="switch-journey", timeout=120)
        self.project = project
        self.out_path = out_path
        self.rec: dict[str, Any] = {"project": project, "api_url": cfg.api_url, "started": time.time(), "steps": {},
                                    "jobs": {}, "artifacts": {}, "notes": [], "defects": []}
        self.S: dict[str, Any] = {}  # working state shared between steps (ids)
        self._cur: dict[str, Any] | None = None

    # -- recording -----------------------------------------------------------------------------
    def save(self) -> None:
        self.rec["updated"] = time.time()
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text(json.dumps(self.rec, indent=2, sort_keys=True, default=str))

    def note(self, text: str) -> None:
        self.rec["notes"].append(text)
        print("  note:", text, flush=True)

    def defect(self, text: str) -> None:
        self.rec["defects"].append(text)
        print("  DEFECT:", text, flush=True)

    @contextmanager
    def step(self, key: str, title: str):
        cur = {"title": title, "checks": [], "numbers": {}, "status": "running", "started": time.time()}
        self.rec["steps"][key] = cur
        self._cur = cur
        print(f"\n== {key} {title}", flush=True)
        try:
            yield cur
            failed = [c for c in cur["checks"] if not c["ok"]]
            cur["status"] = "fail" if failed else "pass"
        except Blocked as exc:
            cur["status"] = "blocked"
            cur["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            cur["status"] = "fail"
            cur["error"] = f"{type(exc).__name__}: {exc}"
            cur["traceback"] = traceback.format_exc()[-3000:]
            print(cur["traceback"], flush=True)
        finally:
            cur["seconds"] = round(time.time() - cur["started"], 2)
            print(f"== {key} -> {cur['status']} ({cur['seconds']}s)", flush=True)
            self._cur = None
            self.save()

    def check(self, label: str, ok: bool, **detail: Any) -> bool:
        ok = bool(ok)
        assert self._cur is not None
        self._cur["checks"].append({"label": label, "ok": ok, **({"detail": detail} if detail else {})})
        print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f"  {json.dumps(detail, default=str)[:300]}" if detail else ""),
              flush=True)
        return ok

    def number(self, name: str, value: Any) -> None:
        assert self._cur is not None
        self._cur["numbers"][name] = value

    # -- lab access ----------------------------------------------------------------------------
    def cmd(self, name: str, payload: dict | None = None, *, key: str | None = None,
            expected_revision: int | None = None, project: str | None = None) -> dict:
        return self.client.command(name, payload or {}, project=project or self.project, key=key,
                                   expected_revision=expected_revision)

    def cmd_result(self, *a: Any, **kw: Any) -> dict:
        return self.cmd(*a, **kw)["result"]

    def expect_error(self, label: str, name: str, payload: dict, *codes: str, **kw: Any) -> dict | None:
        """Run a command that the lab must refuse; record the refusal code."""
        try:
            r = self.cmd(name, payload, **kw)
        except LabError as exc:
            ok = (not codes) or exc.code in codes
            self.check(label, ok, code=exc.code, message=str(exc.error.get("message"))[:240])
            return exc.error
        self.check(label, False, unexpected_success=json.dumps(r.get("result"), default=str)[:200])
        return None

    def query(self, name: str, params: dict | None = None, project: str | None = None) -> Any:
        return self.client.query(name, params or {}, project=project or self.project)

    def wait_job(self, job_id: str, timeout: float = 900) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            j = self.query("get_job", {"job_id": job_id, "include_lock": False})
            if j["status"] in TERMINAL:
                return j
            time.sleep(0.5)
        raise TimeoutError(job_id)

    def record_job(self, label: str, job: dict, t0: float | None = None) -> dict:
        term = job.get("terminal") or {}
        rec = {"id": job["id"], "status": job["status"], "kind": job.get("kind"),
               "receipt_metrics": (term.get("receipt") or {}).get("metrics"),
               "outputs": {o["slot"]: o["artifact_id"] for o in job.get("outputs", [])}}
        if t0 is not None:
            rec["seconds"] = round(time.time() - t0, 2)
        if job["status"] != "succeeded":
            rec["error"] = term.get("error")
        self.rec["jobs"][label] = rec
        for o in job.get("outputs", []):
            self.rec["artifacts"][f"{label}:{o['slot']}"] = o["artifact_id"]
        return rec

    def run(self, label: str, ref: str, *, inputs: dict | None = None, config: dict | None = None,
            environment: str | None = None, wall_seconds: int = 600, expect: str = "succeeded", **extra: Any) -> dict:
        """run_component + wait. Returns {id, job, receipt, out:{slot: artifact_id}}."""
        t0 = time.time()
        payload = {"component_ref": ref, "inputs": inputs or {}, "config": config or {}, "wall_seconds": wall_seconds,
                   "environment": environment, **extra}
        r = self.cmd_result("run_component", payload)
        job = self.wait_job(r["job_id"])
        self.record_job(label, job, t0)
        term = job.get("terminal") or {}
        if expect:
            self.check(f"job {label} {expect}", job["status"] == expect, job=job["id"],
                       error=(term.get("error") or {}).get("message") if job["status"] != "succeeded" else None)
        return {"id": r["job_id"], "trial_id": r["trial_id"], "job": job, "receipt": term.get("receipt") or {},
                "out": {o["slot"]: o["artifact_id"] for o in job.get("outputs", [])}}

    # -- CLI in a fresh process -----------------------------------------------------------------
    def at_file(self, obj: Any) -> str:
        """Write a JSON CLI argument next to the record and return `@path` (the CLI's JSON-or-@file options).

        JSON never goes on argv: Linux caps one argv string at 128 KiB (MAX_ARG_STRLEN, `Argument list too long`)."""
        d = self.out_path.parent / "cli-payloads"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{len(list(d.iterdir())):04d}.json"
        f.write_text(json.dumps(obj, default=str))
        return f"@{f}"

    def cli(self, *args: str, check: bool = True, timeout: float = 900) -> tuple[int, Any, str]:
        env = dict(os.environ)
        r = subprocess.run([str(Path(sys.executable).parent / "lab"), *args], capture_output=True, text=True,
                           env=env, timeout=timeout)
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else None
        except json.JSONDecodeError:  # line-oriented output (investigate events): one JSON object per line
            try:
                lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
                data = {**lines[-1], "lines": lines[:-1]}
            except (json.JSONDecodeError, IndexError):
                data = None
        return r.returncode, data, r.stderr

    def download_json(self, artifact_id: str) -> Any:
        import tempfile
        with tempfile.NamedTemporaryFile() as tmp:
            self.client.download(self.project, artifact_id, tmp.name)
            return json.loads(Path(tmp.name).read_text())

    def download_text(self, artifact_id: str) -> str:
        import tempfile
        with tempfile.NamedTemporaryFile() as tmp:
            self.client.download(self.project, artifact_id, tmp.name)
            return Path(tmp.name).read_text()
