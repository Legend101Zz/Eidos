"""G7 (v1.0.0): an old-version ledger writer still running after an upgrade (migration 0003's event
counter, and every later migration) must not corrupt or collide. Migration 0004 fences writers that
do not declare the current writer generation; the current command service declares it."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.pg


def _old_writer_insert_event(conn) -> None:
    # What a pre-0003 writer did: take seq from the bigserial default, no generation declared.
    conn.execute(text("INSERT INTO ledger.events(project_id, type, subject, body) "
                      "VALUES ('_system', 'old.writer', 's', CAST(:b AS jsonb))"), {"b": json.dumps({})})


def _counter(roles) -> int:
    eng = create_engine(roles.url("reader"))
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT last_seq FROM ledger.event_counter WHERE id = 1")).scalar()
    finally:
        eng.dispose()


def test_writer_without_generation_is_fenced_and_current_writer_is_not(pg_roles):
    from eidos.persistence.ledger import SqlLedger
    eng = create_engine(pg_roles.url("writer"))
    try:
        before = _counter(pg_roles)
        with pytest.raises(Exception, match="eidos ledger writer fenced"), eng.begin() as c:
            _old_writer_insert_event(c)
        # an old writer's UPDATE of mutable state too
        with pytest.raises(Exception, match="writer fenced"), eng.begin() as c:
            c.execute(text("UPDATE ledger.event_counter SET last_seq = last_seq + 1 WHERE id = 1"))
        assert _counter(pg_roles) == before
        led = SqlLedger(pg_roles.url("writer"), pool_size=1)
        try:
            with led.transaction() as tx:
                seq = tx.append_event("_system", "fence.ok", "s", {})
            assert seq == before + 1
        finally:
            led.dispose()
    finally:
        eng.dispose()


def test_old_writer_connected_across_the_upgrade_is_refused_after_it(tmp_path):
    """A writer connected to a 0003 ledger keeps running while ``lab upgrade`` migrates to head:
    its next write is refused (no seq collision, no corrupt row) while the new writer proceeds."""
    from conftest import ADMIN, _pg_available
    if not _pg_available():
        pytest.skip("PostgreSQL not available")
    from eidos.persistence.ledger import SqlLedger
    from eidos.persistence.roles import DbRoles, current_revision, migrate, provision
    db = f"eidos_f_{uuid.uuid4().hex[:8]}"
    roles = DbRoles.generate("127.0.0.1", int(ADMIN.rsplit(":", 1)[1].split("/")[0]), db, prefix=f"ef{uuid.uuid4().hex[:6]}")
    provision(ADMIN, roles)
    try:
        migrate(roles, target="0003")
        assert current_revision(roles) == "0003"
        old = create_engine(roles.url("writer"), pool_size=1, max_overflow=0)
        with old.begin() as c:  # the old writer works before the upgrade (with the 0003 counter it used)
            c.execute(text("UPDATE ledger.event_counter SET last_seq = last_seq + 1 WHERE id = 1"))
        with old.begin() as c:
            c.exec_driver_sql("SELECT 1")  # keep a pooled, already-open connection across the upgrade
        migrate(roles)
        assert current_revision(roles) == "0004"
        before = _counter(roles)
        with pytest.raises(Exception, match="writer fenced"), old.begin() as c:
            _old_writer_insert_event(c)
        assert _counter(roles) == before
        new = SqlLedger(roles.url("writer"), pool_size=1)
        try:
            with new.transaction() as tx:
                tx.c.execute(text("UPDATE event_counter SET last_seq = last_seq + 1 WHERE id = 1"))
        finally:
            new.dispose()
        assert _counter(roles) == before + 1
        reader = create_engine(roles.url("reader"))
        with reader.connect() as c:
            assert c.execute(text("SELECT count(*) FROM ledger.events WHERE type = 'old.writer'")).scalar() == 0
            assert c.execute(text("SELECT min_generation FROM ledger.writer_fence")).scalar() == 4
        reader.dispose()
        old.dispose()
    finally:
        eng = create_engine(ADMIN, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
            for r in ("migrator", "writer", "reader"):
                c.exec_driver_sql(f'DROP ROLE IF EXISTS "{roles.prefix}_{r}"')
        eng.dispose()
