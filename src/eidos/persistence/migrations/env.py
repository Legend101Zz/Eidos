"""Alembic environment. Migrations run only under the dedicated migration role."""

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
url = config.get_main_option("sqlalchemy.url")


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        _run(connectable)
        return
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.begin() as conn:
        _run(conn)


def _run(conn) -> None:
    conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS ledger")
    context.configure(connection=conn, version_table="alembic_version", version_table_schema="ledger",
                      transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
