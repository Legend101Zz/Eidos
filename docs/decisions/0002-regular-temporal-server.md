# 0002 — Regular Temporal Server with PostgreSQL persistence

Status: accepted (2026-11-07)

**Decision.** Ship the pinned regular `temporal-server` (1.32.2, sha256-verified download or operator-supplied binaries) with its own PostgreSQL role/databases in the lab-owned cluster; generate a compact 9-port config. `temporal server start-dev` is used only by tests.

**Why.** The design requires persistent, supervised orchestration; start-dev omits security checks and is ephemeral by default. Verified in the sandbox: schema setup via embedded schemas, health, namespace registration, workflows, backup/restore of Temporal persistence.

**Alternatives.** start-dev with --db-filename (rejected for shipping); DBOS (architectural fallback only).

**Reversal.** If the pinned server cannot be qualified on a supported OS, ship an explicitly selected existing Temporal service via configuration rather than start-dev.
