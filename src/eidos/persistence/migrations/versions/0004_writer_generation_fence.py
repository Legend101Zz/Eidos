"""Writer-generation fence: after an upgrade, a ledger writer from an older Eidos version that is
still running (a manual partial upgrade, a stray API process) must not corrupt or collide with the
new schema. Every write statement on a ledger table requires the session to declare a writer
generation at least ``writer_fence.min_generation``. The current command service declares it per
transaction (``SqlLedger.transaction``); writers that predate the fence declare nothing and are
refused with a clear error instead of, e.g., inserting an event sequence number the counter of
migration 0003 also hands out. The schema owner (migrations) is exempt.

A later migration that changes write semantics raises ``min_generation`` together with
``eidos.persistence.ledger.WRITER_GENERATION``.

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

GENERATION = 4
TABLES = ("records", "events", "budget_entries", "lineage", "publications", "exposures", "decisions", "artifacts",
          "projects", "principals", "commands", "heads", "outbox", "jobs", "attempts", "budgets", "turns",
          "event_counter")


def upgrade() -> None:
    op.execute(f"""
CREATE TABLE IF NOT EXISTS ledger.writer_fence (
  id integer PRIMARY KEY CHECK (id = 1),
  min_generation integer NOT NULL,
  set_by text NOT NULL,
  set_at timestamptz NOT NULL DEFAULT now());
INSERT INTO ledger.writer_fence(id, min_generation, set_by) VALUES (1, {GENERATION}, 'migration 0004')
  ON CONFLICT (id) DO UPDATE SET min_generation = GREATEST(ledger.writer_fence.min_generation, EXCLUDED.min_generation),
                                 set_by = EXCLUDED.set_by, set_at = now();
CREATE OR REPLACE FUNCTION ledger.require_writer_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  need integer;
  have integer;
BEGIN
  IF current_user = (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'ledger')
     OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    RETURN NULL;  -- migrations and the database administrator
  END IF;
  SELECT min_generation INTO need FROM ledger.writer_fence WHERE id = 1;
  have := COALESCE(NULLIF(current_setting('eidos.writer_generation', true), ''), '0')::integer;
  IF need IS NOT NULL AND have < need THEN
    RAISE EXCEPTION 'eidos ledger writer fenced: this writer declares generation %, the ledger requires % '
                    '(an older Eidos version is still running after an upgrade; stop it and use lab upgrade)',
                    have, need USING ERRCODE = 'P0001', HINT = 'eidos-writer-fenced';
  END IF;
  RETURN NULL;
END $$;
""")
    for t in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_writer_fence ON ledger.{t}")
        op.execute(f"CREATE TRIGGER {t}_writer_fence BEFORE INSERT OR UPDATE OR DELETE ON ledger.{t} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION ledger.require_writer_generation()")


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_writer_fence ON ledger.{t}")
    op.execute("DROP FUNCTION IF EXISTS ledger.require_writer_generation()")
    op.execute("DROP TABLE IF EXISTS ledger.writer_fence")
