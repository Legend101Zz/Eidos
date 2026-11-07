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

## Previously open, now fixed (round 5, operator items 1–8)

| Item | Status | Evidence |
|---|---|---|
| Candidate code defaulted to same-user and could read lab secrets | **Fixed.** Jobs and component builds default to the strongest profile the lab has (bubblewrap on Linux). A weaker profile needs an explicit `isolation_waiver`, which is pinned in the lock, carried into the plan's realized conditions and recorded in the assessment (`isolation_waivers`). | `test_isolation_policy.py`. Live: a default job ran `linux:bwrap-sealed`, and its launcher probe failed to read the real `admin.token` (`no-credential-read: passed`). Torch training runs under the default. |
| Assessor ran unsandboxed | **Fixed.** The assessment release runs through the launcher in bubblewrap where available. It has read-only access to the release, evidence and lab interpreter, write access to its own staging only, no secrets or environment, and an evidence size cap. Its probes and profile are recorded as `assessor_isolation` in the assessment. | `test_assessor_sandbox.py` |
| No in-turn model-budget check | **Fixed.** Each decision's maximum envelope is reserved before any request. `BUDGET_EXHAUSTED` ends the slot with no model call. Usage settles against the reservation on success and failure; leaked reservations are released at turn completion; unbudgeted usage is still written as entries. | `test_turn_recovery.py::test_in_turn_model_budget...`. Live run: 0 tokens left reserved after the investigation. |
| Event-sequence gaps | **Fixed (gap-free).** Sequence numbers come from a counter row locked until commit (migration 0003), so sequence order is commit order and `horizon()` is the last committed seq. Consumers need no gap tolerance. | `test_event_sequence.py`: it failed before the fix and passes now. |
| `build_context` retry conflicts | **Fixed.** Inputs are horizon-pure: budgets from the immutable turn spec and the catalog as of the horizon. Keys derive from the content digest, and the turn context is first-write-wins. | `test_turn_recovery.py::test_context_build_retry...` |
| Generic reads recorded no exposure | **Fixed.** `get_record`, `list_records`, `record_history`, `get_job`, `list_jobs` and `investigation_view` record exposures for assessment, qualification, evidence-plan and reassessment records and for confirmation-arm job receipts. | `test_review_findings_b.py::test_generic_reads...` |
| Idle poll started a model turn | **Fixed.** A quiet timeout runs `check_news` (a ledger query, no model). A turn starts only on a wake, news since the last horizon, or `max_idle_turn_seconds` (6 h). The change is version-guarded with `workflow.patched`. | `test_workflows.py::test_idle_polls_without_news...` (plus frozen-history replay) |
| Component tests only local | **Fixed (pending CI confirmation).** A `components` CI job runs all 59 tests with zero skips allowed. | `.github/workflows/ci.yml`, `scripts/ci/components.sh` |

## Still open

- **macOS** has no sealed profile, so candidate code there runs same-user. This is recorded as `strongest_available: dev:same-user` with a note in every lock. The Lima VM profile is not implemented.
- **Distinct candidate identity** requires a candidate-readable interpreter (e.g. under /opt). Otherwise `separate-identity` reports failed.
- **Live Temporal timeout/retry semantics** for the inferred items A5/A7/A9–A11 are covered by unit/integration tests with fakes, not by a long-running live timeout exercise.
