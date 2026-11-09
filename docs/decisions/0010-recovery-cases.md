# 0010: Recovery policies for five interruptions of the design's failure table

Status: accepted (2026-11-09, release 1.1.0).

**Context.** The design's table "Failures a production installation must handle" lists five interruptions that
`ACCEPTANCE_MATRIX.md` § C still recorded as `not-started` in v1.0.1. Some of the mechanics existed (command-key
idempotency, an assessment id derived from plan, evidence and assessor, a rehash in `publish_outputs`, withdrawal
traversal), but none was defined as a recovery policy or tested as one, and three had real gaps:

- a refused publication was an untyped `INVALID` error that escaped the runner as an exception;
- a release withdrawn during confirmation left the plan waiting for an assessment that the suspended approval
  would refuse forever, with no defined way forward;
- the assessor activity used the key `assess:<plan>:<plan digest>`, so a restarted assessor that re-evaluated the
  same evidence (any change in its evaluation payload) got a command-key conflict instead of the existing
  assessment; and an evidence set listed in another order produced a second assessment;
- there was no command to change an investigation's question; and a withdrawn release could still be run by an
  explicit `run_component` (the resolver refused it, but only as a generic resolution error).

**Decision.** One policy per interruption, each with an integration test in
`tests/integration/test_recovery_cases.py`.

1. **Storage or database lag.** `publish_outputs` verifies every output again immediately before inserting any
   reference: the blob must exist, have the declared size and rehash to its digest (`LocalArtifactStore.check`).
   A short bounded re-check (3 tries, 50 and 100 ms) absorbs storage that lags the database. One unverifiable
   output refuses the whole publication with `CONTENT_UNVERIFIABLE` and a per-slot reason (`missing`,
   `size-changed`, `digest-changed`); nothing is published and the command key stays unused. The runner answers
   that refusal by re-staging the affected outputs once from the attempt's own staging files (a damaged blob is
   replaced, never trusted) and retrying under the same key. If the refusal persists, the attempt fails with
   category `storage` and the unverifiable slots: late or missing content is explicit, never published.
2. **The human changes the question or withdraws a release mid-investigation.**
   - New command `revise_question` (grant `steer`). It appends an attributed question revision to the
     investigation (`question_history`), effective at the **next turn**. A turn already running keeps the view at
     its horizon, and a decision it proposes after the change is recorded unapplied (`stale-revision`, existing
     rule): it never sees the new question in the middle of a turn.
   - Admitted work follows `running_job_policy`: `drain` (default; jobs keep running, and the event records the
     question revision they were admitted under), `checkpoint-and-stop` or `cancel`. Under a stopping policy, the
     remaining calls of a decision accepted before the change are refused (`QUESTION_REVISED`) for the
     investigator; the human who made the change is not blocked.
   - The change is recorded with **exposure**: an exposure row (subject `question:<investigation>`, the charter's
     study id) stores what the reviser could have seen (ledger horizon, finished jobs, completed turns,
     assessments), and the question revision links to it.
   - A withdrawn release admits no new work (`WITHDRAWN_METHOD` at resolution). A withdrawal wakes every
     investigation whose work used the release (`investigation.release_withdrawn`), so its next turn sees it.
     Admitted work drains under a scientific withdrawal (results remain, marked affected through the existing
     traversal) and is stopped under a security withdrawal (existing behaviour).
3. **Assessor restart or the same evidence twice.** The service deduplicates by frozen plan digest, evidence-set
   digest and assessor release digest. The evidence set is normalised (sorted, de-duplicated artifact ids per arm)
   before hashing, so the same evidence in another order is the same evidence. A duplicate returns **the same
   receipt** as the first commit (outcome, conformance, findings, plan, evidence digest) with `deduplicated: true`,
   and leaves an `assessment.deduplicated` event. The assessor activity's command key now covers the whole
   payload, so a restart can never collide with an earlier key.
4. **Assessment method withdrawn during confirmation.** Following the design's withdrawal rules ("Preserve the
   frozen plan and existing receipts, flag affected support, and block promotion under the withdrawn method.
   Reassessment under a corrected release is a new attributed assessment/plan"):
   - `withdraw_component` labels every unassessed plan that depends on the release (pinned assessor or template,
     an arm component, an affected job) `health: affected, blocked_by: <withdrawal>`. Admitted work drains.
   - When the plan's work ends, it becomes `assessment-blocked` and **no assessment is requested**;
     `submit_assessment` for it fails closed with `WITHDRAWN_METHOD`.
   - New command `reassess_evidence_plan` (grant `freeze_plan`): a new, attributed plan that reuses the old plan's
     frozen arms and executed receipts and pins a corrected, approved assessor release. It is refused when the
     withdrawn release produced the evidence (an arm component or the template), because the evidence itself is
     affected and must be re-executed under a new plan. The old plan, receipts and withdrawal are unchanged.
   - An existing alternate justification of the claim keeps working as before (`add_justification`).
5. **A branch request loses its reply.** No new mechanism: the command service's key idempotency returns the same
   receipt for `plan_branch` and `launch_branch` retried under the same key, and `launch_trial` refuses a second
   launch of a trial under a new key. The test pins this down: one branch record, one child job.

**Consequences.**
- New error codes: `CONTENT_UNVERIFIABLE`, `WITHDRAWN_METHOD`, `QUESTION_REVISED` (all HTTP 409).
- New commands: `revise_question`, `reassess_evidence_plan`. New events: `investigation.question_revised`,
  `investigation.release_withdrawn`, `assessment.deduplicated`, `evidence_plan.assessment_blocked`,
  `evidence_plan.reassessment`. New plan status `assessment-blocked`.
- No schema migration: everything is stored in existing records, heads, labels, events and exposures.
- Not covered: storage that is a remote object store (the local store is the only one in v1), and a component
  release swapped during a live turn beyond the withdrawal case.
