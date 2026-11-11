# 0021: Assessment by default and evidence-derived claim state

Status: accepted (2026-11-11, round-2 item E2; recommendations 3 and 4 of diagnosis D1). Extends the
evidence-plan design (`src/eidos/domain/evidence.py`), ADR 0007 (refused proposals) and ADR 0018 (the ordinary
accuracy path).

## Context

The diagnosis of development pilot 1 (`~/eidos-internal/tickets/D1-REPORT.md`, section 3) found two defects that
hold regardless of the task:

- **Reachability.** The assessor commands were advertised and qualified, but a verdict needed bespoke composition of
  several tools. D02-r2 corrected a wrong charter id, drafted a plan and never froze it; its pipelines ended in
  classification scorers, not the `predictions` slot the assessment workflow reads. D02-r3 filed a claim and drafted
  no plan.
- **Under-claim.** D02-r3 exported a candidate whose independent gain was +0.13 test accuracy, but chose the status
  `inconclusive` because it had no assessor verdict. Its status alone carried the result; D02-r1 chose
  `improvement_claimed` on essentially the same limitation and passed.

Both come from a free-form conclusion: the status of a claim was whatever the model chose. Design principles that
stay: prospective rules, independent assessment, recorded evidence, freeze before confirmation.

## Decision 1: one command from a locked candidate to a launched plan

`assess_candidate` (lab command, investigator tool, MCP tool, `lab assess`) takes a locked candidate, a baseline and a
charter rule and returns a frozen, launched evidence plan, or a recorded refusal.

**Path selection** comes from the charter rule's metric identity (`eidos.domain.assessment.METRIC_PATHS`):
`classification-report/1:accuracy` uses `eidos.task.paired-classification-report@1.0.0` and
`eidos.assessors.paired-ci@1.2.0` (ADR 0018); the paired-report metrics use the template's train/evaluate path with the
same assessor. A metric with no assessor path (for example regression metrics, which have no assessor today) is
refused with `no-assessment-path` rather than improvised.

**Why one transaction, not a template job.** The template (`eidos.templates.improves-under-budget@1.2.0`) is a
component that runs as a job. Running it from a synchronous command would mean a job, a callback and a second place
the pipeline can fail while an investigator waits, in the environment (`lab`) that D1 found least capable. The command
therefore expands the same draft itself (`classification_draft`, `train_evaluate_draft`), records the template release
in the plan as before, and states `disclosures.expanded_by`. `tests/components/test_assessment_pipeline_template.py`
asserts the expansions equal the template's own output, so they cannot drift silently.

**Validation before launch.** Each failure is a reason with a stable `code`:

| What | Codes |
|---|---|
| Charter id and rule: the investigation's pinned charter is authoritative | `charter-required`, `charter-mismatch`, `charter-not-found`, `exploration-only-charter`, `rule-not-found`, `no-assessment-path` |
| Output schema: the final step produces what the pinned assessor reads, artifacts match the adapter's ports | `output-schema` |
| Seed pairing and fresh initialization: same seeds in both arms, the job's recorded seed matches, no checkpoint or branch input | `seed-pairing`, `seed-mismatch`, `not-fresh-initialization` |
| Locked candidate: an investigation's candidate jobs are runs of the recipe named by its latest `candidate_lock` note | `candidate-not-locked` |
| Data provenance | `provenance-required`, `selection-data`, `held-out-unattested`, `selection-exposed` |
| Protocol and request | `protocol-not-approved`, `isolation-required`, `claim-required`, `claim-kind`, `claim-not-found`, `job-invalid`, `request-invalid` |

**A refusal is recorded, not raised.** A command that raises rolls its transaction back, so the reason would vanish. A
refused request is committed as `assessment_request` with status `refused`, its reasons and an event, and the receipt
has `status: refused`. `lab assess` exits 1 and the investigator sees it under "Assessment requests". Failures from
drafting, freezing or launching happen inside a savepoint and are recorded the same way (`plan-refused`,
`step-blocked`), leaving no partial plan.

**Data provenance.** `data_provenance.split` must be `held-out` or `selection`. Selection data stays exploratory:
`selection` is refused here, and a plan whose `data_provenance.split` is `selection` cannot be frozen by any path.
Held-out needs an operator attestation committed in the charter before the investigation (`held_out: {labels:
[digests], splits: [names]}`, validated by the charter rules, included in its digest). An investigator's own
declaration is not enough, and labels that were an input to an exploratory job of the investigation are refused as
`selection-exposed`. The attestation does not prove the predictions came from those labels' inputs; ADR 0018's limits
on what a numeric rule can prove still apply.

**Typed references.** `eidos.project.command` publishes `prediction-array/1` and task inputs are `task-array/1`, while
the adapter's ports declare `class-predictions/1` and `class-labels/1` (it reads the same NPZ, JSON or CSV by content).
The command registers a typed reference to the same bytes, with `typed-reference` lineage.

## Decision 2: the claim state comes from evidence

A conclusion now records `claim_state` (schema `conclusion-claim/1`, in `body.conclusion`, whose schema is
`conclusion/2`). `eidos.domain.assessment.derive_claim_state` computes it when the turn closes, from recorded claims,
plans and assessments:

| Evidence on the claim's current revision | State | `confirmed` |
|---|---|---|
| A conformant, healthy assessor pass | `protocol-passed` (scoped to the assessment's claim scope and population) | yes |
| No assessment, or inconclusive, or evidence not healthy, or a deviated pass | `candidate-submitted` | no |
| An intact `protocol-failed` | `not-supported` | no |

- A model may ask, via `control.claim.disposition`, for `candidate`, `confirmed`, `null` or `abstain`. `abstain` and
  `null` are honoured (`abstained`; `null-reported`, or `not-supported` when an intact failure exists). `confirmed`
  without a pass is recorded as a downgrade (`downgraded_from`, `downgrade_reason`).
- `candidate-submitted` keeps the point estimate, uncertainty, population and unfulfilled obligations. Figures from an
  assessment are marked `source: assessor`; investigator figures are marked `source: investigator` and do not change
  the state. The population is the charter's.
- An intact failure outweighs a pass on the same claim revision, so re-running a plan until it passes does not confirm
  a claim. Assessments of other claims or older revisions stay visible in `assessments_considered`.
- Models that still write `scope.answer.status` keep working: the status is read as a disposition, never as a state.
- `file_claim` refuses an asserted `state` or `confirmed`; a filed claim starts as `candidate-submitted`,
  `submit_assessment` updates it, and `get_claim` returns the current `claim_state` (accounting for later withdrawals).

**Compatibility.** Older conclusions have no `claim_state`; readers treat that as before. The wire schema
`schemas/eidos-wire/v1/conclusion.schema.json` (`eidos.boundary.wire.RECORDS`, published alongside the worker messages
but not one of them) allows the absent fields, and fixtures in `tests/fixtures/conclusion/` cover old and new records.
`Control.claim_json` and the lenient decision schema gain an optional `claim`.

## Decision 3: the study adapter maps states explicitly

`study/eidos_arms/arm4.py::map_claim_state` maps the claim state onto `eidos-answer/0.2`, as documented in
`study/eidos_arms/CHARTER_RULES.md`. A submitted candidate with a positive point estimate becomes
`improvement_claimed` / `recipe_improvement` with a scope stating "pending independent confirmation"; `not-supported`
becomes `no_improvement_found`. The answer records the state and the model's original status in `deviations`. The
evaluator still decides.

## Alternatives rejected

- **Raising on refusal** loses the reasons with the rollback. Rejected for the recording requirement.
- **Letting the model choose among the new states** repeats the defect that caused the under-claim.
- **Treating `inconclusive` as `not-supported`**: an inconclusive assessment is not a failed one, and doing so would
  discard a candidate on insufficient evidence.
- **A held-out flag on artifacts**: charter attestation is committed before exploration and is already part of the
  immutable, digest-pinned record; a mutable flag would add a second source of truth.
- **Removing `draft_evidence_plan` / `freeze_evidence_plan`**: they stay for plans outside the default path. They gain
  the selection refusal on freeze; they are otherwise unchanged.

## Consequences

- The default route to a verdict is one call, and its refusals say what to fix.
- Without an attested held-out split, every candidate stays "submitted for confirmation", by design. The next pilot's
  operator decides whether to attest investigator-reachable held-out labels; the current charters do not.
- `get_claim`'s new `claim_state` and the conclusion's `claim_state` are additive.
- A plan drafted directly with no `data_provenance` is treated as before: its assessment can still produce
  `protocol-passed`. The selection guarantee applies to plans that declare selection data and to everything through
  `assess_candidate`.
