# 0005 — External task bundles through adapters; exposure boundary

Status: accepted (2026-11-07)

**Decision.** `eidos.tasks.bundle` imports `eidos-task-bundle/0.1` directories (hash-verified, immutable, exploration-only charter because the bundle fixes no final margin) and exports `eidos-answer/0.2`. Task projects run unchanged through the whole-project command component; validation scoring uses a generic scorer. The investigator receives only declared bundle inputs and the catalog; development references, qualification records and test labels are never registered as investigator inputs. Builder-side use of labelled graders is out of scope for this session (denied by the sandbox policy).

**Why.** Keeps the research contract external to Eidos internals and keeps benchmark knowledge out of the product (no task IDs or methods in code).

**Reversal.** New interface versions get new adapters; old run records stay with their release identity.
