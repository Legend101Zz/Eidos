# Changelog

All notable changes to Eidos. Tags are annotated, created only on CI-green commits, and never moved. Development tags `eidos-v0.x` were freezes, not releases.

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
