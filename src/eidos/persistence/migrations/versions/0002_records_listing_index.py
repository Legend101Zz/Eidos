"""Index for project/kind listings ordered by creation (catalog, claims, plans); forward-only.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS records_project_kind_created ON ledger.records (project_id, kind, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ledger.records_project_kind_created")
