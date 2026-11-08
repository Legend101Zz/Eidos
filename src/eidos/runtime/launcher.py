"""Trusted process launcher for research attempts.

* ``dev:same-user``: a host process in the project's own interpreter (exploratory work only).
* ``linux:bwrap-sealed``: bubblewrap with no network, read-only inputs, a private /tmp, cleared
  environment, and (when the lab runs as root and the user exists) a distinct OS identity.
* ``macos:seatbelt-sealed``: ``sandbox-exec`` with a generated Seatbelt profile: no network, no reads
  of the lab home or credential directories (except the attempt's own files), writes only to
  staging/scratch/resume and a private tmp, cleared environment. Same OS user; mach IPC is not
  filtered. ``sandbox-exec`` is deprecated by Apple but still shipped; probes decide what holds.

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
        if sys.platform == "darwin":  # boot time identifies the boot on macOS
            try:
                return subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True).stdout.strip() or "unknown"
            except OSError:
                return "unknown"
        return "unknown"


def proc_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):  # zombie/dead: the process no longer runs
            return None
        return fields[19]
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

    def signal_group(self, sig: int) -> None:
        if self.alive():
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass

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
    deny_read: list[str] = field(default_factory=list)  # Seatbelt: contents unreadable unless listed above


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


# Variables a research process may inherit on the same-user profile; everything else (provider keys,
# cloud credentials, lab tokens) is dropped. Sealed profiles start from an empty environment.
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ", "TMPDIR", "PYTHONHASHSEED",
             "OMP_NUM_THREADS", "MKL_NUM_THREADS", "CUDA_VISIBLE_DEVICES", "PYTORCH_ENABLE_MPS_FALLBACK",
             "PYTORCH_MPS_HIGH_WATERMARK_RATIO", "UV_CACHE_DIR")
ENV_ALLOW_PREFIXES = ("LC_",)
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "SESSION")


def job_environment(environ) -> dict[str, str]:
    return {k: v for k, v in environ.items()
            if (k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)) and not k.startswith("EIDOS_")}


def scrub_secrets(environ) -> dict[str, str]:
    """Drop variables that look like credentials (for lab services that never call a model)."""
    return {k: v for k, v in environ.items() if k.startswith("EIDOS_") or (
            not any(m in k.upper() for m in SECRET_MARKERS) and not k.startswith(("AWS_", "GOOGLE_", "AZURE_")))}


class IsolationUnavailable(RuntimeError):
    pass


def run_contained(argv: list[str], *, cwd: str, writable: list[str], readonly: list[str] = (), python: str | None = None,
                  timeout: float = 300, allow_same_user: bool = False, env: dict[str, str] | None = None,
                  attempt_dir: str | None = None) -> dict[str, Any]:
    """Run an untrusted (e.g. model-authored) command under the strongest available profile.

    Sealed profiles: empty environment plus ``env``, no network, writes only to ``writable``, the user's
    home and credential stores unreadable (except listed paths). Without a sealed profile the call is
    refused unless ``allow_same_user`` is set; then the environment is still the allowlist (no secrets)."""
    import tempfile
    profile = detect_sealed_profile()
    waiver = None
    if not profile:
        if not allow_same_user:
            raise IsolationUnavailable("no sealed profile (bwrap/seatbelt) on this host; pass allow_same_user to "
                                       "run untrusted commands as the same user (recorded as a waiver)")
        profile, waiver = "dev:same-user", "allow_same_user requested; no sealed profile available"
    adir = attempt_dir or tempfile.mkdtemp(prefix="eidos-contained-")
    prefixes: list[str] = []
    if python and profile != "dev:same-user":
        out = subprocess.run([python, "-I", "-c", "import json,sys; print(json.dumps([p for p in sys.path if p]))"],
                             capture_output=True, text=True, env=job_environment(os.environ))
        prefixes = sorted(set(json.loads(out.stdout) if out.returncode == 0 else [])
                          | {str(Path(python).resolve().parent.parent), str(Path(python).parent.parent)})
    spec = LaunchSpec(argv=argv, cwd=cwd, attempt_dir=adir, env=dict(env or {}), profile=profile,
                      readonly=list(readonly), writable=list(writable), python_prefixes=prefixes,
                      deny_read=sealed_deny_read() if profile != "dev:same-user" else [])
    h = launch(spec)
    deadline = time.monotonic() + timeout
    while h.exit_code() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    timed_out = h.exit_code() is None
    if timed_out:
        h.terminate(grace=2)
    read = lambda n: (Path(adir) / n).read_text(errors="replace") if (Path(adir) / n).exists() else ""  # noqa: E731
    return {"returncode": h.exit_code() if not timed_out else None, "timed_out": timed_out, "stdout": read("stdout.log"),
            "stderr": read("stderr.log"), "profile": profile, "waiver": waiver,
            "config_digest": profile_config_digest(spec)}


SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def seatbelt_available() -> bool:
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return False
    r = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)(deny network*)", "/usr/bin/true"],
                       capture_output=True)
    return r.returncode == 0


def detect_sealed_profile() -> str | None:
    """The sealed profile this host can provide, if any (probes still decide per receipt)."""
    if bwrap_available():
        return "linux:bwrap-sealed"
    if seatbelt_available():
        return "macos:seatbelt-sealed"
    return None


def sealed_deny_read(*roots: str | None) -> list[str]:
    """Read-denied roots for a sealed launch: the given lab roots, the whole home directory and the
    user credential stores. Paths the launch itself needs (project root, interpreter prefixes, inputs,
    its own attempt directory) are re-allowed beneath these roots by the profile builder."""
    return [*(str(r) for r in roots if r), str(Path.home()), *credential_dirs()]


def credential_dirs() -> list[str]:
    home = Path.home()
    return [str(home / d) for d in (".ssh", ".aws", ".gnupg", ".netrc", ".pgpass", ".git-credentials", ".docker",
                                    ".kube", ".config/gcloud", ".config/gh", ".config/git", "Library/Keychains")]


def seatbelt_policy(spec: LaunchSpec, tmp: str) -> tuple[str, list[str]]:
    """Seatbelt profile text plus ``-D`` parameters (paths are passed as parameters, never spliced).

    Reads of each ``deny_read`` root are refused except beneath the attempt's own paths that lie
    *inside* that root (an ancestor such as a project root containing the lab home re-allows nothing).
    Writes are refused everywhere except staging/scratch/resume, a private tmp, /dev and the logs.
    """
    params: list[str] = []

    def norm(x: str) -> str:
        return os.path.realpath(x) if os.path.isabs(x) else x

    def par(prefix: str, path: str) -> str:
        params.append(f"{prefix}{len(params)}={path}")
        return f'(param "{prefix}{len(params) - 1}")'

    writable = [norm(w) for w in spec.writable + [tmp]]
    allowed = list(dict.fromkeys(norm(x) for x in spec.python_prefixes + spec.readonly + [spec.cwd] + writable if x))
    rules = ["(version 1)", "(allow default)", "(deny network*)",
             # other processes' arguments/environments (lab daemons may hold provider keys) and signals
             "(deny process-info* (target others))",
             # well-known ways to start work outside the sandbox or reach credential services
             '(deny process-exec (literal "/bin/launchctl") (literal "/usr/bin/osascript") (literal "/usr/bin/open"))',
             '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd"))']
    for d in dict.fromkeys(norm(x) for x in spec.deny_read if x):
        inner = [a for a in allowed if a != d and a.startswith(d.rstrip("/") + "/")]
        target = f"(subpath {par('D', d)})"
        if inner:
            target = (f"(require-all {target} (require-not (require-any "
                      + " ".join(f"(subpath {par('R', a)})" for a in inner) + ")))")
        rules.append(f"(deny file-read-data file-read-xattr {target})")
    keep = ['(subpath "/dev")'] + [f"(subpath {par('W', w)})" for w in dict.fromkeys(writable)]
    keep += [f"(literal {par('L', norm(str(Path(spec.attempt_dir) / f)))})" for f in ("stdout.log", "stderr.log")]
    rules.append("(deny file-write* (require-not (require-any " + " ".join(keep) + ")))")
    return "\n".join(rules), params


def seatbelt_argv(spec: LaunchSpec, tmp: str) -> list[str]:
    text, params = seatbelt_policy(spec, tmp)
    a = [SANDBOX_EXEC, "-p", text]
    for kv in params:
        a += ["-D", kv]
    return a + spec.argv


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
    elif spec.profile == "macos:seatbelt-sealed":
        tmp = adir / "tmp"
        tmp.mkdir(exist_ok=True)
        inner = seatbelt_argv(spec, str(tmp))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "TMPDIR": str(tmp), **spec.env}
    else:
        inner = spec.argv
        env = job_environment(os.environ)  # allowlist: no lab credentials, provider keys or other secrets
        env.update(spec.env)
    # The wrapper ignores SIGUSR1 (the checkpoint-and-stop signal is for the worker, which installs
    # its own handler) so it survives to record the exit status.
    wrapper = ["/bin/sh", "-c", 'trap "" USR1; "$@"; echo $? > "$0"', exit_file, *inner]
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
# the lab credential, and the user's home directory (dotfiles, client tokens, keys) as a whole
res["no-credential-read"] = denied(lambda: open(cfg["credential_path"], "rb").read(1)) and \
    (not cfg.get("home") or denied(lambda: os.listdir(cfg["home"])))
def write_host():
    with open(cfg["host_write_path"], "w") as f:
        f.write("x")
res["no-host-write"] = denied(write_host)
def net():
    s = socket.create_connection((cfg["net_host"], cfg["net_port"]), timeout=2); s.close()
res["no-network-egress"] = denied(net)
res["no-control-socket"] = denied(lambda: socket.create_connection(("127.0.0.1", cfg["api_port"]), timeout=2).close())
res["no-scorer-capability"] = not any(k.startswith("EIDOS_") for k in os.environ)
def peer_hidden():
    data = ""
    try:
        data = open("/proc/%d/environ" % cfg["peer_pid"], "rb").read().decode(errors="ignore")
    except Exception:
        try:
            import subprocess
            data = subprocess.run(["/bin/ps", "eww", "-o", "command=", "-p", str(cfg["peer_pid"])],
                                  capture_output=True, text=True, timeout=5).stdout
        except Exception:
            pass
    return cfg["peer_marker"] not in data
res["no-process-inspection"] = peer_hidden()
res["uid"] = os.getuid()
res["computation"] = sum(range(10)) == 45
print(json.dumps(res))
"""


def run_probes(spec: LaunchSpec, *, label_path: str, credential_path: str, host_write_path: str, api_port: int,
               net_host: str = "1.1.1.1", net_port: int = 443) -> dict[str, Any]:
    """Run hostile-access probes with the exact profile configuration; return a receipt fragment."""
    import secrets as _secrets
    marker = "peer-" + _secrets.token_hex(8)
    # A same-user peer holding a marker in its environment, like a lab daemon holding a provider key.
    peer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env={**os.environ, "EIDOS_PEER": marker},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = {"label_path": label_path, "credential_path": credential_path, "host_write_path": host_write_path,
           "net_host": net_host, "net_port": net_port, "api_port": api_port, "peer_pid": peer.pid, "peer_marker": marker,
           "home": str(Path.home())}
    python = spec.argv[0]
    probe_spec = LaunchSpec(argv=[python, "-I", "-c", PROBE, json.dumps(cfg)], cwd=spec.cwd,
                            attempt_dir=str(Path(spec.attempt_dir) / "probe"), env={}, profile=spec.profile,
                            readonly=spec.readonly, writable=[], python_prefixes=spec.python_prefixes,
                            deny_read=spec.deny_read)
    try:
        h = launch(probe_spec)
        deadline = time.monotonic() + 60
        while h.exit_code() is None and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        peer.kill()
        peer.wait()
    try:
        out = json.loads((Path(probe_spec.attempt_dir) / "stdout.log").read_text().strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        err = (Path(probe_spec.attempt_dir) / "stderr.log").read_text()[-2000:] if (Path(probe_spec.attempt_dir) / "stderr.log").exists() else ""
        return {"probe_error": err or "probe produced no result", "controls": {}}
    controls: dict[str, str] = {}
    for key in ("no-label-read", "no-credential-read", "no-network-egress", "no-control-socket", "no-scorer-capability",
                "no-process-inspection"):
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
    shape = LaunchSpec(argv=["<cmd>"], cwd=spec.cwd, attempt_dir="<attempt>", profile=spec.profile,
                       readonly=sorted(spec.readonly), writable=["<staging>", "<scratch>"],
                       python_prefixes=sorted(spec.python_prefixes), deny_read=sorted(spec.deny_read))
    if spec.profile == "linux:bwrap-sealed":
        cfg = bwrap_argv(shape)
    elif spec.profile == "macos:seatbelt-sealed":
        cfg = list(seatbelt_policy(shape, "<tmp>"))
    else:
        cfg = ["host-process"]
    return _digest(json.dumps(cfg))
