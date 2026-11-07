"""Managed local services: PostgreSQL (existing installation's binaries, lab-owned cluster) and
the *regular* Temporal Server (pinned release, PostgreSQL persistence) — not ``start-dev``."""

from __future__ import annotations

import glob
import hashlib
import os
import platform
import pwd
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

from eidos.settings import LabConfig

TEMPORAL_VERSION = "1.32.2"
TEMPORAL_RELEASES = {
    ("linux", "amd64"): ("https://github.com/temporalio/temporal/releases/download/v1.32.2/temporal_1.32.2_linux_amd64.tar.gz",
                         "6260f110b6853d2d7f8daa31793d1fac44096077e3b058dc3482de6ac50bc543"),
}
PG_MIN_MAJOR = 14


class ServiceError(Exception):
    pass


# --------------------------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------------------------

def find_pg_bin(explicit: str = "") -> str:
    cands = [explicit] if explicit else []
    which = shutil.which("pg_ctl")
    if which:
        cands.append(str(Path(which).parent))
    cands += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    cands += sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"), reverse=True)
    cands += sorted(glob.glob("/usr/local/opt/postgresql@*/bin"), reverse=True)
    cands += ["/Applications/Postgres.app/Contents/Versions/latest/bin"]
    for c in cands:
        if c and (Path(c) / "pg_ctl").exists() and (Path(c) / "initdb").exists():
            return c
    raise ServiceError(f"PostgreSQL >= {PG_MIN_MAJOR} binaries not found; install PostgreSQL or pass --pg-bin")


def pg_version(pg_bin: str) -> str:
    return subprocess.run([f"{pg_bin}/postgres", "--version"], capture_output=True, text=True).stdout.strip()


def _as_pg_user(argv: list[str]) -> list[str]:
    """PostgreSQL refuses to run as root; when the lab runs as root use the 'postgres' account."""
    if os.geteuid() == 0:
        return ["runuser", "-u", "postgres", "--", *argv]
    return argv


def pg_data(cfg: LabConfig) -> Path:
    return cfg.p("services", "pg", "data")


def pg_init(cfg: LabConfig, superuser_password: str) -> None:
    data = pg_data(cfg)
    if (data / "PG_VERSION").exists():
        return
    data.parent.mkdir(parents=True, exist_ok=True)
    pwfile = data.parent / ".pw"
    pwfile.write_text(superuser_password)
    if os.geteuid() == 0:
        pw = pwd.getpwnam("postgres")
        os.chown(data.parent, pw.pw_uid, pw.pw_gid)
        os.chown(pwfile, pw.pw_uid, pw.pw_gid)
    os.chmod(pwfile, 0o600)
    try:
        r = subprocess.run(_as_pg_user([f"{cfg.pg_bin}/initdb", "-D", str(data), "-U", "postgres", "-A", "scram-sha-256",
                                        f"--pwfile={pwfile}", "--encoding=UTF8"]), capture_output=True, text=True)
    finally:
        pwfile.unlink(missing_ok=True)
    if r.returncode != 0:
        raise ServiceError(f"initdb failed: {r.stderr[-2000:]}")
    conf = data / "postgresql.conf"
    with open(conf, "a") as fh:
        fh.write(f"\n# eidos\nlisten_addresses = '127.0.0.1'\nport = {cfg.pg_port}\nunix_socket_directories = '{data.parent}'\n"
                 "max_connections = 100\nshared_buffers = 128MB\nfsync = on\nsynchronous_commit = on\n")


def pg_start(cfg: LabConfig) -> None:
    if pg_running(cfg):
        return
    log = cfg.p("logs", "postgres.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        log.touch()
        pw = pwd.getpwnam("postgres")
        os.chown(log, pw.pw_uid, pw.pw_gid)
    r = subprocess.run(_as_pg_user([f"{cfg.pg_bin}/pg_ctl", "-D", str(pg_data(cfg)), "-l", str(log), "-w", "-t", "60", "start"]),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ServiceError(f"pg_ctl start failed: {r.stderr[-1000:] or r.stdout[-1000:]}")


def pg_stop(cfg: LabConfig, mode: str = "fast") -> None:
    if not pg_running(cfg):
        return
    subprocess.run(_as_pg_user([f"{cfg.pg_bin}/pg_ctl", "-D", str(pg_data(cfg)), "-m", mode, "-w", "stop"]),
                   capture_output=True, text=True)


def pg_running(cfg: LabConfig) -> bool:
    if not (pg_data(cfg) / "postmaster.pid").exists():
        return False
    r = subprocess.run(_as_pg_user([f"{cfg.pg_bin}/pg_ctl", "-D", str(pg_data(cfg)), "status"]), capture_output=True, text=True)
    return r.returncode == 0


def admin_url(cfg: LabConfig) -> str:
    pw = cfg.read_secret("pg-superuser")
    return f"postgresql+psycopg://postgres:{pw}@127.0.0.1:{cfg.pg_port}/postgres"


# --------------------------------------------------------------------------------------------
# Temporal (regular server, PostgreSQL persistence)
# --------------------------------------------------------------------------------------------

def temporal_platform() -> tuple[str, str]:
    osname = platform.system().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), "")
    return osname, arch


def install_temporal(cfg: LabConfig, source: str | None = None) -> str:
    """Install the pinned server binaries into the lab (or use an explicit existing directory)."""
    dest = cfg.p("services", "temporal", "bin")
    if (dest / "temporal-server").exists():
        return str(dest)
    if source and Path(source, "temporal-server").exists():
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("temporal-server", "temporal-sql-tool"):
            shutil.copy2(Path(source, name), dest / name)
        return str(dest)
    key = temporal_platform()
    url = TEMPORAL_RELEASES.get(key, (None,))[0] or (
        f"https://github.com/temporalio/temporal/releases/download/v{TEMPORAL_VERSION}/"
        f"temporal_{TEMPORAL_VERSION}_{key[0]}_{key[1]}.tar.gz")
    dest.mkdir(parents=True, exist_ok=True)
    tgz = dest.parent / "server.tgz"
    if not tgz.exists():
        urllib.request.urlretrieve(url, tgz)
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    pinned = TEMPORAL_RELEASES.get(key, (None, None))[1]
    if pinned and digest != pinned:
        tgz.unlink()
        raise ServiceError(f"temporal server download digest {digest} != pinned {pinned}")
    (dest.parent / "server.sha256").write_text(f"{digest}  {url}\n")
    with tarfile.open(tgz) as tf:
        for member in ("temporal-server", "temporal-sql-tool"):
            tf.extract(member, dest, filter="data")
    tgz.unlink()
    return str(dest)


def temporal_config_yaml(cfg: LabConfig, user: str, password: str) -> str:
    b = cfg.temporal_port
    svc = {"frontend": (b, b + 100, b + 10), "history": (b + 1, b + 101, None), "matching": (b + 2, b + 102, None),
           "worker": (b + 3, b + 103, None)}
    services = ""
    for name, (grpc, member, http) in svc.items():
        services += f"  {name}:\n    rpc:\n      grpcPort: {grpc}\n      membershipPort: {member}\n      bindOnLocalHost: true\n"
        if http:
            services += f"      httpPort: {http}\n"

    def store(db: str, conns: int) -> str:
        return (f"      sql:\n        pluginName: \"postgres12\"\n        databaseName: \"{db}\"\n"
                f"        connectAddr: \"127.0.0.1:{cfg.pg_port}\"\n        connectProtocol: \"tcp\"\n"
                f"        user: \"{user}\"\n        password: \"{password}\"\n        maxConns: {conns}\n"
                f"        maxIdleConns: {conns}\n        maxConnLifetime: \"1h\"\n")
    return f"""log:
  stdout: true
  level: warn
persistence:
  defaultStore: postgres-default
  visibilityStore: postgres-visibility
  numHistoryShards: 4
  datastores:
    postgres-default:
{store(cfg.db_name + '_temporal', 10)}    postgres-visibility:
{store(cfg.db_name + '_temporal_visibility', 2)}global:
  membership:
    maxJoinDuration: 30s
    broadcastAddress: "127.0.0.1"
services:
{services}clusterMetadata:
  enableGlobalNamespace: false
  failoverVersionIncrement: 10
  masterClusterName: "active"
  currentClusterName: "active"
  clusterInformation:
    active:
      enabled: true
      initialFailoverVersion: 1
      rpcName: "frontend"
      rpcAddress: "127.0.0.1:{b}"
dcRedirectionPolicy:
  policy: "noop"
dynamicConfigClient:
  filepath: "config/dynamicconfig/eidos.yaml"
  pollInterval: "10s"
"""


DYNAMIC_CONFIG = """# Eidos: keep histories bounded and limits explicit.
limit.historyCount.warn:
  - value: 20000
limit.blobSize.warn:
  - value: 524288
"""


def temporal_setup(cfg: LabConfig) -> None:
    """Create Temporal's databases/role (separate from the research ledger) and its schema."""
    import json as _json

    from sqlalchemy import create_engine, text
    secrets = _json.loads(cfg.read_secret("db.json"))
    user, pw = f"{cfg.db_prefix}_temporal", secrets["temporal"]
    eng = create_engine(admin_url(cfg), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:n"), {"n": user}).first()
        c.exec_driver_sql(f"{'ALTER' if exists else 'CREATE'} ROLE \"{user}\" LOGIN PASSWORD '{pw}'")
        for db in (cfg.db_name + "_temporal", cfg.db_name + "_temporal_visibility"):
            if not c.execute(text("SELECT 1 FROM pg_database WHERE datname=:d"), {"d": db}).first():
                c.exec_driver_sql(f'CREATE DATABASE "{db}" OWNER "{user}"')
            c.exec_driver_sql(f'REVOKE ALL ON DATABASE "{db}" FROM PUBLIC')
            c.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{db}" TO "{user}"')
    eng.dispose()
    tool = str(Path(cfg.temporal_bin) / "temporal-sql-tool")
    env = {**os.environ, "SQL_PLUGIN": "postgres12", "SQL_HOST": "127.0.0.1", "SQL_PORT": str(cfg.pg_port),
           "SQL_USER": user, "SQL_PASSWORD": pw}
    for db, schema in ((cfg.db_name + "_temporal", "postgresql/v12/temporal"),
                       (cfg.db_name + "_temporal_visibility", "postgresql/v12/visibility")):
        subprocess.run([tool, "--db", db, "setup-schema", "-v", "0.0"], env=env, capture_output=True, text=True)
        r = subprocess.run([tool, "--db", db, "update-schema", "--schema-name", schema], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise ServiceError(f"temporal schema update failed for {db}: {r.stderr[-1500:]}")
    root = cfg.p("services", "temporal")
    (root / "config" / "dynamicconfig").mkdir(parents=True, exist_ok=True)
    cfgfile = root / "config" / "eidos.yaml"
    cfgfile.write_text(temporal_config_yaml(cfg, user, pw))
    os.chmod(cfgfile, 0o600)
    (root / "config" / "dynamicconfig" / "eidos.yaml").write_text(DYNAMIC_CONFIG)


def temporal_argv(cfg: LabConfig) -> list[str]:
    return [str(Path(cfg.temporal_bin) / "temporal-server"), "--root", str(cfg.p("services", "temporal")),
            "--config", "config", "--env", "eidos", "--allow-no-auth", "start"]


async def temporal_ready(cfg: LabConfig, timeout: float = 60.0) -> bool:
    import asyncio

    from temporalio.client import Client
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            c = await Client.connect(cfg.temporal_target, namespace="temporal-system")
            await c.service_client.check_health()
            return True
        except Exception:
            await asyncio.sleep(1)
    return False


async def ensure_namespace(cfg: LabConfig, retention_days: int = 30) -> None:
    from datetime import timedelta

    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
    from temporalio.client import Client
    from temporalio.service import RPCError
    c = await Client.connect(cfg.temporal_target, namespace="temporal-system")
    try:
        await c.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=cfg.temporal_namespace))
    except RPCError:
        from google.protobuf.duration_pb2 import Duration
        d = Duration()
        d.FromTimedelta(timedelta(days=retention_days))
        await c.workflow_service.register_namespace(RegisterNamespaceRequest(
            namespace=cfg.temporal_namespace, workflow_execution_retention_period=d))
