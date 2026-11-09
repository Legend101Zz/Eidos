"""Database provisioning: databases, roles, migrations and grants.

Authority over the research ledger is enforced here, by PostgreSQL privileges:

* ``<prefix>_migrator`` owns the schema and runs migrations (audited maintenance only).
* ``<prefix>_writer`` is held only by the command service. It may INSERT into append-only tables
  and UPDATE only mutable state tables; triggers forbid rewriting history even so.
* ``<prefix>_reader`` has SELECT for projections/reports, except principal credential hashes.

Investigator, assessor, workflow, training and sealed-evaluation processes receive *no* database
credentials at all; they submit through the authenticated command API.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

APPEND_ONLY = ("records", "events", "budget_entries", "lineage", "publications", "exposures", "decisions",
               "artifacts", "projects")
MUTABLE = ("principals", "commands", "heads", "outbox", "jobs", "attempts", "budgets", "turns", "event_counter")
MIGRATIONS = Path(__file__).parent / "migrations"


@dataclass(frozen=True)
class DbRoles:
    host: str
    port: int
    database: str
    prefix: str
    migrator_password: str
    writer_password: str
    reader_password: str

    def url(self, role: str) -> URL:
        pw = {"migrator": self.migrator_password, "writer": self.writer_password, "reader": self.reader_password}[role]
        return URL.create("postgresql+psycopg", username=f"{self.prefix}_{role}", password=pw,
                          host=self.host, port=self.port, database=self.database)

    @staticmethod
    def generate(host: str, port: int, database: str, prefix: str = "eidos") -> DbRoles:
        return DbRoles(host, port, database, prefix, secrets.token_urlsafe(24), secrets.token_urlsafe(24),
                       secrets.token_urlsafe(24))


def _q(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def provision(admin_url: URL | str, roles: DbRoles) -> None:
    """Create roles and the ledger database (idempotent). Requires a superuser/admin URL."""
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        for role, pw in (("migrator", roles.migrator_password), ("writer", roles.writer_password),
                         ("reader", roles.reader_password)):
            name = f"{roles.prefix}_{role}"
            exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:n"), {"n": name}).first()
            verb = "ALTER" if exists else "CREATE"
            if not pw.replace("-", "").replace("_", "").isalnum():
                raise ValueError("generated passwords must be url-safe")
            conn.exec_driver_sql(f"{verb} ROLE {_q(name)} LOGIN PASSWORD '{pw}'")
        if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname=:d"), {"d": roles.database}).first():
            conn.exec_driver_sql(f"CREATE DATABASE {_q(roles.database)} OWNER {_q(roles.prefix + '_migrator')}")
        conn.exec_driver_sql(f"REVOKE ALL ON DATABASE {_q(roles.database)} FROM PUBLIC")
        for role in ("migrator", "writer", "reader"):
            conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {_q(roles.database)} TO {_q(roles.prefix + '_' + role)}")
    eng.dispose()


def migrate(roles: DbRoles, target: str = "head") -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", roles.url("migrator").render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, target)
    apply_grants(roles)


def current_revision(roles: DbRoles) -> str | None:
    eng = create_engine(roles.url("reader"))
    try:
        with eng.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM ledger.alembic_version")).first()
            return row[0] if row else None
    finally:
        eng.dispose()


def apply_grants(roles: DbRoles) -> None:
    w, r = _q(roles.prefix + "_writer"), _q(roles.prefix + "_reader")
    eng = create_engine(roles.url("migrator"))
    with eng.begin() as conn:
        conn.exec_driver_sql("REVOKE ALL ON SCHEMA ledger FROM PUBLIC")
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA ledger TO {w}, {r}")
        conn.exec_driver_sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA ledger FROM {w}, {r}")
        for t in APPEND_ONLY:
            conn.exec_driver_sql(f"GRANT SELECT, INSERT ON ledger.{t} TO {w}")
        for t in MUTABLE:
            conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE ON ledger.{t} TO {w}")
        conn.exec_driver_sql(f"GRANT SELECT ON ledger.alembic_version TO {w}, {r}")
        if conn.exec_driver_sql("SELECT to_regclass('ledger.writer_fence')").scalar():  # migration 0004+
            conn.exec_driver_sql(f"GRANT SELECT ON ledger.writer_fence TO {w}, {r}")
        conn.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ledger TO {w}")
        for t in APPEND_ONLY + MUTABLE:
            if t != "principals":
                conn.exec_driver_sql(f"GRANT SELECT ON ledger.{t} TO {r}")
        conn.exec_driver_sql(
            f"GRANT SELECT (id, kind, display, project_ids, scopes, attributes, created_at, expires_at, revoked_at) "
            f"ON ledger.principals TO {r}")
    eng.dispose()


def roles_from_config(cfg) -> DbRoles:
    """Ledger roles for a lab installation (``eidos.settings.LabConfig``)."""
    s = cfg.db_secrets()
    return DbRoles("127.0.0.1", cfg.pg_port, cfg.db_name, cfg.db_prefix, s["migrator"], s["writer"], s["reader"])
