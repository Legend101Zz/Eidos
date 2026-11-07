"""Attempt runner: materialize, launch, supervise, verify, publish.

Runs inside the training-runner / sealed-runner processes as the trusted launcher. It holds a
``launcher`` API credential and write access to the artifact store, and nothing else: it never
touches the database, and the research process it starts receives no lab credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import eidos_pytorch
import eidos_wire
import eidos_worker
from eidos.client import LabClient, LabError
from eidos.runtime.artifacts import LocalArtifactStore, StoreError
from eidos.runtime.launcher import (
    CANDIDATE_USER,
    LaunchSpec,
    ProcessHandle,
    candidate_uid,
    launch,
    profile_config_digest,
    run_probes,
)
from eidos_wire import InputBinding, Invocation, OutputManifest, WireError, sha256_file

SDK_PATHS = sorted({str(Path(m.__file__).resolve().parent.parent) for m in (eidos_wire, eidos_worker, eidos_pytorch)})
HEARTBEAT_SECONDS = 10.0

VALIDATE_SNIPPET = (
    "import json,sys\n"
    "from eidos_worker.validate import validate_component\n"
    "r=validate_component(sys.argv[1])\n"
    "print(json.dumps({'component': r.component, 'manifest_problems': r.manifest_problems, 'passed': r.ok,"
    "'fixtures': [{'path': f.path, 'passed': f.passed, 'skipped': f.skipped, 'problems': f.problems} for f in r.fixtures]}))\n"
)


class RetryableAttemptError(Exception):
    """Infrastructure failure; a new attempt (new epoch) may succeed."""


def materialize_bundle(bundle_path: str, dest: Path) -> Path:
    data = json.loads(Path(bundle_path).read_text())
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    for rel, content in (data.get("files") or {}).items():
        target = (dest / rel).resolve()
        if root not in target.parents or Path(rel).is_absolute():
            raise StoreError(f"bundle path escapes the source directory: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict) and "base64" in content:
            import base64
            target.write_bytes(base64.b64decode(content["base64"]))
        else:
            target.write_text(content)
    return dest


_PREFIX_CACHE: dict[str, list[str]] = {}


def python_prefixes(python: str) -> list[str]:
    if python not in _PREFIX_CACHE:
        out = subprocess.run([python, "-c", "import sys,json;print(json.dumps([sys.prefix, sys.base_prefix, sys.exec_prefix]))"],
                             capture_output=True, text=True, timeout=60)
        paths = json.loads(out.stdout) if out.returncode == 0 else []
        real = {str(Path(python).resolve().parent.parent)}
        _PREFIX_CACHE[python] = sorted(set(paths) | real)
    return _PREFIX_CACHE[python]


class AttemptRunner:
    def __init__(self, client: LabClient, store: LocalArtifactStore, *, lab_root: str | Path, launcher_id: str,
                 api_port: int, credential_path: str, sealed_label_canary: str | None = None,
                 allowed_profiles: tuple[str, ...] = ("dev:same-user",)):
        self.client = client
        self.store = store
        self.root = Path(lab_root)
        self.launcher_id = launcher_id
        self.api_port = api_port
        self.credential_path = credential_path
        self.label_canary = sealed_label_canary or str(self.root / "config" / "label-canary")
        self.allowed_profiles = allowed_profiles

    # -------------------------------------------------------------------------------------
    def run_job(self, job_id: str, project: str, *, activity_attempt: int,
                heartbeat: Callable[[dict], None] = lambda d: None,
                cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        job = self.client.query("get_job", {"job_id": job_id}, project=project)
        if job["status"] in ("succeeded", "failed", "cancelled", "pruned"):
            return {"job_status": job["status"], "reattached": False}
        # Reattach to a live attempt rather than starting a duplicate.
        att = next((a for a in job["attempts"] if a["id"] == job["current_attempt"]), None)
        if att and att["status"] == "running" and att["backend_handle"].get("pid"):
            h = ProcessHandle.from_dict(att["backend_handle"])
            if h.alive() or h.exit_code() is not None:
                ctx = {"job_id": job_id, "attempt_id": att["id"], "epoch": att["epoch"], "lock": job["lock"],
                       "attempt_dir": h.attempt_dir, "reattached": True, "started": time.time()}
                return self._supervise(project, ctx, h, heartbeat, cancelled)
            # The recorded process is gone without an exit status (machine restart, power loss, kill):
            # reconcile it as lost, with the elapsed time it may have consumed, before a new fenced attempt.
            from datetime import datetime
            try:
                started = datetime.fromisoformat(str(att["started_at"])).timestamp()
            except ValueError:
                started = time.time()
            self.client.command("finish_attempt", {"job_id": job_id, "attempt_id": att["id"], "epoch": att["epoch"],
                                "status": "lost", "error": {"category": "lost", "message":
                                "process handle dead without exit status (pid/start-time/boot-id check)"},
                                "usage": {"compute_seconds": round(max(0.0, time.time() - started), 3), "uncertain": True}},
                                project=project, key=f"lost:{att['id']}")
        st = self.client.command("start_attempt", {"job_id": job_id, "lease_seconds": 120}, project=project,
                                 key=f"start:{job_id}:{activity_attempt}")["result"]
        if not st.get("run"):
            return {"job_status": st.get("status"), "reattached": False}
        lock = st["lock"]
        profile = lock.get("isolation_profile", "dev:same-user")
        ctx = {"job_id": job_id, "attempt_id": st["attempt_id"], "epoch": st["epoch"], "lock": lock,
               "attempt_dir": str(self.root / "jobs" / job_id / st["attempt_id"]), "reattached": False,
               "started": time.time()}
        if profile not in self.allowed_profiles:
            return self._finish(project, ctx, "failed", {}, {"category": "isolation-unavailable",
                                "message": f"this launcher cannot provide profile {profile}"}, final=True)
        try:
            spec = self._prepare(st, ctx)
        except (StoreError, OSError, WireError, LabError) as exc:
            return self._finish(project, ctx, "failed", {}, {"category": "contract", "message": str(exc)[:2000]},
                                final=True)
        receipt = self._isolation_receipt(spec, profile)
        self.client.command("record_isolation_receipt", {"job_id": job_id, "attempt_id": ctx["attempt_id"],
                            "epoch": ctx["epoch"], "receipt": receipt}, project=project,
                            key=f"iso:{ctx['attempt_id']}")
        h = launch(spec)
        self.client.command("record_attempt_handle", {"job_id": job_id, "attempt_id": ctx["attempt_id"],
                            "epoch": ctx["epoch"], "backend_handle": h.to_dict()}, project=project,
                            key=f"handle:{ctx['attempt_id']}")
        return self._supervise(project, ctx, h, heartbeat, cancelled)

    # -------------------------------------------------------------------------------------
    def _prepare(self, st: dict, ctx: dict) -> LaunchSpec:
        lock = st["lock"]
        adir = Path(ctx["attempt_dir"])
        if adir.exists():
            shutil.rmtree(adir)
        staging, scratch = adir / "staging", adir / "scratch"
        for d in (staging, scratch):
            d.mkdir(parents=True)
        env = lock["environment"]
        python = env["python_executable"]
        source = None
        if st.get("bundle"):
            source = materialize_bundle(st["bundle"]["path"], adir / "source")
        elif lock["component"].get("kind") == "build":
            source = materialize_bundle(st["inputs"]["bundle"]["path"], adir / "source")
        profile = lock.get("isolation_profile", "dev:same-user")
        sealed = profile == "linux:bwrap-sealed"
        inputs: dict[str, InputBinding] = {}
        readonly = list(SDK_PATHS) + [env["project_root"]] + ([str(source)] if source else [])
        for port, inp in (st.get("inputs") or {}).items():
            path = inp["path"]
            if sealed:  # private readable copies; the candidate identity cannot read the store
                cp = adir / "inputs" / port
                cp.parent.mkdir(exist_ok=True)
                shutil.copyfile(path, cp)
                os.chmod(cp, 0o444)
                path = str(cp)
            readonly.append(path)
            inputs[port] = InputBinding(inp["artifact_id"], inp["digest"], path, inp.get("media_type") or "application/octet-stream",
                                        inp.get("schema"), inp.get("size"))
        ports = {k: v for k, v in (lock.get("ports") or {}).items()}
        for name, b in (lock.get("extra_bundles") or {}).items():
            p = materialize_bundle(self.store.path_for(b["digest"]), adir / f"source-{name}")
            readonly.append(str(p))
            if name.startswith("ext-"):  # published training-extension releases pinned in the lock
                ports.setdefault("extension.source/1", {"paths": []})["paths"].append(str(p))
            else:
                ports[f"{name}.source/1"] = {"path": str(p)}
        comp = dict(lock["component"])
        comp["source_dir"] = str(source) if source else None
        pythonpath = os.pathsep.join(SDK_PATHS + ([str(source)] if source else []))
        if sealed and candidate_uid(python):
            for d in (adir, staging, scratch):
                shutil.chown(d, CANDIDATE_USER, CANDIDATE_USER)
            if adir.parent.exists():
                os.chmod(adir.parent, 0o755)
        if lock["component"].get("kind") == "build":
            argv = [python, "-I", "-c", "import sys; sys.path[:0]=" + json.dumps(pythonpath.split(os.pathsep)) + "\n" + VALIDATE_SNIPPET,
                    str(source)]
        else:
            inv = Invocation(job_id=ctx["job_id"], attempt_id=ctx["attempt_id"], epoch=ctx["epoch"],
                             project_id=lock.get("project_id", ""), component=comp, inputs=inputs,
                             config=lock.get("config") or {}, staging_dir=str(staging), scratch_dir=str(scratch),
                             limits=lock.get("limits") or {}, ports=ports, seed=lock.get("seed"))
            inv.write(adir / "invocation.json")
            os.chmod(adir / "invocation.json", 0o444)
            readonly.append(str(adir / "invocation.json"))
            argv = [python, "-m", "eidos_worker.run", str(adir / "invocation.json")]
        return LaunchSpec(argv=argv, cwd=env["project_root"], attempt_dir=str(adir),
                          env={"PYTHONPATH": pythonpath, "PYTHONHASHSEED": "0"},
                          profile=profile, readonly=readonly, writable=[str(staging), str(scratch)],
                          python_prefixes=python_prefixes(python) if sealed else [])

    def _isolation_receipt(self, spec: LaunchSpec, profile: str) -> dict:
        import platform
        base = {"profile": profile, "platform": platform.platform(), "kernel": platform.release(),
                "launcher_release": "eidos-launcher/1", "config_digest": profile_config_digest(spec),
                "observed_at": time.time()}
        if profile == "dev:same-user":
            return {**base, "controls": {}, "summary": "host process, same OS user; no sealed controls claimed"}
        Path(self.label_canary).parent.mkdir(parents=True, exist_ok=True)
        if not Path(self.label_canary).exists():
            Path(self.label_canary).write_text("canary")
            os.chmod(self.label_canary, 0o600)
        probes = run_probes(spec, label_path=self.label_canary, credential_path=self.credential_path,
                            host_write_path=str(self.root / "config" / "probe-host-write"), api_port=self.api_port)
        return {**base, **probes, "threat_model": "local process; kernel, administrator and drivers trusted"}

    # -------------------------------------------------------------------------------------
    def _supervise(self, project: str, ctx: dict, h: ProcessHandle, heartbeat, cancelled) -> dict:
        wall = float(ctx["lock"].get("limits", {}).get("wall_seconds", 900))
        started = ctx["started"]
        last_hb = 0.0
        while h.exit_code() is None:
            if not h.alive() and h.exit_code() is None:
                time.sleep(0.2)
                if h.exit_code() is None:
                    return self._finish(project, ctx, "failed", {}, {"category": "lost",
                                        "message": "process vanished without an exit status"})
            now = time.time()
            if now - last_hb >= HEARTBEAT_SECONDS:
                last_hb = now
                heartbeat({"attempt_id": ctx["attempt_id"], "elapsed": now - started})
                try:
                    hb = self.client.command("heartbeat_attempt", {"job_id": ctx["job_id"], "attempt_id": ctx["attempt_id"],
                                             "epoch": ctx["epoch"], "progress": {"elapsed": now - started}},
                                             project=project)["result"]
                    if hb.get("cancel_requested") and hb.get("stop_policy") == "checkpoint-and-stop":
                        if not ctx.get("stop_signalled"):
                            ctx["stop_signalled"] = now
                            ctx["stop_requested"] = True
                            h.signal_group(__import__("signal").SIGUSR1)
                        elif now - ctx["stop_signalled"] > float(ctx["lock"].get("limits", {}).get("stop_grace_seconds", 120)):
                            h.terminate()
                            return self._finish(project, ctx, "cancelled", {}, {"category": "stop-grace-exceeded",
                                                "message": "worker did not checkpoint within the grace period"})
                    elif hb.get("cancel_requested"):
                        h.terminate()
                        return self._finish(project, ctx, "cancelled", {}, None)
                except LabError as exc:
                    if exc.code == "FENCED":
                        h.terminate()
                        return {"job_status": "fenced", "attempt_id": ctx["attempt_id"]}
            if cancelled():
                h.terminate()
                return self._finish(project, ctx, "cancelled", {}, None)
            if now - started > wall:
                h.terminate()
                return self._finish(project, ctx, "failed", {}, {"category": "resource",
                                    "message": f"wall-clock limit {wall:.0f}s exceeded"}, final=True)
            time.sleep(0.25)
        return self._collect(project, ctx, h)

    def _collect(self, project: str, ctx: dict, h: ProcessHandle) -> dict:
        adir = Path(ctx["attempt_dir"])
        staging = adir / "staging"
        if ctx["lock"]["component"].get("kind") == "build":
            return self._collect_build(project, ctx, adir)
        try:
            man = OutputManifest.read(staging)
        except (OSError, ValueError, WireError) as exc:
            err = (adir / "stderr.log").read_text()[-3000:] if (adir / "stderr.log").exists() else ""
            return self._finish(project, ctx, "failed", {}, {"category": "infrastructure",
                                "message": f"no valid output manifest ({exc}); exit={h.exit_code()}", "stderr_tail": err})
        outputs = []
        for o in man.outputs:
            src = (staging / o.filename)
            if src.is_symlink() or staging.resolve() not in src.resolve().parents:
                return self._finish(project, ctx, "failed", {}, {"category": "contract",
                                    "message": f"output {o.slot} is not a regular file inside staging"}, final=True)
            digest, size = sha256_file(src)
            if digest != o.digest.replace("sha256:", "") or size != o.size:
                return self._finish(project, ctx, "failed", {}, {"category": "contract",
                                    "message": f"output {o.slot} does not match its declared digest/size"}, final=True)
            try:
                self.store.put_file(src, expected_digest=digest, expected_size=size)
            except StoreError as exc:
                return self._finish(project, ctx, "failed", {}, {"category": "resource", "message": str(exc)}, final=True)
            outputs.append(o)
        if outputs:
            md = man.to_dict()
            md["outputs"] = [dict(o.__dict__) for o in outputs]
            self.client.command("publish_outputs", {"job_id": ctx["job_id"], "attempt_id": ctx["attempt_id"],
                                "epoch": ctx["epoch"], "manifest": md}, project=project, key=f"publish:{ctx['attempt_id']}")
        if ctx.get("stop_requested") and man.status == "succeeded":
            receipt = {**(man.receipt or {}), "stopped_by_policy": "checkpoint-and-stop"}
            return self._finish(project, ctx, "cancelled", receipt, None)
        return self._finish(project, ctx, man.status, man.receipt, man.error, final=man.status == "failed")

    def _collect_build(self, project: str, ctx: dict, adir: Path) -> dict:
        try:
            report = json.loads((adir / "stdout.log").read_text().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError):
            report = {"passed": False, "manifest_problems": ["validator produced no report"],
                      "stderr_tail": (adir / "stderr.log").read_text()[-2000:] if (adir / "stderr.log").exists() else ""}
        report["fixtures_run"] = sum(1 for f in report.get("fixtures", []) if not f.get("skipped"))
        data = json.dumps(report, sort_keys=True).encode()
        digest, size = self.store.put_bytes(data)
        man = {"wire": "eidos-wire/1", "job_id": ctx["job_id"], "attempt_id": ctx["attempt_id"], "epoch": ctx["epoch"],
               "status": "succeeded", "outputs": [{"slot": "validation_report", "filename": "validation_report.json",
                                                   "digest": digest, "size": size, "media_type": "application/json",
                                                   "schema": "component-validation-report/1"}], "receipt": report}
        self.client.command("publish_outputs", {"job_id": ctx["job_id"], "attempt_id": ctx["attempt_id"],
                            "epoch": ctx["epoch"], "manifest": man}, project=project, key=f"publish:{ctx['attempt_id']}")
        return self._finish(project, ctx, "succeeded", report, None)

    def _finish(self, project: str, ctx: dict, status: str, receipt: dict, error: dict | None, *, final: bool = False) -> dict:
        elapsed = time.time() - ctx["started"]
        res = self.client.command("finish_attempt", {"job_id": ctx["job_id"], "attempt_id": ctx["attempt_id"],
                                  "epoch": ctx["epoch"], "status": status, "receipt": receipt or {}, "error": error,
                                  "usage": {"compute_seconds": round(elapsed, 3)}, "final": final},
                                  project=project, key=f"finish:{ctx['attempt_id']}")["result"]
        if res.get("retry"):
            raise RetryableAttemptError((error or {}).get("message", "retryable failure"))
        return {"job_status": res.get("job_status"), "attempt_id": ctx["attempt_id"], "reattached": ctx["reattached"],
                "fenced": res.get("fenced", False)}
