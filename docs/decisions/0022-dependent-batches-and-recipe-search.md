# 0022: Dependent decision batches and a general recipe-search component

Status: accepted (2026-11-11, round-2 improvement E3; development pilot 1 did not use it). Extends ADR 0007 and F5
(decisions and effect admission).

## Evidence (D1 report, recommendations 5 and 6)

- Arm 4 was slow on model round trips, not compute: sum of job durations was 22.8 / 39.0 / 17.7 s against 608 to 1685 s
  of turns. A decision already allows several calls, but `author_source` only returned its artifact id after the
  decision ran, so running that source took another model decision.
- Several training jobs were admitted from one decision while its summary said "sequentially". Admission order does
  not serialize completion, and the study forbids parallel training.
- Coding agents evaluated tens of settings per invocation; arm 4 evaluated about 3 / 28 / 2.6 configurations per 1800 s.

## Decision

### Dependent batches

- A call's argument can be `{"$ref": "call[<i>].<field>"}` or `{"$ref": "call[<i>].outputs.<slot>"}`, at any depth.
  The persisted decision keeps the reference. The whole batch is therefore recorded prospectively, before any effect,
  with the same stable command key per call as before (`keyed_calls`). `depends_on` is derived by the validator; a
  proposal cannot state its own.
- **Strict validation before acceptance.** `validate_decision` refuses a malformed reference, an extra key beside
  `$ref`, an index outside the decision, a forward or self reference (the only way to write a cycle, so a cycle cannot
  be written), a field the named tool does not produce, `outputs.*` on a tool that starts no job, and a reference in
  `component_ref`, `environment`, `investigation_id` or `purpose`, which are checked at acceptance and so stay literal.
  `accept_decision` also refuses `outputs.<slot>` that the referenced component does not declare (unless
  `dynamic_outputs`). A config holding a reference has its contract checked at execution by `resolve_lock`, since its
  final value does not exist earlier.
- **A reference to a job means "after it has succeeded".** `execute_decision` waits for the job (`run_component`,
  `launch_trial`, `launch_branch`, `author_component`) before resolving it, so `publish_component` and scorers can
  depend on a validation or training job in one decision.
- **Sequential by default.** Jobs of one decision are admitted one at a time: the next waits until the earlier job is
  terminal (one CPU). A decision may ask for concurrency with `resource_policy.max_concurrent_jobs`, only as far as the
  project's operator-set `policy.resources.max_concurrent_jobs` allows; otherwise the proposal is refused. A model
  cannot grant concurrency to itself.
- **Waiting is durable.** `execute_decision` waits in slices (45 s), then returns `pending`. `TurnWorkflow` (patch
  `dependent-batch-v1`) sleeps on a durable timer and runs it again with a `continuation` number. Committed calls replay
  by command key, so nothing is admitted twice; a stop, pause, closed turn or lost run still refuses the next effect,
  and a stop seen while waiting ends the batch with `stopped` and a refused receipt. Near the turn's own timeout the
  workflow passes `final`: calls that cannot start are recorded `BATCH_DEADLINE`. Admitted jobs keep running; their
  lifetime is independent of turns.
- **Failures stop the dependent tail and are recorded.** A failed call or job gives every call that needs it,
  transitively, a `DEPENDENCY_FAILED` receipt. Independent calls still run, as before. Each receipt has its index and
  command key, and calls that used references also carry `depends_on` and `resolved` (path, reference, value). Lineage
  comes from each job's lock, as for any job. A missing output slot records `REF_UNRESOLVED`.
- `nothing_outstanding` is now reported only when a decision started no job. A batch's jobs may all finish before it
  ends; their events are the news a `wait_for_jobs` waits for, and treating this as a failed wait would have refused
  correct batches.
- The decision schema (`DECISION_SCHEMA`, the structured-output schema and `Decision`) adds `resource_policy`, and a
  new wire message `decision-proposal` publishes the reference grammar, with valid and invalid golden fixtures.

### Recipe search: `eidos.search.recipe-sweep@1.0.0`

A standard-library component that runs a project command (`config.command.argv`, with an optional JSON recipe template)
once per recipe, seed and fold, in private working directories, using the worker SDK only:

- Space: grid (Cartesian, sorted parameter names) or random with a required seed; repeated draws are skipped.
- Budget: `max_trials` and/or `max_seconds`, required; an unbounded search is refused. The job's wall limit caps it.
- Stopping: `none`, `patience` (with `min_delta`) or `target`.
- Output: `evidence_table` with every evaluated recipe, including failed ones, ranked (ties to lower standard deviation,
  then earlier recipe), and `best_recipe`. The receipt's `evidence_digest` excludes wall seconds, so re-runs of a
  deterministic project match. A recipe that did not measure every seed and fold is failed and unranked.
- It is exploratory. Only `source`, `train`, `validation` and `task` are read, so test data has no input port; an
  objective named after a test or held-out split is refused, and a command reporting such a metric is recorded as a
  failed evaluation. No claim is made. The winner must be locked before independent confirmation.

It runs without the lab (`study/toolkit/run_component.py`, `build_toolkit.py` copies `components/`), so the standalone
toolkit has the same component, and is installed for arm 4 alongside `eidos.project.command`.

## Not done, and limits

- A failed job's dependents are not retried automatically: a retry is the investigator's next decision.
- The command's reported metrics are self-reports, as for `eidos.project.command`. The sweep guards its ports and
  reported metric names; it cannot prevent the project's own code from reading a file its environment allows.
- Fold splits are the command's own responsibility (`{fold}` of `{folds}`); the component does not partition data.
- A seconds budget ends the search by wall clock, so a re-run with only a seconds budget need not evaluate the same
  recipes.
- No live measurement yet of search density in a pilot; the expected effect is a hypothesis.
