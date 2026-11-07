"""Trusted process launcher for research attempts.

* ``dev:same-user``: a host process in the project's own interpreter (exploratory work only).
* ``linux:bwrap-sealed``: bubblewrap with no network, read-only inputs, a private /tmp, cleared
  environment, and (when the lab runs as root and the user exists) a distinct OS identity.

Processes are started in their own session and survive a launcher restart. Identity is checked
with (pid, kernel start time, boot id) — never the PID alone — and the exit code is written to
a file by a shell wrapper so a reattached launcher can still learn it.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CANDIDATE_USER = "eidos-cand"


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def proc_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        if sys.platform == "darwin":  # pragma: no cover - macOS fallback
            try:
                return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or None
            except OSError:
                return None
        return None


@dataclass
class ProcessHandle:
    pid: int
    start_time: str | None
    boot_id: str
    attempt_dir: str
    exit_file: str
    profile: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> ProcessHandle:
        return ProcessHandle(**{k: d[k] for k in ProcessHandle.__dataclass_fields__})

    def alive(self) -> bool:
        if self.boot_id != boot_id():
            return False
        st = proc_start_time(self.pid)
        return st is not None and st == self.start_time and not Path(self.exit_file).exists()

    def exit_code(self) -> int | None:
        try:
            return int(Path(self.exit_file).read_text().strip())
        except (OSError, ValueError):
            return None

    def terminate(self, grace: float = 5.0) -> None:
        if not self.alive():
            return
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and self.alive():
            time.sleep(0.1)
        if self.alive():
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@dataclass
class LaunchSpec:
    argv: list[str]
    cwd: str
    attempt_dir: str
    env: dict[str, str] = field(default_factory=dict)
    profile: str = "dev:same-user"
    readonly: list[str] = field(default_factory=list)
    writable: list[str] = field(default_factory=list)
    python_prefixes: list[str] = field(default_factory=list)


_CAND_OK: dict[str, bool] = {}


def candidate_uid(python: str | None = None) -> tuple[int, int] | None:
    """The distinct candidate identity, if it exists *and* can run the project interpreter."""
    if os.geteuid() != 0:
        return None
    try:
        pw = pwd.getpwnam(CANDIDATE_USER)
    except KeyError:
        return None
    if python:
        if python not in _CAND_OK:
            r = subprocess.run(["runuser", "-u", CANDIDATE_USER, "--", python, "-I", "-c", "pass"], capture_output=True)
            _CAND_OK[python] = r.returncode == 0
        if not _CAND_OK[python]:
            return None
    return pw.pw_uid, pw.pw_gid


def bwrap_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent", "true"],
                       capture_output=True)
    return r.returncode == 0


def bwrap_argv(spec: LaunchSpec) -> list[str]:
    a = ["bwrap", "--die-with-parent", "--unshare-all", "--new-session", "--clearenv",
         "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in ("/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime"):
        if os.path.exists(p):
            a += ["--ro-bind", p, p]
    seen = set()
    for p in spec.python_prefixes + spec.readonly + [spec.cwd]:
        if p and os.path.exists(p) and p not in seen:
            seen.add(p)
            a += ["--ro-bind", p, p]
    for p in spec.writable:
        a += ["--bind", p, p]
    a += ["--setenv", "HOME", "/tmp", "--setenv", "PATH", "/usr/bin:/bin"]
    for k, v in spec.env.items():
        a += ["--setenv", k, v]
    a += ["--chdir", spec.cwd, "--"]
    return a + spec.argv


def launch(spec: LaunchSpec) -> ProcessHandle:
    adir = Path(spec.attempt_dir)
    adir.mkdir(parents=True, exist_ok=True)
    exit_file = str(adir / "exit_code")
    out = open(adir / "stdout.log", "ab")
    err = open(adir / "stderr.log", "ab")
    if spec.profile == "linux:bwrap-sealed":
        inner = bwrap_argv(spec)
        env = {"PATH": "/usr/bin:/bin"}
    else:
        inner = spec.argv
        env = {k: v for k, v in os.environ.items() if not k.startswith("EIDOS_")}  # never leak lab credentials
        env.update(spec.env)
    wrapper = ["/bin/sh", "-c", '"$@"; echo $? > "$0"', exit_file, *inner]
    preexec = None
    ids = candidate_uid(spec.argv[0]) if spec.profile == "linux:bwrap-sealed" else None
    if ids:
        uid, gid = ids
        os.chown(adir, uid, gid)

        def preexec():  # pragma: no cover - runs in the child
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
    proc = subprocess.Popen(wrapper, cwd=spec.cwd if spec.profile != "linux:bwrap-sealed" else str(adir),
                            stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                            start_new_session=True, preexec_fn=preexec)
    out.close()
    err.close()
    h = ProcessHandle(proc.pid, proc_start_time(proc.pid), boot_id(), str(adir), exit_file, spec.profile)
    (adir / "handle.json").write_text(json.dumps(h.to_dict()))
    return h


# ---------------------------------------------------------------------------------------------
# Isolation probes: executed by the trusted launcher inside the *same* sandbox configuration.
# ---------------------------------------------------------------------------------------------

PROBE = r"""
import json, os, socket, sys
cfg = json.loads(sys.argv[1])
res = {}
def denied(fn):
    try:
        fn()
        return False
    except Exception:
        return True
res["no-label-read"] = denied(lambda: open(cfg["label_path"], "rb").read(1))
res["no-credential-read"] = denied(lambda: open(cfg["credential_path"], "rb").read(1))
def write_host():
    with open(cfg["host_write_path"], "w") as f:
        f.write("x")
res["no-host-write"] = denied(write_host)
def net():
    s = socket.create_connection((cfg["net_host"], cfg["net_port"]), timeout=2); s.close()
res["no-network-egress"] = denied(net)
res["no-control-socket"] = denied(lambda: socket.create_connection(("127.0.0.1", cfg["api_port"]), timeout=2).close())
res["no-scorer-capability"] = not any(k.startswith("EIDOS_") for k in os.environ)
res["uid"] = os.getuid()
res["computation"] = sum(range(10)) == 45
print(json.dumps(res))
"""


def run_probes(spec: LaunchSpec, *, label_path: str, credential_path: str, host_write_path: str, api_port: int,
               net_host: str = "1.1.1.1", net_port: int = 443) -> dict[str, Any]:
    """Run hostile-access probes with the exact profile configuration; return a receipt fragment."""
    cfg = {"label_path": label_path, "credential_path": credential_path, "host_write_path": host_write_path,
           "net_host": net_host, "net_port": net_port, "api_port": api_port}
    python = spec.argv[0]
    probe_spec = LaunchSpec(argv=[python, "-I", "-c", PROBE, json.dumps(cfg)], cwd=spec.cwd,
                            attempt_dir=str(Path(spec.attempt_dir) / "probe"), env={}, profile=spec.profile,
                            readonly=spec.readonly, writable=[], python_prefixes=spec.python_prefixes)
    h = launch(probe_spec)
    deadline = time.monotonic() + 60
    while h.exit_code() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        out = json.loads((Path(probe_spec.attempt_dir) / "stdout.log").read_text().strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        err = (Path(probe_spec.attempt_dir) / "stderr.log").read_text()[-2000:] if (Path(probe_spec.attempt_dir) / "stderr.log").exists() else ""
        return {"probe_error": err or "probe produced no result", "controls": {}}
    controls: dict[str, str] = {}
    for key in ("no-label-read", "no-credential-read", "no-network-egress", "no-control-socket", "no-scorer-capability"):
        controls[key] = "passed" if out.get(key) else "failed"
    # Judge host writes from the host side: a write into the sandbox's private tmpfs is not one.
    host_file = Path(host_write_path)
    controls["no-host-write"] = "failed" if host_file.exists() else "passed"
    host_file.unlink(missing_ok=True)
    ids = candidate_uid(python) if spec.profile == "linux:bwrap-sealed" else None
    controls["separate-identity"] = "passed" if ids and ids[0] != os.geteuid() else "failed"
    if not out.get("computation"):
        controls["permitted-computation"] = "failed"
    return {"controls": controls, "probe_uid_inside": out.get("uid"),
            "outer_uid": ids[0] if ids else os.geteuid(), "probe_digest": _digest(PROBE)}


def _digest(s: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def profile_config_digest(spec: LaunchSpec) -> str:
    cfg = bwrap_argv(LaunchSpec(argv=["<cmd>"], cwd=spec.cwd, attempt_dir="", profile=spec.profile,
                                readonly=sorted(spec.readonly), writable=["<staging>", "<scratch>"],
                                python_prefixes=sorted(spec.python_prefixes))) if spec.profile == "linux:bwrap-sealed" else ["host-process"]
    return _digest(json.dumps(cfg))
