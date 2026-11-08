"""macOS Seatbelt candidate-sandbox probes (GitHub-runner evidence, not researcher-machine qualification).

Builds a throwaway lab-shaped layout and drives the trusted launcher directly:

1. hostile-access probes under ``macos:seatbelt-sealed`` (every sealed control must pass);
2. the same probes under ``dev:same-user`` as a planted-effect control: the accesses must succeed
   there, otherwise the probes are vacuous;
3. a permitted computation under the sandbox: read its private input copy, write staging, scratch,
   resume and its private tmp, and confirm writes to the project root are refused.

A live listener stands in for the lab API so the control-socket probe cannot pass merely because
nothing is listening; egress reachability from the host is checked before it is claimed denied.
Writes ``$OUT/seatbelt_probes.json``; exits non-zero on any failure. Usage: python seatbelt_probes.py
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from eidos.domain.isolation import SEALED_CONTROLS
from eidos.runtime.launcher import (
    LaunchSpec,
    credential_dirs,
    launch,
    run_probes,
    seatbelt_available,
    seatbelt_policy,
)

OUT = Path(os.environ.get("OUT", ".")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
result: dict = {"evidence": "GitHub-hosted macOS runner; not researcher-machine qualification",
                "platform": platform.platform(), "machine": platform.machine(),
                "sw_vers": subprocess.run(["sw_vers"], capture_output=True, text=True).stdout.strip()
                if sys.platform == "darwin" else None, "failures": []}


def finish() -> None:
    (OUT / "seatbelt_probes.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    sys.exit(1 if result["failures"] else 0)


result["seatbelt_available"] = seatbelt_available()
if not result["seatbelt_available"]:
    result["failures"].append("sandbox-exec unusable on this host; runs stay dev:same-user with recorded waivers")
    finish()

base = Path(tempfile.mkdtemp(prefix="eidos-seatbelt-")).resolve()
lab = base / "lab"
for d in ("config", "secrets", "artifacts"):
    (lab / d).mkdir(parents=True)
canary = lab / "config" / "label-canary"
canary.write_text("planted label")
cred = lab / "secrets" / "admin.token"
cred.write_text("planted credential")
project = base / "project"
project.mkdir()
adir = lab / "jobs" / "j1" / "a1"
staging, scratch, resume = adir / "staging", adir / "scratch", lab / "jobs" / "j1" / "resume"
for d in (staging, scratch, resume, adir / "inputs"):
    d.mkdir(parents=True)
inp = adir / "inputs" / "data"
inp.write_text("input bytes")

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(8)
api_port = srv.getsockname()[1]


def _accept() -> None:
    while True:
        try:
            c, _ = srv.accept()
            c.close()
        except OSError:
            return


threading.Thread(target=_accept, daemon=True).start()
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    result["host_egress_reachable"] = True
except OSError:
    result["host_egress_reachable"] = False


def spec(profile: str, attempt_dir: Path, argv: list[str] | None = None) -> LaunchSpec:
    return LaunchSpec(argv=argv or [sys.executable], cwd=str(project), attempt_dir=str(attempt_dir), profile=profile,
                      readonly=[str(project), str(inp)], writable=[str(staging), str(scratch), str(resume)],
                      python_prefixes=sorted({sys.prefix, sys.base_prefix}), deny_read=[str(lab), *credential_dirs()],
                      env={"PYTHONHASHSEED": "0"})


def probes(profile: str, attempt_dir: Path) -> dict:
    return run_probes(spec(profile, attempt_dir), label_path=str(canary), credential_path=str(cred),
                      host_write_path=str(lab / "config" / "probe-host-write"), api_port=api_port)


text, params = seatbelt_policy(spec("macos:seatbelt-sealed", adir), str(adir / "tmp"))
result["policy"] = {"text": text, "params": params}
sealed = probes("macos:seatbelt-sealed", adir)
result["sealed_probes"] = sealed
for c in SEALED_CONTROLS:
    if sealed.get("controls", {}).get(c) != "passed":
        result["failures"].append(f"seatbelt control {c} = {sealed.get('controls', {}).get(c)} ({sealed.get('probe_error', '')[:300]})")

neg = probes("dev:same-user", base / "negative")
result["negative_control_probes"] = neg
expect_fail = ["no-label-read", "no-credential-read", "no-host-write", "no-control-socket"]
if result["host_egress_reachable"]:
    expect_fail.append("no-network-egress")
undetected = [c for c in expect_fail if neg.get("controls", {}).get(c) != "failed"]
if undetected:
    result["failures"].append(f"planted-effect control: probes did not detect access without the sandbox: {undetected}")

WORK = r"""
import json, os, pathlib, sys
cfg = json.loads(sys.argv[1]); r = {}
r["input"] = pathlib.Path(cfg["input"]).read_text()
for k in ("staging", "scratch", "resume"):
    p = pathlib.Path(cfg[k]) / "w.txt"; p.write_text(k); r[k] = p.read_text() == k
t = pathlib.Path(os.environ["TMPDIR"]) / "t.txt"; t.write_text("t"); r["tmp"] = t.read_text() == "t"
def refused(fn):
    try:
        fn(); return False
    except OSError:
        return True
r["project_write_refused"] = refused(lambda: (pathlib.Path(cfg["project"]) / "x.txt").write_text("x"))
r["artifact_dir_listing_refused"] = refused(lambda: os.listdir(cfg["artifacts"]))
r["home_ssh_listing_refused_or_absent"] = refused(lambda: os.listdir(os.path.expanduser("~/.ssh")))
(pathlib.Path(cfg["staging"]) / "result.json").write_text(json.dumps(r))
print(json.dumps(r))
"""
cfg = {"input": str(inp), "staging": str(staging), "scratch": str(scratch), "resume": str(resume),
       "project": str(project), "artifacts": str(lab / "artifacts")}
h = launch(spec("macos:seatbelt-sealed", adir / "work", [sys.executable, "-I", "-c", WORK, json.dumps(cfg)]))
deadline = time.monotonic() + 60
while h.exit_code() is None and time.monotonic() < deadline:
    time.sleep(0.05)
work = {"exit_code": h.exit_code(), "stderr": (adir / "work" / "stderr.log").read_text()[-1500:]}
try:
    work.update(json.loads((staging / "result.json").read_text()))
except (OSError, ValueError) as exc:
    work["error"] = str(exc)
result["permitted_computation"] = work
ok = (work.get("exit_code") == 0 and work.get("input") == "input bytes" and all(work.get(k) for k in
      ("staging", "scratch", "resume", "tmp", "project_write_refused", "artifact_dir_listing_refused")))
if not ok:
    result["failures"].append("permitted computation under the sandbox did not behave as specified")
if (project / "x.txt").exists():
    result["failures"].append("project root write escaped the sandbox")
srv.close()
finish()
