# Changelog

All notable changes to Eidos. Tags are annotated, created only on CI-green commits, and never moved. Development tags `eidos-v0.x` were freezes, not releases.

## Unreleased: 1.4.0

- [ADR 0029](docs/decisions/0029-study-fresh-checkpoint-contract.md) gives all four study arms the existing fresh-checkpoint naming contract in their shared prompt and tool-facing charter before Pilot 4.

- Preserve ordinary finalization's reproduction/export/confirmation under the default policy; admit conclude-only recovery separately and handle disabled operator admission without deadline loops (R18).
- Isolate direct and derived held-out evidence in model prompts, same-turn feedback and read results across exploration and finalization. Preserve the original charter attestation in internal audit metadata. An explicit held-out assessment can use one uniquely attested labels binding without disclosing it to the investigator.
- Replay the four verified historical pilot-3 crash tails through the coordinator and preserve terminal quota classification from its recorded provider receipt.

- **Deadline admission (ADR 0026).** Valid read follow-ups can use the actual remaining decision/turn time with a durable-write margin; HTTP and total request timeouts fit that boundary. Runtime deadline exhaustion is explicit telemetry/control, never a fabricated model wait. The exploration boundary transitions to finalization without sleeping through hold-back. The arm pins its export margin to the outer watchdog, and the ledger rejects late conclusions even when their decisions were accepted on time.
- **Bounded autonomous finalization (ADR 0027).** Default autonomous policy admits one read-only conclude/stop turn after repeated refusals, runtime exhaustion or exploration completion, inside the original wall/token envelope and separately accounted. It reports the fixed incumbent and evidence limitations, or abstains. A failed recovery ends the default run with a separately attributed controller abstention and a structured export receipt. The investigator outcome remains failed, and operator policy remains available.
- **Study outcome contracts.** Earlier recovered provider failures leave later behavioral outcomes intact. Fresh-seed confirmation uses the candidate-declared checkpoint filename. Offline study contract tests run in CI.
- **Prospective rule preflight and diagnostics.** Scorer-port bindings are validated before a selection rule becomes immutable. Classification/reference and reproduction-seed examples explain correct contracts. Refused decisions retain raw response artifact references.

- **CI package availability.** Ubuntu package jobs use the official archive when the runner image points at the Azure mirror, whose repeated DNS failures blocked verification. Root package installs preserve configured proxy settings and use a bounded TLS-verified DNS bootstrap if the runner cannot resolve Ubuntu repository names. Signed-package checks and all test jobs remain required.

## Unreleased: 1.3.0

### Development evaluation ladder (ADR 0028, T1)

- Renumbered the development evaluation ladder from ADR 0026 to [ADR 0028](docs/decisions/0028-development-evaluation-ladder.md) to avoid the round-4 ADR numbering collision.

- Use one Tier-1 controller for the frozen round-3 and round-4 builds. Record the server-registered deadline and measured margin, preserve bounded replies and finalization receipts, and classify controller abstention separately.

- Tier-1 mini-evals reuse pilot adapters and local grading for arm 4 with optional arm 2, with per-card zero-tolerance comparison, model-facing identifier coverage, verified frozen-build provenance, and separate history for each attempt.
- Tier-0 extraction preserves all registered model-response artifacts, request timings and terminal state. Four real pilot-3 failure fixtures support core coordinator replay tests.
- Reference regeneration records source hashes and missing data. Frozen eval labs use SSD storage and dedicated ports. Baseline-stub CI exercises the study suite without model calls. The loopback provider bridge binds with a local server name so reverse DNS cannot delay startup.

### Incumbent and challengers, baseline identity, fresh-state repair, provider resilience (ADR 0023, G1)

- **Incumbent plus challengers until the exploration deadline.** A selection rule (metric, `validation` or
  `training-cv`, direction; optional `ceiling`, `max_rounds`) is recorded before the rounds it judges and is fixed. A
  `candidate_lock` that changes the incumbent must beat it on that rule, read from recorded receipts (the lock job's
  scorer lineage), else `record_note` refuses it; each change is versioned on its note and emitted as
  `incumbent.changed`. No lock changes after a plan is frozen or an assessment requested.
- **Exploration allowance and stops.** `begin_turn` records `spec.exploration`: challenger rounds, the wall and token
  allowance after a hold-back for reproduction and export (before the conclusion reserve), and the phase. Exploration
  stops at a perfect score, `patience_rounds` without improvement, a declared search limit, the hold-back, or the
  selection freeze; then `accept_decision` refuses new open-search and hypothesis-test compute.
- **`context-policy/3`** pins the rule, incumbent, rounds, allowance and phase, and reads the baseline from its durable
  record. **`investigator-playbook/2`** (`investigator-prompt/3`) replaces "lock early, stop as soon as" with challenger
  rounds. `context-policy/2` remains for replay.
- **Baseline identity.** The lab records an investigation's first baseline job (project command without authored
  source, or a job a `baseline` note designates) and the first job that scored it (`investigation_baseline`; `baseline.recorded`,
  `baseline.scored`). "Baseline: none recorded yet" no longer appears once a baseline has run.
- **Fresh-state repair.** A reply still malformed after the schema re-ask (or the output retries in tool mode) gets a
  bounded, counted, recorded request from a fresh state (`fresh_state_repairs`, default 1) before the bounded
  no-effect outcome. Tool mode no longer turns persistent invalid output into a retried activity failure.
- **Provider resilience.** Failures are classified (quota exhausted, transient, permanent). A transient failure's
  `Retry-After` (with jitter, bounded by the time before the conclusion reserve) sets the next Temporal attempt's delay
  and the turn retry's backoff; quota exhaustion (e.g. MiniMax 2056) is non-retryable and marks the investigation
  needs-input with code `provider-unavailable` at once. Every attempt records its usage, uncertainty, elapsed time and
  failure class. The study provider bridge forwards `Retry-After`.

### Selection is prospective and evidence-bound (ADR 0023 addendum R14, G1b)

- **Rule before challengers.** A lock naming a job needs a recorded selection rule; a re-lock needs recorded evidence
  for both incumbent and challenger, a challenger admitted after the rule, and a strictly better value. Unverified and
  incumbent-unscored re-locks are refused.
- **Provenance.** Selection rules name their `scorer` and selection `data`; only that scorer's receipts on that data
  count, never attested held-out data, and a lock cannot nominate `scored_by`.
- **Reproduction criterion.** `reproduce_within` (or `reproduction_tolerance`, default 0.02): an incumbent is
  reproduced only by a scored re-run within it; locks and the conclusion record the evaluation.
- **Hold-back.** Exploratory turns, their activities and retries end at the exploration deadline; the boundary is
  recomputed between decisions and exploratory reservations must fit before the token hold-back. A Retry-After that
  cannot fit stops the retry and the next turn finalizes.
- **Patience** counts only rounds whose challengers are terminal and evaluated; scoring a pending challenger stays
  allowed in the finalize phase.
- **Baseline** only from the project command or a `baseline` note, never from a `reproduction` purpose.

### Baseline error analysis and typed sweep templates (ADR 0024, from the diagnosis of development pilot 2)

- **`eidos.diagnose.baseline-errors@1.0.0`** (new, numpy): from train, validation and optional baseline predictions,
  groups and task, a `baseline-diagnosis/1` report with split statistics, error slices (class or target quantile,
  input-norm and input-mean quantile, groups), invariance probes (quick ridge and k-NN refits under none, per-example
  centering, per-example standardization, split-wise standardization and shift-invariant features, with within-split
  CV) and ranked hypotheses that tell an additive offset, a gain change, a feature-wise shift, a spatial or positional
  shift and information loss apart, each with a prediction and a falsifier. Provenance and input digests recorded.
  Reads no test data; exploratory.
- **`eidos.search.recipe-sweep@1.1.0`: typed templates.** `config.template {name: quick|broad, ...}` runs a broad sweep in
  one call with no authored code: preprocessing (none, train-fitted standardize, per-example center or standardize)
  crossed with linear, k-NN, kernel ridge, small MLP, and SVM and GBM where scikit-learn exists (otherwise recorded as
  skipped), screened on validation, finalists cross-validated within train, every attempted recipe recorded, a
  reproduce command for the winner. Command sweeps are unchanged.
- **Config contracts support `anyOf`**, so the sweep's schema accepts a template or a full command sweep and still
  refuses neither.
- **Investigator guide:** diagnose, then a template sweep, then targeted challengers; diagnosis jobs are pinned with
  sweeps in the context. Method cards, author guide and toolkit coverage updated; arm 4 also installs the diagnosis
  component; the standalone toolkit carries both.

### Review R16 of G2 (ADR 0024 addendum)

- **Diagnosis:** `prediction_key` and `groups_key` are checked against held-out names before any input is opened; array
  names with a word beginning `test`, `holdout` or `heldout` are refused.
- **Sweep 1.1.0:** repeated seeds are refused (`seed_policy.seeds`, `template.seeds`); every evaluation gets its own
  directory and a working-directory error is recorded as that evaluation's failure, so the evidence table is still
  written. `command.copy` entries and non-code source-bundle files with a word beginning `test`, `holdout` or `heldout`
  (`test1.csv`, `testfeatures.csv`) are refused.
- **`eidos.search.recipe-sweep@1.0.0` stays installable** beside 1.1.0 (`components/eidos.search.recipe-sweep@1.0.0/`,
  byte-identical to the 1.0.0 release), so a fresh project resolves a pinned 1.0.0 reference. Arms publish it unbound;
  the toolkit runs each release with its own module.

### Compact MCP research workflow for external agents (ADR 0025, from diagnosis D2 recommendation 4)

- **`upload_source`** stores project files as an immutable `source-bundle/1` artifact in one MCP call (no separate HTTP
  upload or manual metadata).
- **`run_batch`** runs up to 12 calls with ADR 0022 `$ref` references over MCP, through the investigator's batch
  executor, now shared as `eidos.batches.BatchRunner`. Calls are validated against their own tool schemas and the
  reference rules before any effect (`INVALID_REF`, `INVALID_BATCH`), recorded first as an `mcp-batch` note, keyed
  `<key>:<i>`, run one job at a time, and resumed after a bounded wait by calling again with the same key.
- **`wait_status`** waits at most 50 s and returns compact job, plan, claim and investigation summaries with artifact
  handles.
- **`submit_candidate`** records the `candidate_lock`, files the `improves` claim and optionally requests the assessment,
  returning the lab-derived claim state (`candidate-submitted` without a confirming pass). An unsucceeded job is refused.
- **Smaller schemas and results.** The idempotency, revision and payload boilerplate is stated once in the server
  instructions; tool text results are compact JSON. `tools/list` is 39,726 bytes for 39 tools (was 46,387 for 35).
- **Session reuse.** The study's `mcp_http.py` reuses one MCP session (also across invocations, through a 0600 state
  file without the token), re-initializing once if the server forgets it, and prints each result once.
- **Review R15.** A batch runs in one investigation: a call's scope may be named or set in `payload`, and launches take
  their trial's scope. A call outside it, or a `$ref` across investigations, is `INVESTIGATION_MISMATCH` before any
  effect. `submit_candidate` binds its whole normalized request, assessment included, to the key with a `<key>:submit`
  record before any step, so a changed retry is `COMMAND_KEY_CONFLICT` with nothing replayed. Compact receipts of a
  refused assessment keep every reason.

### Round-3 integration (H1; ADR 0025 addendum H1)

- **ADR 0023 on the MCP path.** `submit_candidate` takes the prospective `selection` rule and locks through the same
  validated `candidate_lock`. New `open-search` and `hypothesis-test` work admitted outside a decision (`run_batch`, direct
  calls) is refused once exploration has stopped, unless it reads a pending challenger's output. While exploration runs,
  that work's wall is capped at the hold-back.
- **One order in the playbook and tool guide:** baseline, diagnosis, broad template sweep, incumbent and challenger
  rounds, reproduction, assess or submit, conclude. The guide's lock paragraph follows R14.
- **Pinned facts:** the latest diagnosis is always pinned ahead of the three latest sweeps.
- **Study:** the arm-3 tools paragraph describes the new MCP tools (the manual upload is gone; `mcp_http.py` stays as the
  fallback). The arm-2 toolkit verification runs the diagnosis and a template sweep. The toolkit ships
  `eidos.domain.assessment`, which `paired-ci` needs standalone, and lists `eidos.task.paired-classification-report`.

### Review R17 of the round-3 integration (H2; ADR 0023 and ADR 0024 addenda H2)

- **MCP rounds count.** One shared round history covers investigator turns, MCP `run_batch` batches and locks recorded
  by `submit_candidate`; `max_rounds`, patience and the ceiling stop MCP challengers as they stop the investigator's.
  A batch is one round, checked as it began.
- **`measurement-development` is gated** once exploration has stopped, like `open-search` and `hypothesis-test`; only
  work that finishes an already admitted round is let through.
- **The hold-back holds before the first lock.** The wall and token hold-back apply in the baseline phase; when they are
  reached the investigation finalizes with `candidate: none` and the refusal says no candidate was locked.
- **The pinned `recipe-sweep@1.0.0` cannot read held-out files.** The lab refuses test-, holdout- and heldout-prefixed
  data files in its `command.copy` or source bundle at resolution; the release bytes are unchanged.

## Unreleased: 1.2.0

### Review R13 of the integrated round (ADR 0021 addendum)

- **Freeze boundary.** `freeze_evidence_plan` refuses any confirmation-eligible plan, including manual plans, whose
  attested labels or splits an earlier job already read (`selection-exposed`) or whose root steps repeat a recipe and
  seed already run (`seed-already-observed`).
- **Pinned candidate state.** `investigation_view` carries `claim_state`, derived by the conclusion's rules, and the
  pinned facts use it: a pass on an exploratory plan or an earlier claim revision is never shown as `protocol-passed`.
- **Candidate lock format.** The playbook and tool guide give the exact `candidate_lock` format, and assessment also
  accepts a lock naming exactly one job in `evidence_refs`. The pinned lock says when a note names no job.

### Investigator reliability and completion (ADR 0020, from the diagnosis of development pilot 1)

- **Environment preflight.** Registration records a capability probe (interpreter digest, key modules, versions) and
  the project's declared requirements. Admission refuses a job whose component or project declares what the environment
  measurably lacks (`ENVIRONMENT_PREFLIGHT_REFUSED`), and records an `admission.refused` event. An unprobed environment
  is never refused. `eidos.task.score-classification` and `score-regression` 1.0.2 declare numpy.
- **Job views show the environment** (`investigation_view`, `get_job` with or without the lock, `list_jobs`).
- **Actionable failures.** Failure context includes the exception type and a bounded, redacted stderr tail. Failed job
  records keep up to 3000 characters of stderr.
- **`context-policy/2`.** A pinned facts block (baseline, lock, success rule, environments and capabilities, known
  failures, wall time) survives truncation and the job window; repeated failures read "failed 3× in environment X with
  ModuleNotFoundError numpy"; the catalog is dropped first and every omission is logged. `context-policy/1` remains for
  replay and older investigations.
- **`investigator-playbook/1`** in the system prompt (`investigator-prompt/2`): baseline without source, quick wins,
  early lock, fresh-seed reproduction, assessment or submission, deadline-aware stopping, null handling.
- **Deadline-aware completion.** Optional `investigator.wall_seconds` and `conclude_threshold_seconds`: turns and jobs
  stop before the conclusion's reserve, the investigator is told to conclude and a conclusion is required once it
  starts, retries are bounded by the remaining wall time, and the deadline surfaces as `wall-deadline-reached`.
- **Review R10 of the above (ADR 0020 addendum).** The environment probe imports nothing from the probed environment
  (`environment-capabilities/2`: `-I -S`, distribution metadata only, killed as a process group); it no longer measures
  importability or GPUs. Requirements are parsed with `packaging` (markers for the probed interpreter, compound constraints,
  extras, includes); import names come from metadata; what cannot be decided is inconclusive, never refused, and recorded in
  the job lock. Failure text is redacted before any cut, for stored and model-visible data, including quoted JSON secrets.
  Every pinned context section has a budget, omissions are recorded, and the final text never exceeds the limit. A turn,
  retries and execution included, ends at the reserve's start and a fresh conclusion turn begins there (stop reason
  `deadline`). The environment registration key digests its whole payload. The study arm passes `investigator.wall_seconds`.

### Assessment by default and evidence-derived claim state (ADR 0021)
- **`assess_candidate`** (lab command, investigator tool, MCP tool, `lab assess`) goes from a locked candidate, a
  baseline and a charter rule to a frozen, launched evidence plan, using the paired-classification adapter and
  `eidos.assessors.paired-ci@1.2.0` for accuracy and the template's train/evaluate path for paired metrics. It
  validates output schemas, seed pairing and fresh initializations, the charter id and data provenance before launch.
  Refusals are recorded (`assessment_request`) with stable reason codes.
- **Selection data stays exploratory.** `data_provenance.split: selection` is refused, a plan that declares it cannot
  be frozen, and held-out data needs an operator attestation in the charter (`held_out: {labels, splits}`).
- **A conclusion's claim state comes from recorded evidence.** `conclusion.claim_state` (schema `conclusion-claim/1`,
  published as `schemas/eidos-wire/v1/conclusion.schema.json`) is `protocol-passed` only for a conformant, healthy
  assessor pass, `candidate-submitted` with point estimate, uncertainty, population and unfulfilled obligations when
  nothing confirmed it, and `not-supported` after an intact failure. The model can abstain or report a null; a claimed
  confirmation without a pass is recorded as a downgrade. `get_claim` returns the derived state.
- **Study adapter.** `study/eidos_arms/arm4.py::map_claim_state` maps the states explicitly onto `eidos-answer/0.2`;
  see `study/eidos_arms/CHARTER_RULES.md`.
- Tests: `tests/unit/test_claim_state.py`, `tests/unit/test_assessment_pipeline_rules.py`,
  `tests/unit/test_conclusion_schema.py`, `tests/unit/test_arm4_claim_state_mapping.py`,
  `tests/components/test_assessment_pipeline_template.py`, `tests/integration/test_assess_candidate_e2e.py`.

### Prospective confirmation (ADR 0021, E2b)
- **A plan that can confirm a claim needs verified held-out data.** `freeze_evidence_plan` records `confirmation` from
  the ledger against the charter's `held_out`. Missing provenance is exploratory: its assessment is `inconclusive`.
  This applies to manual plans too, and a new conclusion over an older plan gives a submitted candidate.
- **Confirming evidence must not exist yet.** Freezing refuses plans pinning existing results, by schema or digest.
  `assess_candidate` now freezes recipes and seeds and launches fresh arms; a held-out set confirms only once, and
  a recipe's already-run seeds are refused. `baseline` and `candidate` are now `{"job_id": ...}` naming a recipe.
- **`eidos.templates.improves-under-budget@1.2.0`'s classification path can no longer be frozen**, since it pins
  existing predictions.
- The train/evaluate path verifies the candidate against the investigation's `candidate_lock` and pins its component
  releases, so it can launch.
- An abstention or a null in either representation takes precedence over the other. A reported point estimate is
  normalized by the metric's direction, with lower-is-better metrics no longer reversed.
- The investigator's tool guide states that a candidate can be submitted without a held-out verdict, the normal case
  when the evaluator owns the held-out data.

### Dependent batches and recipe search (E3, ADR 0022)
- **Dependent decision batches.** One accepted decision can now author source and run train, infer and score on it:
  call arguments can be `{"$ref": "call[i].field"}` or `call[i].outputs.slot`, resolved when the dependent call
  executes. The batch is recorded prospectively and each call keeps its receipt, command key and provenance. Malformed
  or unknown references, forward references and references in literal arguments are refused before acceptance.
- **Jobs of a decision run sequentially** unless a decision's `resource_policy` asks for concurrency and the project's
  operator-set `policy.resources.max_concurrent_jobs` allows it. `TurnWorkflow` waits for a batch's jobs durably (patch
  `dependent-batch-v1`). A failure stops only the dependent tail, recorded as `DEPENDENCY_FAILED`.
- **New component `eidos.search.recipe-sweep@1.0.0`**, in `components/` and the standalone toolkit: grid or seeded
  random search over a project command, on visible validation only, with seeds, folds, a required budget and a stopping
  rule. It records every recipe in a ranked evidence table and the best recipe. Exploratory; no claim, no test data.
- New wire schema `decision-proposal` and fixtures. Tool guide updated.

### Review fixes E3b (review R11 of 0a75804, ADR 0022 addendum R11)
- **Evidence plans use the one-CPU allowance.** `launch_evidence_plan` admits one job at a time, or the
  `max_concurrent_jobs` it asks for if the project's resource policy allows that, and the next step is admitted when a
  job ends. In a decision, a plan waits for an idle batch and holds its whole allowance until its last job has ended.
  The launch result's `jobs` now lists only the jobs admitted so far.
- **Sweep evaluations are process groups.** Each is stopped and reaped as a whole group on a timeout, a stop request,
  cancellation, or a normal exit that left workers behind.
- **Sweep evaluations cannot publish inherited credentials.** They run in a scrubbed environment, and failure text is
  redacted before it reaches the table.
- **The sweep copies no project file implicitly.** Without a source bundle, `command.copy` must list the files, and files
  named after a held-out split are refused, there and in a source bundle.
- **`DecisionControl` carries `scope`, `scope_text` and `bounded_outcome`**, which the runtime writes and the published
  schema dropped. A scope the model wrote that is not a JSON object is kept as `scope_text`. The decision fixtures now
  come from the runtime's own conversion.

### Integration of ADR 0020, 0021 and 0022
- **One workflow.** The playbook and the tool guide describe one path: baseline; a sweep or quick wins with batches; lock;
  fresh-seed reproduction; `assess_candidate` where held-out data exists, otherwise "candidate submitted for confirmation";
  conclude before the deadline.
- **`context-policy/2` pinned facts** also state the candidate and assessment state, recent sweeps and batches, and the body
  lists assessment requests.
- **`assess_candidate` follows the one-CPU rule in a batch** and passes the decision's checked `max_concurrent_jobs`.
  A refused request holds no allowance.
- **A waiting batch stops at the conclusion reserve** as well as at the turn's timeout.
- **`DecisionControl` publishes `claim` and `claim_text`** beside `scope` and batch references, with fixture
  `decision-proposal-conclude-claim`.

## Unreleased / 1.1.1

Reasoning models through OpenAI-compatible endpoints (ADR 0016). Measured with MiniMax-M3.1-Flash through
its OpenAI-compatible endpoint at 16384 output tokens and a 300 s request timeout: `json` 8/8 (was 5/8 at the 4096
cap and 6/8 at 16384 tokens / 120 s), `tool` 6/8 (was 4/8); `docs/release/evidence/g4-111-minimax-m31flash-*.json`.

### Review fixes F5d (review R9 of b6e57f8)
- **An operator stop or pause always wins over a turn's conclusion (ADR 0017 addendum "review R9").** A decision
  accepted with `conclude` (or `stop`) control that raced a stop or pause could still conclude the investigation:
  `TurnWorkflow` ignored a refused (`stopped`/`skipped`) execution and passed the decision's control to
  `complete_turn`, which wrote `concluded` over the operator's state. A decision with no calls took the same path.
  `complete_turn` now checks the investigation under its lock before applying conclusion control. If the
  investigation is not active, the turn closes as `cancelled` with `control_refused`, and the investigation keeps its
  state. `TurnWorkflow` treats a refused execution as cancellation (patch `refused-execution-cancels-v1`), and it
  returns `cancelled` when the lab refused the conclusion.
- **Flaky test fixed.** `test_failed_turn_policy.py::test_turn_workflow_failure_is_a_retryable_turn_failure` counted
  invocations of a fake `complete_turn`, which Temporal may deliver again. It failed once in CI with turn `t2`
  recorded twice. The fake now persists completions per turn (first wins, like the lab's locked completion handler)
  and begins each turn once, and the test asserts the persisted completions.
- Tests: `tests/integration/test_f5d_conclude_race.py`, `tests/integration/test_f5d_refused_execution.py`.

### Review fixes F5c (review R8 of 117741a)
- **Turn acquisition is fenced by an ordered ticket (ADR 0017 addendum "review R8").** The coordinator gives each
  turn workflow it starts a ticket (its workflow clock at the start, patch `turn-ticket-v1`), and `begin_turn`
  refuses a ticket older than the newest one that began or resumed a turn of the investigation (`superseded-run`,
  `stale-ticket`). Before, a delayed `begin_turn` from a terminated run took the turn back from the newer run, which
  was then refused as superseded. Turn workflows started before the upgrade carry no ticket and keep the earlier
  rule among themselves (known limitation).
- **A stop or a closed turn stops a decision's effects.** `hand_off_decision` checks the locked turn and
  investigation (stopped, paused, concluded, turn closed) before granting a new handoff or returning an existing one.
  Every effect of a decision is sent with an `admission` (`{decision_id, execution}`) and checked in its own
  transaction before the handler runs, `file_claim` included: refused with the new error `EFFECT_NOT_ADMITTED` and
  nothing written unless the decision is handed off, not revoked, its turn open, the investigation active and the run
  still the turn's owner. `execute_decision` stops at the first refused effect. This also refuses the remaining
  effects of a terminated run's still-running activity once another run resumed the turn.
- **The refused-proposal needs-input wait uses the mark's cursor (ADR 0019).** It discarded the cursor that
  `mark_needs_input` returns and compared operator events with the last turn's horizon, so an operator event between
  the two ended the wait at once. The cursor now goes to its `check_news` calls and survives a Continue-As-New.
- **A collapsed two-way interval never passes under a threshold rule (ADR 0018 section 8).** With the two-way
  interval, evidence whose interval is unbounded below (too few seeds, collapsed variance) is decided by the
  interval under every decision standard: failed if the upper bound is below `min_effect`, otherwise inconclusive,
  never passed. The result carries `unbounded_reason`, `decided_by` and `reason`. Before, a threshold rule compared
  the point estimate, so the reviewer's three-favourable-seed sample still passed.
- Tests: `tests/integration/test_f5c_ticket_and_handoff.py`, `tests/integration/test_f5c_refused_wait_cursor.py`,
  `tests/unit/test_two_way_interval.py` section 14.

### Large CLI payloads go through `@file`, not argv
- The e2e journey (`scripts/journeys/e2e_journey.py`) failed at step 3b with `OSError: [Errno 7] Argument list too
  long`: the paired-ci control suite (about 500 KiB) was passed inline to `lab cmd register_control_suite --payload`,
  and Linux caps one argv string at 128 KiB. The journey now writes every JSON argument (`--payload`, `--params`,
  `--config`, `--migration`) to `$OUT/payloads/` and passes `@path`; the switch-order and A8 journeys, the upgrade
  check and the MCP-client receipt verifier do the same. `lab investigate start --settings` now also accepts `@file`
  (it was the only JSON option that did not). The suite is unchanged.

### Ordinary classification accuracy evidence path (ADR 0018)
- **New built-in component `eidos.task.paired-classification-report@1.0.0`** (lab environment, installed by
  `lab project create`). Takes the baseline's and the candidate's held-out predictions and the labels (JSON, JSONL,
  CSV or NPZ), aligns them by example id and fails closed on any id mismatch, duplicate, or null, boolean,
  non-integral or non-finite value. Emits one arm's rows `{example_id, cluster, role, label, prediction}` in slot
  `predictions`; config `role` and `cluster` (the training seed).
- **`eidos.assessors.paired-ci@1.2.0`: two-way (training seed x held-out example) interval** (ADR 0018 section 6,
  review R4). Every seed is scored on the same held-out examples, so the seed-only interval of 1.0.0 and 1.1.0
  omitted example variance: three seeds improving on the same 3 of 100 examples gave CI [0.03, 0.03] and passed
  `min_effect` 0.02. 1.2.0 uses variance by seed plus variance by example (Cameron-Gelbach-Miller without subtracting
  the intersection: biased upward under the additive model, never negative; not a finite-sample coverage guarantee,
  see the F5 entry below) with a Student t critical value on Welch-Satterthwaite degrees of
  freedom, for the target and the guardrails and for both metric families; that example is now `inconclusive`
  (CI [-0.0040, 0.0640]). One seed without `cluster_by`: the plain paired t-interval over examples; with
  `cluster_by` one seed stays unbounded. The pre-existing path has the same flaw: 1.0.0 with `reference-accuracy`
  on `main`'s train/evaluate template (shared `data_seed` pairs) passes the same example; its recorded results are
  not changed (`evaluate_success_rule` keeps the original interval by default; new `interval=` argument), and
  plans pinned to 1.0.0 with several seeds should be re-assessed with 1.2.0. The evaluation records `interval`;
  the effect records `method`, `seeds`, `examples`, `variance_components`, `df`. New dependency-free Student t
  quantile in `eidos.domain.stats`. Paired-report author controls were resized (not weakened) for the
  conservative interval. Regression tests: `tests/unit/test_two_way_interval.py`; coverage simulation:
  `scripts/research/two_way_interval_coverage.py` (two-way 0.945-0.993 vs seed-only 0.53-0.88 at nominal 0.95).
- **Review fixes (F5, group B) to the two-way interval and the pairing of units (ADR 0018 section 7).**
  - With two or more seeds, a collapsed variance estimate (seed component or total exactly zero) or fewer than 3
    seed clusters now fails closed: the lower bound is unbounded (`unbounded_reason`), so the result is never
    `protocol-passed`; estimate, `se`, `df` and components are still reported, and identical arms still fail.
    Before, three seeds that were all favourable by chance under a discrete seed effect (+1 on every example with
    p = 0.4, -1 with p = 0.6, population effect -0.2; probability 6.4%) gave the point interval [1, 1] and passed
    `min_effect` 0.02. The R4 example (identical seeds) is now inconclusive through `seed-variance-collapsed`.
  - **Known limitation, reported in ADR 0018 section 7:** with 3 seeds and near-discrete seed effects the
    false-pass rate is still 5.5% to 8.4% (nominal 2.5%), 1.1% to 1.6% with 5 seeds. Use 5 or more training seeds
    for seed-level conclusions. The ADR's "conservative by design" now separates the variance estimator's upward
    bias under the additive model from finite-sample coverage.
  - Unmatched units between the arms make the evaluation `inconclusive` (`unit_mismatch`), for every interval
    method. Before, `evaluate_success_rule` silently intersected the arms, so a selective loss of units (e.g. the
    candidate's failed pairs dropped from its report) could pass. The flaw was already on `main` for the
    paired-report metrics. Re-running 1.0.0 or 1.1.0 on mismatched evidence can now give `inconclusive` where it
    gave a pass; matched evidence is unaffected.
  - Coverage: `scripts/research/two_way_interval_coverage.py` (discrete and near-discrete few-seed designs, with
    false-pass rates); tests: `tests/unit/test_two_way_interval.py`.
- **`eidos.assessors.paired-ci@1.1.0`: metric `accuracy`** (superseded by 1.2.0 before release) (identity `classification-report/1:accuracy`, any number
  of classes). The assessor recomputes correctness from label and prediction; it refuses unmatched example ids
  between the arms, label disagreements, a row role that differs from its arm, rows from several seeds under a
  plan without `cluster_by`, and a rule whose guardrails need a different row family. An arm with a failed report
  job is assessed (inconclusive, conformance incomplete) rather than refused, so the plan always gets a verdict. `both-correct-rate` and
  `reference-accuracy` are unchanged. Three new literal author controls (planted gain, identical arms, small noisy
  sample → inconclusive) and four fixtures. Previous release: 1.0.0 (references updated).
- **`eidos.templates.improves-under-budget@1.2.0`: `classification` request.** From two training jobs' held-out
  predictions (per training seed) and a labels artifact to a draft with one report job per arm and seed. Several
  seeds → `cluster_by: "seed"`; one seed → `cluster_by: null`, disclosed as "seed variance not estimated" and listed
  as an unresolved alternative. Unpaired seeds are refused. The catalog summaries and method cards describe the
  path; `docs/release/SUPPORTED.md` lists the assessable metrics.
- `components/build_assessor_fixtures.py` regenerates the assessor fixtures and control suite again (it had drifted
  from the committed fixtures' `required_isolation`).

### Post-pilot robustness (ADR 0017)
- **MCP arguments re-encoded by a client or provider.** The MCP adapter undoes exactly two unambiguous shapes, only
  where a schema it holds says what the value must be (the tool's input schema; for `config`, the release's
  `config_schema`, read with the caller's token): `{"item": [...]}` where an array is required, and a canonical
  numeric string (at most 64 characters) where an integer or number is required. Every change is recorded in the
  command receipt as `normalized_arguments` (path, rule, expected type, original; query results carry the same key).
  Ambiguous shapes are passed through and refused by the lab as before; the lab's validators are unchanged.
  The `config` parameter description asks for exact JSON types. Seen with MiniMax-M3.1-Flash through Claude Code
  (`config.argv` as `{"item": [...]}`, `config.seed` as `"11"`).
- **Stop and deadline abandon the in-flight model request.** Before, `stop_investigation` or the decide activity's
  timeout left a running provider request to finish, and its usage reached the ledger late (pilot smoke arm4-02:
  19.5 s after the deadline). Now each decision has an abort signal: the investigation stops or pauses (checked every
  `abort_poll_seconds`, default 1 s), Temporal cancels the activity, or the decision deadline passes (turn deadline
  less 10%, at most 15 s). No request starts after it, and an in-flight request is cancelled and its connection
  closed. Usage is recorded as uncertain (`aborted` with the abandoned request's worst case), the reservation is
  settled, and the turn ends `cancelled` (stop, pause) or with the no-effect `decision-deadline` outcome.
- `accept_decision` refuses (records unapplied) a decision when the investigation is not active, when a later attempt
  of the same slot has started (`superseded-attempt`), or when the attempt began more than the turn deadline ago
  (`decision-deadline-passed`; `reserve_model_budget` records each attempt's start).
- Review R2 fixes (ADR 0017 addendum):
  - `accept_decision` locks the investigation row (after the turn row, the same order as `complete_turn`), so a
    concurrent stop is never missed.
  - The decide activity watches for a stop or a Temporal cancellation until the decision is accepted, not only during
    the provider request. `execute_decision` never runs a decision whose turn is closed.
  - Reading and settling a turn's model reservations is serialized by the turn row lock (`reserve_model_budget`,
    `record_model_usage`), so a reservation is released once. A settlement naming another turn's reservation is
    refused.
  - A numeric string is normalized only if the converted value is exactly the supplied decimal: `"1e-400"` and
    `"9007199254740993.0"` are passed through to the lab and refused.
  - A replayed command's receipt reports this invocation's `normalized_arguments`, and the committing call's as
    `admission_normalized_arguments`.
- Review R3 fixes (ADR 0017 addendum):
  - Each decide attempt begins with `begin_decision`, which bumps the slot's epoch under the turn lock.
    `reserve_model_budget` and `accept_decision` from an earlier attempt are refused (`superseded-attempt`).
  - A decision that an earlier attempt of the same activity execution got accepted is revoked by the retry: it was
    never delivered to the workflow. A revoked decision is never recovered or executed, and the turn moves to its
    next slot (`attempt-revoked`). Decisions accepted by another run of the turn are still recovered.
  - A request abandoned in flight is charged as uncertain usage at its admitted worst case (`in_flight_estimate`),
    never as zero. Usage from completed responses stays certain.
  - `complete_turn` and a retry settle a still-open model reservation as provisional uncertain usage
    (`uncertain_model_reservations`) instead of releasing it as zero. A late settlement reverses that provisional
    charge (`correction` entry) and books its own usage, so nothing is charged twice.

- **Review fixes (F5, group C): turn and slot authorization (ADR 0017 addendum "review F5").**
  - The TurnWorkflow run that begins a turn owns it (`turn_authorization`, with a generation). A resume by another
    run takes it over and revokes the accepted decisions of earlier runs that were never handed to execution.
    `begin_decision`, `reserve_model_budget`, `accept_decision`, the new `hand_off_decision` (the first step of
    `execute_decision`) and `complete_turn` refuse a run that no longer owns the turn.
  - Before, an old acceptance that committed after its workflow was terminated was recovered and could be executed
    by the resuming run, although it had never been delivered.
  - `begin_decision` ignores a delayed earlier attempt of the same execution (`stale-attempt`). Before, it bumped
    the epoch and superseded the live attempt.
  - Reservation refs carry the slot epoch (`model:<turn>:<slot>:<attempt>:e<epoch>`), so two executions at
    attempt 1 no longer share a ref, and one execution's late settlement cannot erase the other's uncertain charge.
  - Known limitation: a decision handed off by an old run can still be executed by both runs at once, which command
    keys make idempotent.
  - Tests: `tests/integration/test_f5_turn_authorization.py`, `tests/integration/test_cancel_through_acceptance.py`.

### Post-pilot robustness: failed turns and runaway reasoning (ADR 0019)
- **A failed turn no longer stalls the investigation.** Before, a turn that ended `error` was treated as a wait. The
  coordinator polled for ledger news for up to 6 h, with nothing scheduled and no `needs_input` (pilot D12-r3: active
  from 04:07 until the study's deadline stopped it at 04:15).
  - The turn now records why it failed: `failure` with `error_type`, `retryable`, `retry_state` and `message`, on
    the turn result and the `turn.completed` event.
  - A retryable failure gets at most 2 automatic retry turns. Each one is recorded first as
    `investigation.turn_retry_scheduled` and starts after a durable backoff of 60 s × 2^(n−1). Retry turns reserve
    the turns and model budgets like any turn.
  - A non-retryable failure (e.g. provider authentication, unsupported model), or a third consecutive failure,
    marks the investigation `needs_input` at once.
  - `needs_input` now has a machine-readable `code` (`turn-failed`, `refused-proposals`, `coordinator-stopped`) and
    bounded `details`, shown by `get_investigation` (CLI, API, MCP) and in project status.
  - A failed `TurnWorkflow` follows the same policy. Before, the coordinator returned `failed` after three in a
    row.
  - Recorded workflow histories replay (`workflow.patched("failed-turn-policy-v1")`).
- **Runaway reasoning.** A response that reaches the output cap with no answer (`finish_reason: length`, only
  reasoning) is no longer a retryable provider failure. Before, Temporal re-ran the whole decision three times,
  reads included.
  - The decision re-asks once, in every output mode: concise instruction, decision only (no read request), the same
    output cap. With the opt-in `runaway_reasoning_effort` setting it also sends the OpenAI-compatible
    `reasoning_effort` field.
  - A second runaway ends the decision in the explicit no-effect outcome `reasoning-runaway`.
  - Every runaway request counts against `max_requests_per_decision`, the token envelope and the usage receipt
    (`per_request`, `structured_output.runaway`, `runaway_reasks`). The default cap is unchanged.
- **Review fixes (F5, group A) to the failed-turn policy and runaway recovery.**
  - A needs-input wait now ends only on an operator event after the needs-input mark's own cursor (returned by
    `mark_needs_input` and stored in the mark as `cursor`; refreshed when an existing mark is marked again). Before,
    it compared with the last turn's horizon, which does not move while turn workflows fail before creating a turn:
    one operator event stayed "news" on every check, so each wait ended at once, reset the failure counter and
    allowed another batch of automatic retries.
  - A turn left open by a failed turn workflow (e.g. its execution timeout) is closed as failed (`failure.abandoned`)
    and settled before an automatic retry and before a `turn-failed` needs-input wait: its open model reservations
    are charged as uncertain usage and its turns reservation is settled. Before, the retry resumed the open turn
    and bypassed the turns reservation, so repeated attempts counted as one turn. A late acceptance from the
    abandoned workflow is refused (`turn-closed`).
  - An aborted decision (stop, pause, cancellation or decision deadline) keeps its structured-output report: a
    runaway that arrived before the abort prevented the re-ask stays in `structured_output.runaway`.
  - Tool mode: a runaway in the reserved tool-free request after the read-tool budget is recorded and ends in
    `reasoning-runaway` (before: `request-budget-exhausted`, no runaway record).
  - Tests: `tests/integration/test_f5_failed_turn_cursor.py`, `tests/unit/test_runaway_reasoning.py` (stop and
    deadline after a runaway, runaway after the read budget).
- `bounded()` (the decide/execute activity result guard) now truncates `reason` and `control`. Before, an oversized
  result kept them whole and still went over Temporal's payload limit, so every turn of the coordinator stress test
  had failed unnoticed.

### Output and time limits
- `scripts/qualify/live_provider_check.py --max-output-tokens N` (default 4096, unchanged); recorded in the receipt's
  `caps`. The receipt also totals `total_reasoning_tokens`.
- Recommended investigator settings for reasoning models in `docs/release/SUPPORTED.md`: `max_output_tokens: 16384`,
  `request_timeout: 300`, `output_mode: "json"`, and a `turn_deadline_seconds` that covers the decision.
- The text-mode decision loop no longer starts a follow-up request that could outlive the turn deadline (the
  `decide` activity's timeout): explicit no-effect decision with `bounded_outcome: decision-deadline`.
- Checked: the decision's worst-case USD reservation bounds output by `max_requests_per_decision × max_output_tokens`,
  which includes reasoning tokens (they count against the provider's `max_tokens`).
- A model request is started only if its worst case fits what is left of the decision's token envelope
  (`max_tokens_per_decision`): tokens already charged + input estimate + output cap, in every output mode. Otherwise
  the decision ends with an explicit no-effect decision (`request-budget-exhausted`) and nothing is sent. Before, the
  envelope was checked only after each response, so the last request could overshoot it.
- Every request now carries an output cap: `max_output_tokens`, or 4096 when none is configured (before: no
  `max_tokens` sent). The decision's USD reservation prices the same cap.

### Reasoning tokens in the accounting
- Usage receipts record `reasoning_tokens` per request and in total, with `reasoning_accounting`
  (`included-in-output`, `added-to-output`, `not-reported`; never a silent 0). Reasoning tokens reported above a
  request's output tokens, or `investigator.settings.reasoning_tokens_billing: "separate"`, are added to the charged
  output and USD. The ledger's usage detail and the component LLM broker's call records carry them too.
- The component LLM broker's reasoning accounting is an explicit per-provider setting, lab config
  `llm_broker_reasoning_billing` (`{"openai-compatible": "separate"}`; default `included`), no longer inferred from
  the relative counts; with `separate` the reasoning count is added to the charged output. An invalid setting stops
  the broker from starting (logged).

### Read tools in the `json` and `repair` output modes
- The runtime owns the loop: a reply is a decision, a read request (`{"rationale": ..., "read": [{"tool", "args"}]}`,
  at most 4 reads, arguments checked, results bounded and sent back), or malformed (one schema-guided re-ask). Every
  round counts against `max_requests_per_decision`, the token envelope and the usage receipt
  (`structured_output.read_requests`, `structured_output.reads`). A read request in the last allowed request is not
  executed (`request-budget-exhausted`).
- A decision naming a read tool in `calls` is refused by the runtime (one re-ask explaining how to read) and still by
  the lab's validator; read tools are never effects. In `json` mode the response schema admits `read` only when read
  tools are available. Native tool calls are no longer offered in the text modes; `tool` mode is unchanged.
- A leading `<think>...</think>` block is removed before extraction (named repair `think-block`).

## 1.1.0 (2026-11-09)

The remaining harness features of the design. Details and metrics: `docs/release/RELEASE_NOTES_v1.1.0.md`.
Packages: `eidos-lab` 1.1.0, `eidos-wire` 1.1.0, `eidos-worker-sdk` 1.2.0, `eidos-pytorch` 1.1.0 (unchanged), new
`eidos-pyglove` 0.1.0 and `eidos-composer` 0.1.0. No ledger migration.

### Recovery cases of the failure table (ADR 0010)
- **Storage/database lag:** `publish_outputs` re-verifies every staged output (size and full rehash, with a short
  bounded re-check) immediately before publishing, and refuses the whole publication with `CONTENT_UNVERIFIABLE` and a
  per-slot reason (`missing`, `size-changed`, `digest-changed`). The runner re-stages once from its own staging files
  and otherwise fails the attempt with category `storage`. A damaged blob is replaced on re-staging, never trusted.
- **Question changed / release withdrawn mid-investigation:** new `revise_question` (effective at the next turn, admitted
  work by `drain`/`checkpoint-and-stop`/`cancel`, recorded with an exposure row); a withdrawal wakes affected
  investigations, and a withdrawn release admits no new work (`WITHDRAWN_METHOD`).
- **Assessor restart / evidence twice:** order-insensitive evidence-set digest, the same receipt returned on a
  duplicate, an `assessment.deduplicated` event; the assessor activity's command key covers the whole payload
  (a re-evaluating restart used to hit a command-key conflict).
- **Assessment method withdrawn during confirmation:** affected plans are blocked; the assessment fails closed; new
  `reassess_evidence_plan` creates an attributed plan under a corrected, approved assessor, reusing the receipts.
- **Branch request loses its reply:** pinned down by a test (same branch, one child job).

### Separate candidate identity on Linux (ADR 0011)
- Launch specs name an identity: candidate code runs as `eidos-cand`; the assessor keeps the lab identity inside bwrap.
  With a distinct identity the assessor used to fail closed. Receipts report `identity`, `lab_uid`, `candidate_uid`.
- The sandbox now binds the symlinked directories on an interpreter's resolution path (uv's
  `cpython-3.12-<platform>` link); without them the venv interpreter was missing inside bwrap.
- Root setup script `scripts/ci/separate_identity_setup.sh`, documented in `docs/ops/INSTALL_AND_OPERATE.md`; CI job
  `separate-identity` runs a sealed job as `eidos-cand` plus its assessment end to end as root.

### Provider keys in the OS secret store (ADR 0012)
- `lab credentials set|delete|status <provider>` (key from stdin or a hidden prompt, never an argument; status never
  prints it); macOS Keychain via `/usr/bin/security -i` (the key never in argv), Linux libsecret via `secret-tool`;
  no plaintext fallback. `lab start` reads stored keys when `--credentials-stdin` is not given; keys still reach only
  the investigator worker. CI jobs `secret-store-macos` (temporary keychain) and `secret-store-linux` (gnome-keyring).

### Credential broker for component LLM calls (ADR 0013)
- Components that declare `llm` use get a scoped, short-lived, budget-limited grant per attempt (models, requests,
  tokens, USD, expiry, invocation identity) when the project enables `policy.llm_broker`. The broker runs in the
  investigator worker on a Unix socket, reserves against the investigation's ledger before every provider call,
  settles actual usage, records an `llm_call` receipt, and charges uncertain usage after timeouts or a broker crash.
- Sealed runs get a grant only when allowed and reach only the broker socket (bwrap bind; Seatbelt allow rule).
- `eidos_worker.llm` client (standard library, Python 3.9+), injected as `llm`/`context.llm`; `Invocation.llm_grant`
  in the wire contract (eidos-wire 1.1, schemas and fixtures regenerated); example `components/eidos.synthetic.llm-examples`.

### Structured-output repair for local models (ADR 0014)
- `investigator.settings.output_mode`: `tool` (default), `repair` (strict JSON extraction, one schema-guided re-ask),
  `json` (plus OpenAI-compatible JSON mode). Repairs and re-asks are recorded in usage and decision settings.
- Re-measured G4 with qwen2.5 3B and 7B: decisions obtained 1→5 and 0→5 of 5; executable decisions 1→2 and 0→1 of 5.
  The remaining failures are semantic (unknown tools), refused by the lab's validator.

### PyGlove and Composer adapters, behaviour suite (ADR 0015, supersedes ADR 0009)
- `eidos-pyglove` 0.1.0 (pyglove 0.4.5) and `eidos-composer` 0.1.0 (mosaicml 0.32.1, verified event phases with
  hook-phase fixtures and restore), optional worker-side packages outside the lab workspace, each with a pinned CI job.
- `behavior-suite/1` (`components/eidos.behavior.suite`): CheckList-style invariance and direction-of-change templates
  scored from recorded raw predictions.

### Independent review before release (fixed)
A separate read-only review of the diff against v1.0.1 found two blockers and several should-fixes; all were fixed
with tests:
- `lab upgrade` did not enrol the new `svc-llm-broker` principal, so upgraded labs had no broker (now enrolled with
  existing tokens kept; the upgrade-across-releases job checks it).
- A pipeline step bound to a release withdrawn mid-plan made the finishing predecessor's `finish_attempt` fail, so that
  job never became terminal. Refused step admissions are now recorded as blocked steps (inside a savepoint), and pending
  steps count when a withdrawal looks for affected plans.
- Broker: one token per input byte reserved (a 4-characters-per-token estimate could overspend); a `model_usd` scope
  refuses unpriced models; a repeated `request_key` is refused (`REQUEST_REPLAYED`) instead of returning empty text;
  reconciliation locks each call so it cannot double-settle with the broker; at most 32 connections with a 30 s idle
  timeout; the sealed-run allowance needs a researcher or `policy.llm_broker.allow_sealed` (the investigator cannot
  grant it to itself); Seatbelt sandboxes can connect to the broker socket but not replace it; the lab requires
  `eidos-wire>=1.1` and `eidos-worker-sdk>=1.2`.
- Repair layer: non-JSON Python literals are refused cleanly; the re-ask is capped by the decision's remaining tokens.
- Sandbox: symlinked interpreter ancestors that hold the home or credential directories are not bound.

### CI
- 15 jobs (was 10): `separate-identity`, `secret-store-macos`, `secret-store-linux`, `adapter-pyglove`,
  `adapter-composer`. `check_wheel_licences.py --only` checks the adapter wheels.

## 1.0.1 (2026-11-09)

A design-conformance release. It closes three gaps that a check against the architecture (Revision 4) found and that nobody had recorded. It brings no new features and no ledger migration. Details: `docs/release/RELEASE_NOTES_v1.0.1.md`.

### Type checking (mypy in CI)
- `pyproject.toml` has a `[tool.mypy]` configuration: the server tree and the worker packages at Python 3.12, `warn_unused_ignores`, `no_implicit_optional`, and missing-import tolerance only for `torch` and `numpy` (absent from the server environment).
- `scripts/ci/mypy.sh` also checks `eidos-wire`, `eidos-worker-sdk` and `eidos-pytorch` with a **Python 3.9 target**, so 3.10+ syntax or stdlib APIs cannot slip into the worker packages. It uses a pinned mypy 1.19.1, the last release that targets 3.9. The `lint-and-unit` CI job runs it.
- All 91 errors are fixed with annotations and narrowing, and no new `type: ignore` was added. Three of the four older ones are gone:
  - two in `eidos-wire` were unused;
  - `LabClient.upload` now narrows `path` instead of ignoring it.

  The one left is in `eidos-pytorch`: a fallback for a missing `typing.Protocol`, which cannot happen on Python 3.9+. It stays so that package's code does not change. The check found no runtime bug, and behaviour is unchanged.
- `eidos-wire`: the TOML manifest loader now selects `tomllib` with a `sys.version_info` check, which a 3.9 type check can follow. Its behaviour is the same, including the clear error on Python < 3.11.

### Versioned wire JSON Schemas (design § "The worker compatibility promise"; ADR 0008)
- New Pydantic v2 boundary models (`src/eidos/boundary/wire.py`, server side only) generate `schemas/eidos-wire/v1/{invocation,output-manifest,component-manifest}.schema.json` (JSON Schema 2020-12) through `scripts/dev/gen_wire_schemas.py`. Each file carries an `x-eidos` header (generator, wire major), and generation is deterministic. CI fails if regenerating the schemas changes anything.
- Golden fixtures in `tests/fixtures/wire/v1/` are emitted by the real worker SDK code paths (`scripts/dev/emit_wire_fixtures.py`): valid messages, newer-minor messages with unknown fields, and `invalid-*` messages.
  - In the server environment, the valid fixtures validate against the schemas and the invalid ones fail. `jsonschema` is a dev dependency only.
  - In the worker-compat matrix (Python 3.9 with pydantic<2), the SDK emits exactly the golden fixtures. The worker packages stay standard-library only.
- **The server validates received messages against the schema:**
  - the command service's `publish_outputs`, which refuses a violation as `INVALID` with field-level `problems`. The check runs after the terminal and fence checks, so a stale attempt is still reported as fenced;
  - the runner's and the assessor's manifest reads;
  - component registration.

  Before, a manifest with a mistyped field passed `eidos_wire`'s presence checks. For example, a list `receipt` or a numeric `card` was accepted and could fail later with an untyped error. Unknown fields from a newer minor version are still accepted.
- The schema major (1) and the supported worker Python range (3.9+) are stated in `docs/author-guide/worker-sdk.md` and `docs/release/SUPPORTED.md`.

### Deferred integrations recorded (ADR 0009)
- The design's optional **PyGlove** and **MosaicML Composer** adapters are recorded as **deferred for v1**. The supported path is ordinary PyTorch source patches and author-owned trainers. The ADR gives reversal conditions: a user project that needs one, pinned versions, and hook-phase fixtures.
- **CheckList** is recorded as partly adopted: raw predictions are recorded and reused by scorers, `lab.pairs.by_group` provides paired expectations, and `eidos.pytorch.behavior-check` provides one restore-invariance contract. There is no general behaviour-suite capability yet.
- `docs/release/SUPPORTED.md` has a new "Deferred integrations" section.

### CI
- Pushes to `release/**` branches are built.
- `lint-and-unit` adds the mypy step and the schema and fixture regeneration check.

### Packages
- `eidos-lab` 1.0.1. `eidos-wire` 1.0.1 (typing-only changes and the `tomllib` selection above; wire contract unchanged, still `eidos-wire/1`). `eidos-worker-sdk` 1.1.0 and `eidos-pytorch` 1.1.0 are unchanged; only their tests changed.

## 1.0.0 (2026-11-09)

The final v1 software release. It is the same as 1.0.0-rc1 except for the version string and one CI-only change. Released by the owner's decision without the hardware sleep/restart check (R1). Machine sleep and OS restart recovery are a documented limitation; abrupt-stop and service-kill recovery are verified.

### CI
- CI only, no package change: the macOS arm64 job now sets up Go when the Temporal archive cache is cold, so the source-build fallback works there too. The first CI run on `main` (cold cache) failed when the pinned release download returned HTTP 504 and the runner had no Go. `scripts/ci/temporal_server.sh` now says so clearly when no Go toolchain is on PATH.

## 1.0.0-rc1 (2026-11-09; package version 1.0.0rc1)

The v1 **software** release: acceptance areas A1–A15 within the scope of `docs/release/SUPPORTED.md`. It makes **no scientific claim**: A16, the scientific comparison, is a separate later release with `eval-*` tags. Details and evidence: `docs/release/RELEASE_NOTES_v1.0.0.md`.

### Licence and scope
- Licensed under **Apache-2.0** (`LICENSE`, `NOTICE`). Every package declares `License-Expression: Apache-2.0` and ships both files. `research/public/` is excluded.
- New `docs/release/SUPPORTED.md`: Linux x86_64 and macOS arm64; CPU, plus MPS on macOS; Anthropic and OpenAI-compatible providers; Claude Code, Codex CLI and the MCP Python SDK; sealed evaluation on Linux bwrap only. Out of scope, with reasons: Windows, CUDA, distributed training, Lima VM, Claude Desktop.

### Isolation honesty (G3)
- **A same-OS-user Seatbelt receipt no longer satisfies a plan that requires sealed execution.** Profiles now carry `same_os_user` and `hostile_code`. Only `linux:bwrap-sealed` (and the unimplemented Lima profile) are hostile-code profiles.
- On a lab without a hostile-code profile (macOS Seatbelt, or no sealed profile), `freeze_evidence_plan` and `launch_evidence_plan` refuse plans that require sealed execution, with a clear message.
- Isolation receipts and the assessor's receipt record `same_os_user` and `hostile_code_isolation`, derived from the profile name. For job receipts the command service derives them and overwrites any value in the launcher's payload. For the assessor, the assessment activity derives them. Assessments also record each arm's realized isolation profiles and `same_os_user`.
- Fixed: a requirement that named only a profile (`required_isolation.profile` with no controls) was satisfied by any receipt.

### Ledger and upgrades (G7)
- **Migration 0004, writer-generation fence.** Writers that do not declare the current generation are refused on every ledger table. So an old-version writer still running after an upgrade can no longer collide with the migration-0003 event counter or corrupt state. The current command service declares generation 4.
- Fixed a double release of model-budget reservations. `complete_turn` released a turn's unsettled reservations under a separate ref, so a late settlement from a zombie attempt released the same amount again and freed budget held by other reservations.

### Orchestration
- A quiet wait that rolls over (Continue-As-New) no longer starts a model turn with no news, and the max-idle timer counts from the original wait start. The change is patched (`quiet-rollover-keeps-waiting-v1`), and recorded histories still replay.

### Investigator (found preparing the real-model A8 journey)
- The investigation view and the packed context list:
  - the results of the investigator's own recent calls (the `trial_id` returned by `plan_trial`/`plan_branch`, plan ids, validation jobs, export ids, and call errors);
  - its evidence plans with their assessments;
  - the approved (qualified) protocols.

  Before, these ids reached the investigator only inside events, which the packer drops, so `launch_trial`, `launch_branch` and `freeze_evidence_plan` could not be called from evidence.
- The tool guide documents the evidence-plan draft and the trial/branch arguments.

### Defects found by the real-model A8 runs (claude-sonnet-5-5), fixed with tests
- `plan_branch` with a malformed `migration` or a prose `intervention` raised a `TypeError` (HTTP 500). It is now `INVALID` and names the parent's state entries, with an example.
- `author_component` with a malformed manifest raised a JSON decode error (HTTP 500). It is now `INVALID` with the parse error.
- A coordinator that stopped on budget exhaustion or the turn limit left the investigation `active`. The reconciler then restarted it at turn index 0, which replayed turn 1's recorded `begin_turn` receipt and looped on `COMMAND_KEY_CONFLICT`.
  - Now the investigation is marked as needing the operator (patched `terminal-outcome-needs-input-v1`), and the reconciler skips it.
  - A re-dispatched coordinator continues after the ledger's last turn.
- The per-decision `model_usd` reservation priced every token at the output rate. That is about 3× the real worst case, and it ended a run budget-exhausted with a third of its budget unused. The reservation is now a tight worst case.
- The tool guide now documents the component entry calling convention, training-extension binding, and the branch migration, stream and intervention shapes.
- Releases without a manifest `summary` (the built-in `eidos.pytorch.*` components) show their method card's Question and Config paragraphs in the catalog.

### Release review fixes
- `start_investigation` refuses a `model_usd` limit for a real-model runtime without `price_per_mtok`. Without prices no decision reserves or reports USD, so the limit could never apply.
- The needs-input mark of a stopped coordinator is keyed per coordinator run, so it surfaces again after an operator action.
- The per-decision USD envelope uses the runtime's defaults (8 requests per decision). With no output cap, every token is priced as output.
- A manifest authored as an object is materialized as JSON for the validation job.

### Local operations (found on the Mac)
- The supervisor now restarts a PostgreSQL cluster that died (crash recovery). A sixth death within five minutes leaves it `failed` in `run/status.json` and in `lab doctor`.
- `lab status` reports `disk.refusing_new_bytes`, and `lab doctor` fails the disk check while the store refuses uploads and job outputs (below the reserve). It used to warn only.
- `lab init` and `lab restore` refuse a Temporal port block that ends above 32767 (Temporal stores ports as PostgreSQL `smallint`, and the server exited at startup) and API/PostgreSQL ports inside the Temporal block.
- `tests/ops/*.sh` quote the lab executable, so they run from paths with spaces.
- New owner/operator checks: `scripts/qualify/disk_full_check.sh`, `service_failure_check.sh` and `sleep_restart_check.sh`.

### Other
- The assessor's evidence file and its staged copy are mode 0600.
- CI: the setup-uv PyPI fallback runs only when the GitHub release download is actually unavailable. Any other setup-uv failure fails the job (`scripts/ci/uv_fallback.sh`).
- CI checks the licence metadata of every built wheel (`scripts/ci/check_wheel_licences.py`).

### Known-affected earlier tags
- `eidos-v0.2.0` … `eidos-v0.5.0`: qualified assessment of evidence plans was broken (fixed in `8771bb3`, v0.5.1).
- `eidos-v0.4.0` … `eidos-v0.5.1`: sealed checkpoint-and-stop was broken.
- `eidos-v0.4.1` … `eidos-v0.7.0`: a `macos:seatbelt-sealed` receipt could satisfy a plan requiring sealed execution (fixed in 1.0.0).
- Receipts recorded under `macos:seatbelt-sealed` before `b7d9ed1`: their `no-credential-read` control did not cover the home directory.
- `eidos-v0.4.0` … `eidos-v0.7.0`: a late model-usage settlement after `complete_turn` could release a reservation twice (fixed in 1.0.0).
- All earlier tags: a plan requirement naming only a profile, with no controls, was satisfied by any receipt (fixed in 1.0.0).
- All earlier tags: an old-version ledger writer still running after a migration could collide with the new schema, and a dead PostgreSQL was not restarted by the supervisor (fixed in 1.0.0).

### Release record
- G9 final verification passed on tree `ac30bfc3`. Evidence: `docs/release/evidence/g9-*` and `ACCEPTANCE_MATRIX.md` § E.
- Commit IDs were rewritten on 2026-11-09 (metadata only; trees unchanged). Earlier IDs cited in documents map to current ones via `docs/release/COMMIT_ID_MAP.md`.
- CI also runs on pushes to `main`.

## 0.7.0 and earlier

Development freezes. See `HANDOFF.md` and `IMPLEMENTATION_STATUS.md` (rounds 1–17).
