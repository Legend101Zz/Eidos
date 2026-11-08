"""Backup, restore and upgrade.

Backup quiesces new commands/publications and outbox dispatch (maintenance flag), takes a
consistent watermark (ledger dump + Temporal persistence dumps), copies and *verifies* every
referenced blob, then resumes. Running research processes keep running under the detach policy;
work they publish after the watermark is not in the bundle and is reconciled after restore.

Credentials are never copied into a bundle: restore issues new role passwords and re-enrolls
every principal with fresh tokens (documented re-enrollment).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from eidos.settings import LabConfig
from eidos.supervisor import services as svc

FORMAT = "eidos-backup/1"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _pg_env(cfg: LabConfig) -> dict:
    return {**os.environ, "PGPASSWORD": cfg.read_secret("pg-superuser")}


def _dump(cfg: LabConfig, db: str, dest: Path) -> None:
    r = subprocess.run([f"{cfg.pg_bin}/pg_dump", "-h", "127.0.0.1", "-p", str(cfg.pg_port), "-U", "postgres", "-Fc",
                        "-f", str(dest), db], env=_pg_env(cfg), capture_output=True, text=True)
    if r.returncode != 0:
        raise svc.ServiceError(f"pg_dump {db} failed: {r.stderr[-1500:]}")


def _restore(cfg: LabConfig, db: str, src: Path, role: str) -> None:
    r = subprocess.run([f"{cfg.pg_bin}/pg_restore", "-h", "127.0.0.1", "-p", str(cfg.pg_port), "-U", "postgres",
                        "--no-owner", f"--role={role}", "--exit-on-error", "-d", db, str(src)],
                       env=_pg_env(cfg), capture_output=True, text=True)
    if r.returncode != 0:
        raise svc.ServiceError(f"pg_restore {db} failed: {r.stderr[-1500:]}")


def maintenance_flag(cfg: LabConfig) -> Path:
    return cfg.p("run", "maintenance")


def backup(cfg: LabConfig, dest: Path | None = None, *, settle_seconds: float = 2.0) -> dict[str, Any]:
    if not svc.pg_running(cfg):
        raise svc.ServiceError("PostgreSQL is not running; start the lab first")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = Path(dest or cfg.p("backups", f"eidos-backup-{stamp}"))
    dest.mkdir(parents=True, exist_ok=False)
    flag = maintenance_flag(cfg)
    flag.write_text(json.dumps({"reason": "backup", "since": time.time()}))
    try:
        time.sleep(settle_seconds)  # let in-flight transactions finish; new ones get 503 MAINTENANCE
        from sqlalchemy import create_engine, text

        from eidos.persistence.roles import roles_from_config
        eng = create_engine(roles_from_config(cfg).url("reader"))
        with eng.connect() as c:
            watermark = c.execute(text("SELECT COALESCE(max(seq),0) FROM ledger.events")).scalar()
            revision = c.execute(text("SELECT version_num FROM ledger.alembic_version")).scalar()
            blobs = [dict(r) for r in c.execute(text("SELECT DISTINCT digest, size FROM ledger.artifacts")).mappings()]
            running = [dict(r) for r in c.execute(text(
                "SELECT id, status, current_attempt FROM ledger.jobs WHERE status IN ('running','queued','admitted')")).mappings()]
        eng.dispose()
        dbs = {"ledger": cfg.db_name, "temporal": cfg.db_name + "_temporal",
               "temporal_visibility": cfg.db_name + "_temporal_visibility"}
        for name, db in dbs.items():
            _dump(cfg, db, dest / f"{name}.dump")
    finally:
        flag.unlink(missing_ok=True)
    # Blobs are immutable and content-addressed, so copying after resuming is consistent with the
    # watermark: every referenced digest existed before it and cannot change.
    from eidos.runtime.artifacts import LocalArtifactStore
    store = LocalArtifactStore(cfg.p("artifacts"), min_free_bytes=0)
    bdir = dest / "blobs"
    bdir.mkdir()
    missing, copied = [], 0
    for b in blobs:
        src = Path(store.path_for(b["digest"]))
        if not store.verify(b["digest"], b["size"]):
            missing.append(b["digest"])
            continue
        shutil.copyfile(src, bdir / b["digest"])
        if _sha(bdir / b["digest"]) != b["digest"]:
            missing.append(b["digest"])
            continue
        copied += 1
    if missing:
        shutil.rmtree(dest)
        raise svc.ServiceError(f"backup incomplete: {len(missing)} referenced blobs missing or corrupt: {missing[:5]}")
    public_cfg = {k: v for k, v in json.loads(cfg.p("config", "lab.json").read_text()).items()}
    manifest = {
        "format": FORMAT, "created_utc": stamp, "eidos_version": cfg.version, "ledger_revision": revision,
        "event_watermark": watermark, "temporal_version": svc.TEMPORAL_VERSION, "postgres": svc.pg_version(cfg.pg_bin),
        "config": public_cfg, "running_jobs_at_watermark": running, "blob_count": copied,
        "files": {p.name: _sha(p) for p in sorted(dest.glob("*.dump"))},
        "credentials": "not included; restore re-enrolls all principals with new tokens and role passwords",
        "loss_window": "commands committed after the watermark are not in this bundle",
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return {"backup": str(dest), "event_watermark": watermark, "blobs": copied, "running_jobs": len(running)}


def verify_bundle(bundle: Path) -> dict:
    man = json.loads((bundle / "manifest.json").read_text())
    if man.get("format") != FORMAT:
        raise svc.ServiceError(f"unsupported backup format {man.get('format')}")
    for name, digest in man["files"].items():
        if _sha(bundle / name) != digest:
            raise svc.ServiceError(f"backup file {name} fails its checksum")
    blobs = list((bundle / "blobs").iterdir())
    if len(blobs) != man["blob_count"]:
        raise svc.ServiceError("blob count mismatch")
    for b in blobs:
        if _sha(b) != b.name:
            raise svc.ServiceError(f"blob {b.name} corrupt")
    return man


def restore(bundle: Path, home: str, *, api_port: int = 8765, pg_port: int = 5544, temporal_port: int = 7633) -> dict:
    """Restore into a fresh lab directory, then re-enroll credentials. Scheduling resumes only at
    `lab start`, where the reconciler re-dispatches unfinished work and the runner treats handles
    from another boot as lost."""
    from eidos.settings import check_ports
    try:
        check_ports(api_port, pg_port, temporal_port)
    except ValueError as exc:
        raise svc.ServiceError(str(exc)) from None
    import secrets as _secrets
    import sys
    bundle = Path(bundle)
    man = verify_bundle(bundle)
    root = Path(home)
    if (root / "config" / "lab.json").exists():
        raise svc.ServiceError(f"{home} already contains a lab; restore needs a fresh directory")
    for d in ("config", "services", "artifacts", "jobs", "logs", "run", "backups", "envs", "reports", "cache"):
        (root / d).mkdir(parents=True, exist_ok=True)
    os.chmod(root / "config", 0o700)
    old = man["config"]
    cfg = LabConfig(home=str(root), api_port=api_port, pg_port=pg_port, temporal_port=temporal_port,
                    pg_bin=svc.find_pg_bin(old.get("pg_bin", "")), lab_python=sys.executable,
                    db_name=old.get("db_name", "eidos"), db_prefix=old.get("db_prefix", "eidos"),
                    min_free_bytes=old.get("min_free_bytes", 512 << 20))
    cfg.write_secret("pg-superuser", _secrets.token_urlsafe(24))
    cfg.write_secret("db.json", json.dumps({k: _secrets.token_urlsafe(24) for k in ("migrator", "writer", "reader", "temporal")}))
    svc.pg_init(cfg, cfg.read_secret("pg-superuser"))
    svc.pg_start(cfg)
    try:
        from eidos.persistence.roles import apply_grants, provision, roles_from_config
        roles = roles_from_config(cfg)
        provision(svc.admin_url(cfg), roles)
        _restore(cfg, cfg.db_name, bundle / "ledger.dump", f"{cfg.db_prefix}_migrator")
        apply_grants(roles)
        cfg.temporal_bin = svc.install_temporal(cfg, old.get("temporal_bin"))
        svc.temporal_setup(cfg)  # creates role/dbs (+schema if empty); then load the persisted state
        from sqlalchemy import create_engine
        eng = create_engine(svc.admin_url(cfg), isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            for db in (cfg.db_name + "_temporal", cfg.db_name + "_temporal_visibility"):
                c.exec_driver_sql(f'DROP DATABASE "{db}" WITH (FORCE)')
                c.exec_driver_sql(f'CREATE DATABASE "{db}" OWNER "{cfg.db_prefix}_temporal"')
        eng.dispose()
        _restore(cfg, cfg.db_name + "_temporal", bundle / "temporal.dump", f"{cfg.db_prefix}_temporal")
        _restore(cfg, cfg.db_name + "_temporal_visibility", bundle / "temporal_visibility.dump", f"{cfg.db_prefix}_temporal")
        from eidos.runtime.artifacts import LocalArtifactStore
        store = LocalArtifactStore(cfg.p("artifacts"), min_free_bytes=0)
        for b in (bundle / "blobs").iterdir():
            d, _ = store.put_file(b, expected_digest=b.name)
        from eidos.supervisor.ops import enroll_principals
        cfg.save()
        enroll_principals(cfg)
    finally:
        svc.pg_stop(cfg)
    from eidos.runtime.launcher import detect_sealed_profile
    cfg.sealed_profile = detect_sealed_profile() or ""
    cfg.sealed_available = bool(cfg.sealed_profile)
    cfg.extra["restored_from"] = {"bundle": str(bundle), "event_watermark": man["event_watermark"],
                                  "created_utc": man["created_utc"]}
    cfg.save()
    return {"home": str(root), "event_watermark": man["event_watermark"], "blobs": man["blob_count"],
            "running_jobs_at_watermark": man["running_jobs_at_watermark"],
            "next": f"EIDOS_HOME={root} lab start   (reconciles unfinished jobs before scheduling)"}


def upgrade(cfg: LabConfig) -> dict:
    """Apply ledger migrations (compatible, forward-only) and restart services so new workers
    replay in-flight workflows. Old scientific records are never rewritten."""
    from eidos.persistence.roles import current_revision, migrate, roles_from_config
    from eidos.supervisor.ops import start, stop, supervisor_pid
    was_running = supervisor_pid(cfg) is not None
    if was_running:
        stop(cfg)
    svc.pg_start(cfg)
    before = current_revision(roles_from_config(cfg))
    migrate(roles_from_config(cfg))
    after = current_revision(roles_from_config(cfg))
    import sys

    import eidos
    from eidos.runtime.launcher import detect_sealed_profile
    previous_python = cfg.lab_python
    cfg.version = eidos.__version__
    cfg.lab_python = sys.executable  # services restart on the installation that ran the upgrade
    cfg.sealed_profile = detect_sealed_profile() or ""
    cfg.sealed_available = bool(cfg.sealed_profile)
    cfg.save()
    if was_running:
        start(cfg)
    else:
        svc.pg_stop(cfg)
    return {"ledger_revision": {"before": before, "after": after}, "version": cfg.version, "restarted": was_running,
            "lab_python": {"before": previous_python, "after": cfg.lab_python}}
