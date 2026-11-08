"""Operator operations: init, start/stop, status, doctor and logs (backup/restore/upgrade live in
``eidos.supervisor.maintenance``)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from eidos.persistence.roles import roles_from_config
from eidos.settings import SERVICE_ROLES, LabConfig
from eidos.supervisor import services as svc


def _token() -> str:
    return "eidos_" + secrets.token_urlsafe(32)


def enroll_principals(cfg: LabConfig, *, rotate: bool = True) -> dict[str, str]:
    """(Re-)issue tokens for the operator and every service principal. Only hashes go to the ledger."""
    from eidos.application.scopes import scopes_for
    from eidos.persistence.ledger import SqlLedger
    ledger = SqlLedger(roles_from_config(cfg).url("writer"), pool_size=1)
    issued = {}
    try:
        with ledger.transaction() as tx:
            entries = {"admin": (["researcher", "admin"], ["sealed:write"], "human")}
            entries.update({k: (v[0], v[1], "service") for k, v in SERVICE_ROLES.items()})
            for pid, (roles, extra, kind) in entries.items():
                if not rotate and cfg.secret_path(f"{pid}.token").exists():
                    continue
                tok = _token()
                tx.upsert_principal(pid, kind, pid, ["*"], scopes_for(roles) + extra,
                                    hashlib.sha256(tok.encode()).hexdigest(), {"roles": roles})
                cfg.write_secret(f"{pid}.token", tok)
                issued[pid] = roles
    finally:
        ledger.dispose()
    return issued


def ensure_candidate_user() -> bool:
    from eidos.runtime.launcher import CANDIDATE_USER
    if os.geteuid() != 0:
        return False
    import pwd
    try:
        pwd.getpwnam(CANDIDATE_USER)
        return True
    except KeyError:
        r = subprocess.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", CANDIDATE_USER],
                           capture_output=True)
        return r.returncode == 0


def init_lab(home: str, *, api_port: int = 8765, pg_port: int = 5544, temporal_port: int = 7633,
             pg_bin: str = "", temporal_source: str | None = None, db_name: str = "eidos") -> LabConfig:
    root = Path(home)
    if (root / "config" / "lab.json").exists():
        raise svc.ServiceError(f"{home} is already an Eidos lab")
    for d in ("config", "services", "artifacts", "jobs", "logs", "run", "backups", "envs", "reports", "cache"):
        (root / d).mkdir(parents=True, exist_ok=True)
    os.chmod(root / "config", 0o700)
    if os.geteuid() == 0:
        os.chmod(root, 0o755)
    cfg = LabConfig(home=str(root), api_port=api_port, pg_port=pg_port, temporal_port=temporal_port,
                    pg_bin=svc.find_pg_bin(pg_bin), lab_python=sys.executable, db_name=db_name)
    cfg.write_secret("pg-superuser", secrets.token_urlsafe(24))
    cfg.write_secret("db.json", json.dumps({k: secrets.token_urlsafe(24) for k in ("migrator", "writer", "reader", "temporal")}))
    svc.pg_init(cfg, cfg.read_secret("pg-superuser"))
    svc.pg_start(cfg)
    try:
        from eidos.persistence.roles import migrate, provision
        provision(svc.admin_url(cfg), roles_from_config(cfg))
        migrate(roles_from_config(cfg))
        enroll_principals(cfg)
        cfg.temporal_bin = svc.install_temporal(cfg, temporal_source)
        svc.temporal_setup(cfg)
    finally:
        svc.pg_stop(cfg)
    from eidos.runtime.launcher import detect_sealed_profile
    cfg.sealed_profile = detect_sealed_profile() or ""
    cfg.sealed_available = bool(cfg.sealed_profile)
    cfg.extra["candidate_user"] = ensure_candidate_user()
    cfg.save()
    return cfg


def start(cfg: LabConfig, *, wait: float = 120.0) -> dict:
    pid = supervisor_pid(cfg)
    if pid:
        return {"already_running": True, "pid": pid}
    log = open(cfg.p("logs", "supervisor.log"), "ab")
    subprocess.Popen([cfg.lab_python or sys.executable, "-m", "eidos.apps.role", "supervisor"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, env={**os.environ, "EIDOS_HOME": str(cfg.root)}, start_new_session=True)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        st = status_file(cfg)
        if st.get("phase") == "running":
            return st
        if str(st.get("phase", "")).startswith("failed"):
            raise svc.ServiceError(st["phase"])
        time.sleep(0.5)
    raise svc.ServiceError("supervisor did not reach 'running'; see logs/supervisor.log")


def stop(cfg: LabConfig, *, wait: float = 90.0) -> dict:
    pid = supervisor_pid(cfg)
    if not pid:
        svc.pg_stop(cfg)
        return {"was_running": False}
    os.kill(pid, signal.SIGTERM)
    end = time.monotonic() + wait
    while time.monotonic() < end and supervisor_pid(cfg):
        time.sleep(0.3)
    return {"was_running": True, "stopped": supervisor_pid(cfg) is None}


def supervisor_pid(cfg: LabConfig) -> int | None:
    try:
        pid = int(cfg.p("run", "supervisor.pid").read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def status_file(cfg: LabConfig) -> dict[str, Any]:
    try:
        return json.loads(cfg.p("run", "status.json").read_text())
    except (OSError, ValueError):
        return {}


def status(cfg: LabConfig) -> dict[str, Any]:
    st = status_file(cfg)
    st["supervisor_running"] = supervisor_pid(cfg) is not None
    st["home"] = str(cfg.root)
    st["api_url"] = cfg.api_url
    try:
        from eidos.client import LabClient
        c = LabClient(cfg.api_url, cfg.token("admin"), timeout=5)
        st["api"] = c.health()
        st["projects"] = []
        for pr in c.query("list_projects"):
            summ = c.query("project_summary", project=pr["id"])
            st["projects"].append(summ)
        c.close()
    except Exception as exc:
        st["api"] = {"status": "unreachable", "error": str(exc)[:200]}
    usage = shutil.disk_usage(cfg.root)
    st["disk"] = {"free_bytes": usage.free, "reserve_bytes": cfg.min_free_bytes, "pressure": usage.free < cfg.min_free_bytes * 2}
    return st


def doctor(cfg: LabConfig) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name, ok, detail, level="fail"):
        checks.append({"check": name, "status": "ok" if ok else level, "detail": detail})

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        add("postgres-binaries", True, svc.pg_version(cfg.pg_bin))
    except Exception as exc:
        add("postgres-binaries", False, str(exc))
    add("postgres-running", svc.pg_running(cfg), str(svc.pg_data(cfg)))
    if svc.pg_running(cfg):
        try:
            from eidos.persistence.roles import current_revision
            add("ledger-migration", current_revision(roles_from_config(cfg)) is not None, f"revision {current_revision(roles_from_config(cfg))}")
        except Exception as exc:
            add("ledger-migration", False, str(exc)[:300])
    tbin = Path(cfg.temporal_bin) / "temporal-server"
    add("temporal-binary", tbin.exists(), f"{tbin} (pinned {svc.TEMPORAL_VERSION}, regular server, PostgreSQL persistence)")
    st = status(cfg)
    api = st.get("api", {})
    add("api", api.get("status") == "ok", json.dumps(api)[:300])
    relay = (api or {}).get("relay") or {}
    add("outbox-relay", bool(relay) and relay.get("errors", 0) == 0, json.dumps(relay)[:300], level="warn")
    for name, s in (st.get("services") or {}).items():
        add(f"service:{name}", s.get("state") == "running", f"pid={s.get('pid')} restarts={s.get('restarts')}",
            level="fail" if name in ("postgres", "temporal", "api") else "warn")
    add("disk", not st["disk"]["pressure"], f"free {st['disk']['free_bytes'] >> 20} MiB, reserve {cfg.min_free_bytes >> 20} MiB",
        level="warn")
    from eidos.runtime.launcher import bwrap_available, candidate_uid, seatbelt_available
    if sys.platform == "darwin":
        ok = seatbelt_available()
        add("sealed-profile:seatbelt", ok, "macos:seatbelt-sealed " + ("available (sandbox-exec; same OS user; per-run "
            "probes decide which controls hold)" if ok else "unavailable — plans requiring sealed execution cannot be "
            "admitted"), level="warn")
    else:
        add("sealed-profile:bwrap", bwrap_available(), "linux:bwrap-sealed " + ("available" if bwrap_available() else
            "unavailable — plans requiring sealed execution cannot be admitted"), level="warn")
    add("sealed-profile:separate-identity", candidate_uid() is not None,
        "distinct candidate OS user exists; whether it can run each project interpreter is checked at launch and "
        "reported per receipt" if candidate_uid() else "no distinct candidate user (separate-identity control will fail)",
        level="warn")
    if sys.platform == "darwin":
        add("sealed-profile:lima-vm", False, "macOS Lima VM profile is not qualified in this release", level="warn")
    secrets_dir = cfg.p("config", "secrets")
    add("secret-permissions", all((p.stat().st_mode & 0o077) == 0 for p in secrets_dir.iterdir()), str(secrets_dir))
    return checks


def tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            return b"\n".join(fh.read().splitlines()[-n:]).decode(errors="replace")
    except OSError:
        return ""


def logs(cfg: LabConfig, service: str | None, n: int = 100) -> str:
    if service:
        return tail(cfg.p("logs", f"{service}.log"), n)
    return "\n".join(f"== {p.stem} ==\n{tail(p, min(n, 20))}" for p in sorted(cfg.p("logs").glob("*.log")))
