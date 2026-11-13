# ADR 0047 — `prepare_candidate`: the lab prepares an exportable candidate, the investigator locks it

Status: Accepted for implementation (step 3)
**supersedes one sentence of ADR 0038** (below). Resolves review findings 11, 12, 18, 19, 20, 21, 24 and orchestrator decision Q3.

## Context

Turning a sweep winner into a lockable, exportable candidate cost 235 s (a7c5d19 N04) and 828 s with a second author (e503821), plus about 131 s of lock, reproduce and readiness orchestration; N02 abstained in e503821 (and
in the superseded 454a9a6 build) with 1350 s unused because the winner was never exported (discovery-gap report 3.4, 4(d), 5.2). Facts in the code at `86dfe57`:

- A component cannot write a note. `candidate_lock` and `selection_rule` notes are validated by `exploration.check_note`. ADR 0038 gives all arms the same instruction to check visible inference in a fresh process before locking and states
  that the intervention "adds no execution tool, validator, automatic relock or post-submission repair".
- The arm-4 exporter accepts only a job produced by the pinned `eidos.project.command@1.0.9` with exactly one `.pt` or `.pth` checkpoint and a producer receipt, and writes `reproduce_command` from a `project.py train` argv
  (`arm4.export_model_checkpoint`, `grader_reproduce_command`; ADR 0041).
- The evaluator retrains the candidate for every fresh seed under `candidate_command(timeout=120)` and each must reach the practical bar; fresh-process predictions must equal the locked ones (`study/grade_block.py`, `fresh_check`).
- `study/eidos_arms/common.py` `prepare()` installs only `eidos.task.score-classification`; `_check_component_contracts` refuses a call to an uninstalled component at acceptance, and a failed job leaves dependents `DEPENDENCY_FAILED`.
- Nothing in rev 1 compared the locked candidate with the baseline; `select_job` prefers an explicit lock to the unedited baseline, so a noise-fit recipe worse than baseline could be exported on a null card.

## Decision

**Preparation is a macro; the lock is the investigator's act.** The investigator proposes `prepare_candidate {source, seeds}` as the last call of a decision, where `source` is a portfolio `promotion_source` bundle (a `$ref` or an
artifact id). `accept_decision` expands it, after `validate_decision` and before `_check_component_contracts`, with the pure `domain/promotion.expand(call, view, limits)`. Every expanded call then passes the existing contract, phase and
resource-policy checks and the list is re-run through `validate_refs`; the call count is checked against the turn's `max_calls` (default 6, 32 in the study), not a constant. The persisted decision keeps `proposed_calls` and the expanded
`calls`, and each expanded call carries `macro: {tool, proposed_by, basis}` where `basis` is read from the bundle (`one-se-choice` when the portfolio selected the recipe, `explicit-export` when the investigator typed a spec and called
`export`). The study adapter derives `candidate_provenance` (prepared by the macro or authored; selection basis) from the ledger so analysis can separate rule-selected from investigator-selected incumbents.

The expansion, all `eidos.project.command` at the study pin (`investigator.exploration.project_command_ref`; not the 1.0.7 release under `components/`):
(1) `train` seed s1 on `inputs.source`, purpose `open-search`: writes exactly one `model.pt`, `validation_predictions.npz` and a receipt with `fit_seconds`, `validation_<m>`, `validation_<m>_se` and `checkpoint_bytes`; this job is the rule's
evidence. (2) `check`, purpose `reproduction`: inputs source, the model, validation and call 1's predictions; it runs the evaluator's exact `python project.py infer --model ... --inputs ... --out ...` in a fresh process and compares with
the training-time predictions after aligning ids (ADR 0038's check; receipt `readiness_ok`, `readiness_max_abs_diff`, `infer_seconds`). There is no in-`train` self-check. (3) `compare`, purpose `measurement-development`: inputs source,
validation labels, call 1's predictions and the baseline job's `validation_predictions`; a seeded paired bootstrap of candidate-minus-baseline on validation (receipt `paired_delta`, `paired_delta_lo`, `paired_delta_hi`, `paired_n`).
(4) `train` seed s2 with the same config, purpose `reproduction`, scored by its own receipt under the rule. There is no cross-validation job and no scorer-component call, so `score-regression` is not needed; for parity and for the
baseline on regression cards step 3 also installs it in `prepare()`, and `expand()` refuses with the macro's own name in the message if a required release is missing or the baseline wrote no validation predictions.

**First preparation and re-preparation.** A first preparation may `$ref` a portfolio job in the same decision: with no incumbent there is no margin to refuse. Once an incumbent exists, `source` must be an existing artifact whose producer job
has finished, so `expand()` can pre-check the lab margin from that job's receipt (`validation_chosen`, `se_chosen`) against the incumbent's recorded evidence and refuse the macro with the arithmetic before any compute (finding 24a).

**The investigator locks.** When the jobs have succeeded the lab packs a pinned `candidate` fact: validation score and SE, the paired interval against the baseline, readiness, reproduction, fit seconds, checkpoint bytes, the stamp, and a prepared
lock text produced by the pure `domain/promotion.suggested_lock(view)`: `{"job_id": ..., "selection": {metric, split "validation", direction, scorer, data.inputs, margin, reproduce_within}}` with `reproduce_within = max(2 x validation_se, 0.005)`.
The investigator authors the `record_note` (copying, amending or declining). The playbook states the claim rule: an improvement claim needs the paired interval to exclude 0 in the right direction on the candidate's own validation predictions; otherwise
conclude null or abstain, and `select_job` then exports the baseline. Whether lock-text copying causes refusals is a live-smoke measurement; a rule-preset shorthand is the pre-planned escape.

**Supersession.** ADR 0038's sentence "It adds no execution tool, validator, automatic relock or post-submission repair" is superseded for v2: v2 adds one preparation macro, still no automatic lock, relock or repair. The instruction to all arms (check
visible inference in a fresh process before locking) is unchanged; arm 2 has the same components but not the macro (orchestrator decision Q1, disclosed in pilot designs).

**Guards where the facts are.** `expand()` runs at acceptance and cannot read a table an earlier call of the same decision has not produced. The portfolio applies them when it selects, from measured values: evaluator-clock `fit_seconds x 2 <= 120`
(coefficient and clock calibrated by the step-3 evaluator-contract test, which measures wall inside `candidate_command` with OMP, MKL, OPENBLAS and VECLIB thread variables all 1 and includes cold import), `checkpoint_bytes <= 64 MB`, exactly one
checkpoint with a safe ASCII basename, and a preparation cost within 0.8 x the hold-back. `expand()` repeats the check from the portfolio receipt when it exists, and the generated `train` fails its own job above 60 s.

**Adapter.** `arm4.export_sweep_evidence` is pinned to `recipe-sweep@1.1.0` and matches ids ending `_evidence_table`; the portfolio's slot is `portfolio_table`, and the adapter gains explicit entries for the new schemas and bundle digests with the same
provenance checks (job, release, inputs, output role, digest, size). Steps 3 and 4 cannot be exercised live without it, so it ships in the same step as the macro. `common.prepare` installs the portfolio and `score-regression`.

**Not built:** a controller-fallback preparation or lock (orchestrator decision Q3). The first live smoke records how often a finished portfolio is not prepared by the investigator; reopen by a new ADR if that exceeds 20% of runs.

## Rejected alternatives

- Rev 1's macro that records the rule and the lock: it moves incumbent authorship from the investigator to the lab, contradicts ADR 0038's wording and leaves the pilot unable to tell "investigator selected" from "rule selected".
- A component that locks: impossible, no note authority.
- A CV re-score job as evidence (rev 1): selection-data re-scoring; and it needed a lineage the lock gate did not provide.
- Calling `eidos.task.score-regression` in the expansion: not installed by the study; one missing release refuses the whole decision.
- A controller fallback in the first live gate (Q3).
- Refit on train plus validation at export: it makes `validation_predictions` in-sample and weakens ADR 0038's check (deferred, Q4).

## Verification and limits

Tests: pure `expand` (call count against the turn limit; must be last; stamps and `basis`; a missing release or baseline predictions refused by macro name; margin pre-check on re-preparation); a real-ledger integration test (PostgreSQL and Temporal) with a
fixture policy on a classification and a regression practice card: prepared evidence, an **investigator-authored fixture lock** from the prepared text accepted, reproduction recognised, patience counts a sub-noise round; adapter export tests mirroring
`study/eidos_arms/test_sweep_export.py`; an evaluator-contract test running the exported `reproduce_command` for two fresh seeds under `candidate_command(timeout=120)` with all four thread variables set to 1, then `infer`, comparing with the lab's predictions.
Gate: on every dev-subset instance, readiness, the paired interval and reproduction exist with zero model calls; the prepared candidate's test score equals the portfolio recipe's to float tolerance; per-job overhead is measured (my 3-5 s is a guess).

Limits: preparation cost for heavy recipes can exceed the hold-back, so recipes are guarded, not forced; a pickle-based `.pt` relies on an explicit-class unpickler and a digest check; whether a lab job accepts another job's `source-bundle/1` output as `inputs.source` must be
confirmed in step 3; nothing shows the investigator uses the prepared evidence well, which only step 4 can describe.

## Step 3 implementation choices

- Existing policy 2 round accounting and prompt 5/playbook 2 remain defaults. Policy 3 and context policy 4 opt in together in the study investigator configuration; neither reserve changes.
- A source artifact is accepted only from a finished pinned portfolio producer in the same investigation. First preparation may refer to the same decision's portfolio output; re-preparation requires a finished source and recorded score/SE for its margin precheck. Explicit exports without recorded validation evidence cannot bypass this precheck.
- The immutable study project-command 1.0.9 filters booleans out of numeric receipt metrics. A succeeded generated `check` with a recorded `readiness_max_abs_diff` establishes readiness: its implementation raises before producing that metric on a mismatch. The fact reports `readiness_ok: 1.0` from those recorded facts. No pinned component bytes change.
- Macro stamps are recovered from accepted decisions and their execution receipts, not supplied job arguments. Source inclusion pins the installed library bundle at acceptance, checks collisions, and verifies the digest again at execution. Library files stay outside workflow payloads.
- The design's original “60-card harness subset” predates the current registry's holdout restrictions. Step 3 uses the six current design generators with ten instances each: 24 registered design instances and 36 newly generated practice instances on the same development mechanisms. Local deterministic row IDs avoid accessing the production salt. No holdout generator, private controller, holdout burn or live study run is used. This is mechanical preparation evidence, not a new scientific generalisation verdict.
- Cold evaluator wall includes interpreter/import overhead. Calibration reports both wall and fit time separately; a wall/fit ratio for a millisecond-scale fit cannot establish a universal multiplier for heavy recipes. The 120-second evaluator hard limit remains authoritative.

- Prepared lock text is withheld until readiness, paired comparison and a successful second-seed reproduction are all present. Acceptance strips caller-supplied macro stamps; only the lab's expansion can create them. Library collisions include case-folded and file/directory collisions.
- Same-decision preparation repeats measured export and hold-back guards after references resolve, before admitting its first train. The final 60-card run explicitly sets W = 1800 s and verifies both C and H stay at 270 s.
