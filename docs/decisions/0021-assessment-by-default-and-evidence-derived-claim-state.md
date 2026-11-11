# 0021: Assessment by default and evidence-derived claim state

Status: accepted (2026-11-11, round-2 item E2; recommendations 3 and 4 of diagnosis D1; amended the same day by E2b
after review R12, see "Prospective confirmation"). Extends the evidence-plan design (`src/eidos/domain/evidence.py`), ADR 0007 (refused proposals) and ADR 0018 (the ordinary
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
therefore expands the draft itself, records the template release in the plan as before (approval scope), and states
`disclosures.expanded_by`. The train/evaluate expansion (`train_evaluate_draft`) equals the template's own output,
which `tests/components/test_assessment_pipeline_template.py` keeps true. The classification expansion
(`fresh_classification_draft`) deliberately does not; see "Prospective confirmation".

**Validation before launch.** Each failure is a reason with a stable `code`:

| What | Codes |
|---|---|
| Charter id and rule: the investigation's pinned charter is authoritative | `charter-required`, `charter-mismatch`, `charter-not-found`, `exploration-only-charter`, `rule-not-found`, `no-assessment-path` |
| Output schema: the final step produces what the pinned assessor reads, artifacts match the adapter's ports | `output-schema` |
| Seeds and fresh initialization: a list of distinct seeds, new for each recipe; recipes that restored no checkpoint or branch | `seed-pairing`, `seed-already-observed`, `not-fresh-initialization` |
| Locked candidate: on both paths, the candidate is the recipe named by the investigation's latest `candidate_lock` note | `candidate-not-locked` |
| Prospective evidence: recorded jobs only name recipes; an already-existing result can never be confirmation | `observed-evidence`, `held-out-reused` |
| Data provenance | `provenance-required`, `selection-data`, `held-out-unattested`, `selection-exposed` |
| Protocol and request | `protocol-not-approved`, `isolation-required`, `claim-required`, `claim-kind`, `claim-not-found`, `job-invalid`, `request-invalid` |

**A refusal is recorded, not raised.** A command that raises rolls its transaction back, so the reason would vanish. A
refused request is committed as `assessment_request` with status `refused`, its reasons and an event, and the receipt
has `status: refused`. `lab assess` exits 1 and the investigator sees it under "Assessment requests". Failures from
drafting, freezing or launching happen inside a savepoint and are recorded the same way (`plan-refused`,
`step-blocked`), leaving no partial plan.

**Data provenance.** `data_provenance.split` must be `held-out` or `selection`. Selection data stays exploratory:
`selection` is refused here, and a plan whose `data_provenance.split` is `selection` cannot be frozen by any path. A
plan with no verified held-out data is exploratory by construction; see "Prospective confirmation".
Held-out needs an operator attestation committed in the charter before the investigation (`held_out: {labels:
[digests], splits: [names]}`, validated by the charter rules, included in its digest). An investigator's own
declaration is not enough, and labels that were an input to an exploratory job of the investigation are refused as
`selection-exposed`. The attestation does not prove the predictions came from those labels' inputs; ADR 0018's limits
on what a numeric rule can prove still apply.

**Typed references.** `eidos.project.command` publishes `prediction-array/1` and task inputs are `task-array/1`, while
the adapter's ports declare `class-predictions/1` and `class-labels/1` (it reads the same NPZ, JSON or CSV by content).
A typed reference to the same bytes is registered, with `typed-reference` lineage: for labels when the plan is drafted,
and for a fresh run's predictions when the step that reads them is admitted (a step's `input_schemas`).

## Prospective confirmation (E2b, review R12)

Review R12 found that E2's checks stopped short of the two rules this ADR depends on: a confirming plan must be
prospective, and its data must be verified held-out data. `assess_candidate` froze a plan around predictions it
required to already exist, and a plan drafted directly with no `data_provenance` could reach `protocol-passed`. The
rules below hold for every plan, not only the default pipeline.

1. **Fresh evidence only.** The classification path now freezes the candidate and baseline recipes, seeds, labels and
   rule first, then launches fresh arms: `fresh_classification_draft` gives each seed a training step per arm and a
   report step per arm that reads the two new prediction outputs (a step may reference a uniquely named step of
   another arm; training steps are marked `intermediate`, so the assessor reads only report outputs). `baseline` and
   `candidate` name a recorded job as `{"job_id": ...}` only to take its recipe; the seed-to-job map is refused as
   `observed-evidence`. Seeds a recipe has already run are refused as `seed-already-observed`, because re-running one
   reproduces a result that was visible when the plan was written.
2. **No frozen observed results, on any path.** `freeze_evidence_plan` refuses a plan pinning a result artifact, by
   schema or by the digest of an existing result artifact under any schema (`observed-evidence`). This retires
   `eidos.templates.improves-under-budget@1.2.0`'s classification path, which pins predictions that already exist: its
   draft can no longer be frozen. The template release is unchanged, and it still expands; a new template release with
   a fresh-arm classification path is left to whoever needs a manually run template.
3. **Held-out data is used once.** A verified confirmation plan reserves its held-out data (`data_keys`, by labels digest
   or attested split name); a later plan on the same data is refused (`held-out-reused`), including under a different
   claim or rule. Freezing locks the charter so two plans cannot both take it. Re-using a held-out set after seeing its
   results is what the rule forbids, and the operator's route is to attest new held-out data.
4. **Verified, not declared.** At freeze the service records `confirmation: {eligible, reasons, verified, data_keys}`
   from the ledger: each arm's labels digests or evaluation split are checked against the pinned charter's `held_out`.
   The draft's own `data_provenance` is never trusted. Declaring held-out data the charter does not attest is refused
   (`held-out-unattested`). A plan with no provenance, or one pinning a model an earlier job trained, can be frozen but
   is **exploratory**: its assessment is recorded, the evaluation stays readable, and the outcome is `inconclusive` with
   a finding, so neither a pass nor a failure counts.
5. **Older plans.** Plans frozen before this check have no `confirmation` block. They and their assessments stay
   readable, but `can_confirm` is false: a new conclusion or `get_claim` over them gives `candidate-submitted`, never
   `protocol-passed`.

**The train/evaluate path** resolves the investigation's `candidate_lock` and compares the submitted recipe
(`common_train` + `candidate.train`, against the locked training job's component, inputs and config) with its recorded
identity. A candidate with `post` steps is refused, because a lock names one training job and cannot describe them. The
command also pins the bare `eidos.pytorch.train` and `eidos.pytorch.evaluate` the expansion names to their active
releases; unversioned references did not resolve at admission, so this path could never launch before.

**What this means for the study.** In the study the evaluator owns the held-out test data, so an investigator has only
train and validation data. Its assessment honestly stays exploratory, and the normal outcome is "candidate submitted for
confirmation", with the evaluator deciding. The investigator's tool guide says so plainly: a candidate can be submitted
without a held-out verdict, and that is a complete result rather than a failure. The refusal for `provenance-required`
and `held-out-unattested` carries `exploratory: true` and repeats this; the conclusion still records the investigator's
point estimate, marked `source: investigator`, and the unfulfilled obligation. Charters attest held-out data only where an
operator chooses to give the investigator such data.

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
- An abstention or a null in either representation (the claim block's `disposition` or the eidos-answer status) takes
  precedence over the other, so contradictory fields never make a claim. The study adapter applies the same precedence
  to the answer's status when mapping a claim state.
- An investigator's point estimate is normalized by the metric's direction before it is recorded: `value` means
  improvement, positive is better. Baseline and candidate figures are converted using the direction stated with the
  estimate, the charter's metric definition, then the metric's conventional name; if nothing states the direction the
  value is recorded as null with `direction_unknown`, rather than guessed.
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
- **Removing `draft_evidence_plan` / `freeze_evidence_plan`**: they stay for plans outside the default path. They carry
  the same freeze-time rules as the default pipeline.
- **Rejecting any plan without held-out provenance at freeze**: exploratory plans are a legitimate way to measure
  without a verdict, and refusing them would remove a record of work; they are capped instead.
- **Letting already-observed results confirm when the charter rule predates them**: a fixed threshold does not stop an
  investigator from choosing seeds or the plan after seeing results.

## Consequences

- The default route to a verdict is one call, and its refusals say what to fix.
- Without an attested held-out split, every candidate stays "submitted for confirmation", by design. The next pilot's
  operator decides whether to attest investigator-reachable held-out labels; the current charters do not.
- `get_claim`'s new `claim_state` and the conclusion's `claim_state` are additive.
- A plan drafted directly with no `data_provenance` is exploratory and cannot reach `protocol-passed`. The guarantee
  applies to every path, including older plans at the time a new conclusion is drawn.
- Confirmation on the classification path now costs fresh training per seed and per arm. That is the price of a
  prospective result; a charter that attests held-out data must budget for it.

## Integration with ADR 0020 and ADR 0022 (E4)

Plans launched from a batch go through the same `freeze_evidence_plan` and `launch_evidence_plan`, so provenance and the
prospective rules hold unchanged: a result pinned through `$ref` already exists when the plan is frozen (a reference to a
job waits for it to succeed) and is refused as `observed-evidence`. `assess_candidate` is a plan tool for the batch
scheduler: its jobs run one at a time and hold the decision's CPU allowance, and it takes the decision's checked
`max_concurrent_jobs`. A refused `assess_candidate` is a recorded answer that admitted nothing, so it holds no allowance
and gives a wait nothing to wait for. The decision schema carries `control.claim` beside `scope` and batch references.

## Addendum (review R13): the freeze boundary covers every confirmation-eligible plan

Review R13 found that `assess_candidate` checked prior use of labels and of recipes and seeds, but a plan drafted by hand
reached `freeze_evidence_plan` without those checks. An investigator could therefore evaluate recipes on the attested
labels, keep the favourable ones, and freeze a manual plan re-running them. The rules are now applied at the freeze
boundary (`evidence.check_prospective`), for every plan verified as confirmation-eligible, however it was drafted:

- **Data already used.** A plan's attested labels or evaluation splits must not be inputs of any earlier job of the
  project, whatever its purpose: jobs of an exploratory plan have purpose `confirmation` too, and reserve no held-out
  data. The refusal is `selection-exposed`.
- **Recipes and seeds already run.** No root step of the plan (one with no input from another step of the plan) may
  repeat a recipe and seed that already ran, by `recipe_identity`. An unseeded step whose recipe has run at all is
  repeated, since nothing makes the rerun fresh. The refusal is `seed-already-observed`.

Both are refused rather than marked exploratory: the plan declares held-out data, and running a plan with reused seeds
would expose labels that are still clean. To measure such a plan, draft it without held-out `data_provenance`: it then
stays exploratory as before. `held-out-reused` is still checked first. Evaluation splits are matched by name across the
project, which errs towards refusal when two charters reuse a split name.

The pinned candidate state in `investigation_view` is now `conclusion_claim_state` itself, as of the view's horizon, so
it applies the current claim revision, plan eligibility and "an intact failure outweighs a pass". A pass on an exploratory
plan can no longer be pinned as `protocol-passed`.

A `candidate_lock` note identifies its job through JSON text `{"job_id": "<job>"}`, or through exactly one job in
`evidence_refs`, so natural-language locks that cite one job can be assessed. A prose mention of a job does not identify
it. The playbook and tool guide give this format, and the pinned lock says when a note names no job.
