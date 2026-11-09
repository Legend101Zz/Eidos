# 0003 — Authority by PostgreSQL grants; generic revisioned records

Status: accepted (2026-11-07)

**Decision.** Scientific objects (charters, claims, plans, assessments, releases, profiles, branches, …) are project-scoped append-only revisions (`records` + `heads`); mechanics with stronger invariants (commands, events, outbox, jobs/attempts with fencing, artifacts/publications/lineage, budgets, turns/decisions, exposures) have dedicated tables. Only the command service holds the writer role; append-only tables have INSERT-only grants plus triggers. Workers, investigator, assessor and launchers use scoped API tokens and no DB credentials.

**Why.** One authoritative write path that tests can attack directly (tests/integration/test_command_path.py). A generic record store keeps ~50 commands compact; domain validation is in pure functions.

**Found during build.** Record ids were initially global; component refs reused in another project collided. Records/heads are now keyed by project.

**Reversal.** Promote a record kind to a dedicated table when it needs relational constraints or query performance.
