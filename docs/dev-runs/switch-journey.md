# Switch-order journey: dev run

Run: 2026-11-08T07:44:23, project `sw1`, lab API http://127.0.0.1:8865. Full record (every job id, artifact id, receipt metric, timing): `journey-run.json` (path given by `--out`; default `/workspace/.eidos-dev/h1/journey-run.json`).

Rerun from a clean wheel install: `bash tests/journeys/run_ci.sh` (CI job `switch-journey`); against an existing lab: `EIDOS_HOME=<lab> EIDOS_TORCH_PYTHON=<python with torch+numpy> <lab venv python> tests/journeys/switch_journey.py`.
Local run on the current code (fresh wheel install and lab, CPU torch 2.15.0, hosted Linux): **all 8 steps pass, 154/154 checks, 4 m 42 s** for the journey itself.

Step 7 is **fixture-only**: a deterministic scripted policy decides every turn, so it validates the loop's mechanics (admission, charging, crash recovery, reconstruction, reattach), not any model's behaviour.

## Steps

| step | title | status | seconds | checks ok/total |
|---|---|---|---|---|
| 1 | project, environments, built-in components, conflict and absent-phase detection | pass | 38.64 | 22/22 |
| 2 | charter before exploration; override rejection; exposure-informed amendment | pass | 0.1 | 8/8 |
| 3 | train/evaluate/reproduce, branches, edits, learning rule, architecture change, data generation | pass | 54.73 | 46/46 |
| 2b | exposure-informed charter amendment preserving both rules | pass | 0.03 | 6/6 |
| 4 | fresh-process behaviour check and export; wrong builder refused | pass | 9.72 | 8/8 |
| 5 | qualification, promotion, claim, plan, assessment, withdrawal and support | pass | 25.09 | 35/35 |
| 6 | lab.pairs.by_group reused unchanged on the image project (second project/environment) | pass | 22.87 | 11/11 |
| 7 | FIXTURE-ONLY autonomous investigation (scripted policy, no LLM): loop, kill, recovery, reattach | pass | 129.93 | 18/18 |

## Key numbers

| quantity | value |
|---|---|
| both_correct_unedited_45 | 0.2625 |
| reference_accuracy_unedited_45 | 0.984 |
| both_correct_edited_45 | 0.844 |
| reference_accuracy_edited_45 | 0.919 |
| both_correct_unedited_180 | 0.7185 |
| both_correct_edited_180 | 1.0 |
| reference_accuracy_edited_180 | 1.0 |
| both_correct_arch_change_90 | 0.0 |
| no_change_fingerprint | cb561d0046d442d631f9f093a103207553ff62124b3c2236ddc702a1607a8eb0 |

## Assessment (charter rule `early-repair` pinned at revision 1, not retuned)

* outcome **inconclusive**, conformance **conformant**; paired both-correct effect 0.6452 (95% CI 0.5484 to 0.7421, 3 independent seeds, 12000 pairs per arm).
* guardrails: paired-report/1:reference-accuracy violated (change -0.0277, CI -0.0643 to 0.0089)
* assessment id `assess_54aed698892a7a7550cbcc37`

## Withdrawal drill

* withdrawal `withdrawal:eidos.assessors.paired-ci@1.0.0:1`: affected assessments ['assess_54aed698892a7a7550cbcc37'], suspended approvals ['approval:eidos.assessors.paired-ci@1.0.0'], reassessment requests ['reassess:assess_54aed698892a7a7550cbcc37:withdrawal:eidos.assessors.paired-ci@1.0.0:1']
* support after: `supported-with-affected-justifications`

## Investigator kill (fixture-only investigation)

* SIGKILL pid 20312 at turn_inv-sw1-24014_1 right after job(s) ['job_89208fc6195b4786'] were admitted (turn status after kill: [('turn_inv-sw1-24014_1', 'open')]); supervisor restarted the worker as pid 21718.
* final: concluded after 11 turns; budget ledger: `{"trials": {"limit_amount": 24.0, "reserved": 0.0, "used": 8.0, "uncertain": 0.0}, "model_tokens": {"limit_amount": 500000.0, "reserved": 0.0, "used": 29868.0, "uncertain": 0.0}, "turns": {"limit_amount": 30.0, "reserved": 0.0, "used": 11.0, "uncertain": 0.0}, "compute_seconds": {"limit_amount": 2400.0, "reserved": 0.0, "used": 13.69, "uncertain": 0.0}}`

## Lab defects found by the journey and fixed on `task/switch-journey`

| file | observed failure | fix |
|---|---|---|
| `src/eidos/domain/resolver.py` | `train` refused to run without `parent_checkpoint` (missing input) | mirrors the lead's `optional_inputs` change; the journey's staging moves `parent_checkpoint` to `optional_inputs` until the manifest does |
| `src/eidos/application/handlers/runs.py` | `no binding for required capability pytorch.adapter/1` for every project-env component (also `target.source/1`, `lab.domain.*`) | environment-provided capabilities are recorded in the lock instead of resolved as releases |
| `src/eidos/application/handlers/queries.py` | `compare_trials` never marked branch siblings as sharing training history (`lock.config.branch` has no `parent`) | read `independence_unit` from the branch record |
| `src/eidos/orchestration/activities.py` | `AttributeError: CompletedProcess has no attribute status` in `assess_plan`; plans stuck in `awaiting-assessment` | read the `OutputManifest` from staging |
| `src/eidos/investigator/activities.py`, `orchestration/workflows.py` | SIGKILL of the investigator-worker left the turn open for the 600 s start-to-close timeout | investigator activities heartbeat every 5 s; TurnWorkflow sets a 30 s heartbeat timeout |

## Notes and defects recorded by the run

* binary .ckpt fixtures (git-ignored, generated by components/build_fixtures.py) cannot travel in the CLI's text source bundle; these fixtures were excluded from the build job: {'eidos.pytorch.behavior-check': ['fixtures/wrong-builder.json', 'fixtures/right-builder.json'], 'eidos.pytorch.evaluate': ['fixtures/evaluate-small.json', 'fixtures/evaluate-unknown-builder.json'], 'eidos.pytorch.train': ['fixtures/branch-no-change-exact.json', 'fixtures/branch-undeclared-state.json'], 'eidos.pytorch.weight-edit': ['fixtures/zero-recorder-weight.json', 'fixtures/unknown-entry.json']}
* extension source is mounted via an absolute python_path to components/lab.learning.group-balanced-ema; the published release is not what is loaded (train only mounts 'extension.source/1' from a lock bundle)
* template draft arms are multi-job pipelines (train+edit+evaluate); launch_evidence_plan runs one component per arm, so arms were resolved to one evaluate run per (arm, seed) over the checkpoints trained in advance
* step 7 is FIXTURE-ONLY: a deterministic scripted policy ('switch-order-scripted') decides every turn; it validates loop mechanics, not model behaviour
* (helper run only, superseded) step 7 needed a steer; the defect below is fixed and the lead's re-run needed none.

## Failed checks

none

## Restoration on current code (what changed in the journey, and why)

Scientific assertions are unchanged: both-correct 0.2625 unedited at step 45 and 0.844 after the weight edit, reference accuracy 0.984 and 0.919,
exact no-change continuation (fingerprint equal to the uninterrupted control), wrong builder refused, promotion refused without a passing
qualification, the inconclusive guardrail outcome, withdrawal with affected/suspended/reassessment, scorer reuse on the image project, and the
step-7 worker SIGKILL with recovery. The counts went from 149 to 154 because of one added check (below) plus checks that were already present in steps 1 and 3.

| drift | change |
|---|---|
| `eidos.pytorch.train@1.0.0` pinned; the checkout ships 1.2.0 | pin updated |
| Intermediate `checkpoint-step-N` slots are plain outputs, not branchable (`launcher.py` publishes only declared checkpoint slots as `checkpoint` role) | **Faithful option chosen: branch from a run that stops at step 45** (`train:stop-at-45`, same seed and config, `steps=45`, final `checkpoint` slot). The alternative (declaring intermediate slots in the training config) is not possible: the slot set is the manifest's `output` list. The control still runs 180 steps and publishes its own step-45 and step-180 checkpoints. **Added check:** the stopped run's step-45 checkpoint has the same digest as the control's step-45 checkpoint, so the parent is the control's step-45 state byte for byte. The no-change comparison still uses the 180-step control's `state_fingerprint`. |
| "a branch and the uninterrupted run it was forked from share training history" compared the branch with the 180-step control | now compares with the stopped-at-45 run, which is the run the branch is forked from (the control is not its parent any more). Assertion text updated accordingly. A branch of the control's own checkpoint is no longer possible without a declared slot. |
| Runs default to `linux:bwrap-sealed`, which cannot see journey-owned extension code under `tests/journeys/support` and `components/` named by `python_path` | the three runs that load such code (absent-phase, phase-present, learning-rule train and its branch) request `dev:same-user` with a recorded `isolation_waiver`. Everything else keeps the sealed default. |
| `lab project create` installs the four lab-env built-ins | the journey's `LAB_ENV_COMPONENTS` now lists all four, so its installs are the identical bundle in the `lab` environment (idempotent since e822744); the image project installs `lab.pairs.by_group` the same way. |
| Evidence plans require `required_isolation` | the plan request carries the profile and a waiver (`dev:same-user` is weaker than the available sealed profile); the second plan in the withdrawal check copies `required_isolation` from the first, so the freeze is refused for the suspended assessor (`QUALIFICATION_MISSING`) and not for an unresolved obligation. |
| `launch_evidence_plan` maps an arm to a list of job ids | the journey iterates the list (one job per arm here). |
| Step 7 wake-up (see defect 1; REMOVED after the fix) | after the scripted policy has filed its claim and requested the export, and the loop has been idle for 20 s, the journey sends one `steer_investigation` to wake it. Guarded by this investigation's own `claim.filed` and `model.exported` events. |

## Product defect found

1. **The scripted fixture investigation stalls for up to six hours after its last stage.** Since `04e232f` ("Idle polls spend no model turn")
   a quiet idle poll starts a turn only if `check_news` finds a news event (`job.*`, `evidence.ready`, `assessment.committed`, ...) after the
   last turn's horizon. `switch-order-scripted` files its claim and requests `export_model` in a turn that ends `wait_for_jobs` with no job
   launched; `claim.filed` and `model.exported` are not news types, so nothing starts the concluding turn until `max_idle_turn_seconds`
   (6 h). Evidence: event log of the stalled run: `turn.completed (waiting)` at seq 480 followed by no event for more than five minutes;
   a manual `steer_investigation` immediately produced `turn.started` and the investigation concluded. Either the policy should conclude
   in that turn, or `claim.filed`/`model.exported` should count as news, or a `wait_for_jobs` with nothing outstanding should not park the loop.
   **Fixed by the lead:** the policy now files the claim, requests the export and concludes in one decision. The journey's steer workaround is
   **removed**, so a recurrence fails step 7 instead of being papered over. The lead re-ran it with `tests/journeys/run_ci.sh`: **154/154 checks, 8/8 steps, 254 s**, with no steer.
   **Open (product, general):** a real-LLM investigator that ends a turn with `wait_for_jobs` and nothing outstanding would also idle until
   `max_idle_turn_seconds`. This is recorded in HANDOFF.

The earlier `submit_assessment` regression and the canary-permission finding no longer reproduce in this run (step 5 passes with its assessment;
no doctor check is part of this journey).
