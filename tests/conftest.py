"""Shared fixtures. PostgreSQL-backed tests use EIDOS_TEST_PG_ADMIN (default: the lead's dev
cluster) and create a fresh database per test session."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid

import pytest

ADMIN = os.environ.get("EIDOS_TEST_PG_ADMIN", "postgresql+psycopg://postgres:devpass@127.0.0.1:5432/postgres")


def _pg_available() -> bool:
    try:
        import sqlalchemy
        eng = sqlalchemy.create_engine(ADMIN)
        with eng.connect():
            pass
        eng.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_roles():
    if not _pg_available():
        if os.environ.get("EIDOS_TEST_PG_ADMIN"):  # explicitly configured: an unusable server is a failure, not a skip
            pytest.fail(f"EIDOS_TEST_PG_ADMIN is set but unusable ({ADMIN.split('@')[-1]})")
        pytest.skip("PostgreSQL not available (set EIDOS_TEST_PG_ADMIN)")
    from eidos.persistence.roles import DbRoles, migrate, provision
    db = f"eidos_t_{uuid.uuid4().hex[:8]}"
    roles = DbRoles.generate("127.0.0.1", int(ADMIN.rsplit(":", 1)[1].split("/")[0]), db, prefix=f"et{uuid.uuid4().hex[:6]}")
    provision(ADMIN, roles)
    migrate(roles)
    yield roles
    import sqlalchemy
    eng = sqlalchemy.create_engine(ADMIN, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for r in ("migrator", "writer", "reader"):
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{roles.prefix}_{r}"')
    eng.dispose()


class Lab:
    """A command service wired to a real ledger and store, plus helpers to mint principals."""

    def __init__(self, roles, tmp):
        from eidos.application.service import CommandService, load_handlers
        from eidos.persistence.ledger import SqlLedger
        from eidos.runtime.artifacts import LocalArtifactStore
        load_handlers()
        self.ledger = SqlLedger(roles.url("writer"))
        self.store = LocalArtifactStore(tmp / "artifacts", min_free_bytes=0)
        self.svc = CommandService(self.ledger, self.store)
        self.tokens: dict[str, str] = {}
        self.admin = self.principal("admin", ["admin"], ["*"])

    def principal(self, pid, roles, projects, extra=()):
        from eidos.application.ports import Principal
        from eidos.application.scopes import scopes_for
        token = secrets.token_hex(16)
        with self.ledger.transaction() as tx:
            tx.upsert_principal(pid, "service" if pid != "human" else "human", pid, list(projects),
                                scopes_for(roles) + list(extra), hashlib.sha256(token.encode()).hexdigest(),
                                {"roles": roles})
        p = Principal(pid, "service", frozenset(scopes_for(roles) + list(extra)), frozenset(projects), {"roles": roles})
        self.tokens[pid] = token
        return p

    def cmd(self, principal, name, payload, *, project=None, key=None, rev=None):
        return self.svc.execute(principal, name, project_id=project, command_key=key or uuid.uuid4().hex,
                                payload=payload, expected_revision=rev)

    def q(self, principal, name, params=None, *, project=None):
        return self.svc.query(principal, name, project_id=project, params=params or {})


@pytest.fixture()
def lab(pg_roles, tmp_path):
    lab = Lab(pg_roles, tmp_path)
    yield lab
    lab.ledger.dispose()
