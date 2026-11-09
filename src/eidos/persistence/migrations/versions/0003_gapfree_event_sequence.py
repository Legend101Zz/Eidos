"""Gap-free event sequence: allocate seq from a single-row counter locked until commit, so sequence
order equals commit order and a reader's cursor can never pass an uncommitted lower sequence.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE IF NOT EXISTS ledger.event_counter (id integer PRIMARY KEY CHECK (id = 1), last_seq bigint NOT NULL);
INSERT INTO ledger.event_counter(id, last_seq) SELECT 1, COALESCE(max(seq), 0) FROM ledger.events
  ON CONFLICT (id) DO NOTHING;
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ledger.event_counter")
