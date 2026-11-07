# Adversarial review, 2026-11-07 (source 55fd0e2)

Two read-only reviewer passes covered invariants (a) single write path, (b) idempotency and fencing, (c) evaluation separation, (d) budget accounting, and (e) the turn-workflow change. Each **confirmed** finding was reproduced by an executed test and fixed with a regression test (`tests/integration/test_review_findings.py`, `test_review_findings_b.py`, `test_workflows.py::test_inflight_turn_with_invalid_proposal_replays_after_upgrade`).

## Fixed (confirmed)

| # | Finding | Fix |
|---|---|---|
| A1 | A principal in another project could overwrite an investigation's budget limit (scope keyed by investigation id only) | Budget scopes are owned by one project. An existing limit is not overwritten. The `budgets` query is project-scoped (A4). |
| A2 | A late `finish_attempt` could rewrite a terminal job and double-count trials | Terminal guard on finish, publish and outcome-unknown. Late usage is still recorded. |
| A3 | Turn commands did not check the turn's project | `_turn()` helper with a project check on every turn command |
| A5 | A re-dispatched JobWorkflow could replay a stale start receipt | Start keys include the workflow run id |
| B1 (critical) | Sealed-labels artifacts could be bound into any candidate job and exfiltrated through its outputs | Restricted inputs need the matching read grant and a sealed profile. Outputs inherit the most restrictive input class. The launcher re-checks. Restricted ids are not broadcast in events. |
| B2 | The investigator could pre-register a job's output artifact id with forged bytes | The lab mints artifact ids. Publication requires byte, size and producer match. |
| B3 | Same-user default isolation. Receipts were not bound to the job profile and could be overwritten by any launcher | `required_isolation` is an explicit plan obligation (template 1.1.0 too). Receipt profile must match the plan and the job. Receipts are write-once. Sealed trust requires `sealed:read`. |
| B4 | A crashed candidate could forge a success manifest and "complete" checkpoints | Success needs exit code 0. `max_output_bytes` is enforced by the launcher. Only checkpoint slots the component declares are branchable. |
| B5 | `project_root` or the interpreter inside the lab directory made the probes pass while store bytes were readable | Refused at registration and again at launch |
| B6 | Invalid-proposal `continue` broke replay of in-flight turns | `workflow.patched("invalid-proposal-feedback-v1")`. The pre-change history is frozen as a fixture. |
| B7 | All-invalid turns started the next turn immediately (hot loop) | At most 2 consecutive invalid slots per turn, then stop reason `invalid-proposals`, which waits like other wait reasons |

## Fixed (inferred, low-risk)

- **USD charged on every usage path:** bounded outcomes and failures now carry cost.
- **Model usage recorded before decision acceptance:** this is the first durable write.
- **Reattached attempts keep their original clock:** a runner restart no longer resets the wall-clock limit or compute accounting.
- **Retries exhausted before any start:** the job is terminalized and its reservation released.
- **Cross-project job references are refused:** export, publish and qualification can no longer cite another project's jobs.
- **Fenced or orphaned attempts report their compute:** they finish through the normal path instead of disappearing.
- **project.command:** `returncode` is in the job receipt.

## Open (recorded, not fixed)

- **No in-turn model-budget gate:** `begin_turn` is the only gate. Unbudgeted dimensions record no usage rows.
- **Events may be skipped:** sequence gaps under concurrent transactions can let `horizon()`/cursor readers miss a late-committed lower seq.
- **`build_context` retry:** it can conflict if live budget or job fields change between upload and record. The catalog snapshot is not horizon-pinned.
- **`dev:same-user` reach:** candidate code can read lab secrets under this profile, by design. Investigator-authored code defaults to it.
- **Assessor isolation:** the assessor runs approved release code unsandboxed in the worker environment, and prediction files have no size cap.
- **Exposure records:** generic record reads (`get_record`, `list_records`, `investigation_view`) of assessments and evidence-arm receipts record none.
- **Idle polling:** an idle-poll wake starts a model turn even when nothing changed.
- **Not exercised:** no end-to-end sealed run with a distinct candidate identity on a host where it applies, and no live Temporal timeout/retry exercise of items A5/A7/A9–A11.
