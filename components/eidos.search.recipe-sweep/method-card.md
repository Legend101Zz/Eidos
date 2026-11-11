# eidos.search.recipe-sweep — method card

**Question.** Which recipe of a project's own training command does best on *visible validation*? Search is the
purpose; it is not confirmation.

**When to use it.** After `eidos.diagnose.baseline-errors`: diagnosis first, then a broad sweep with a template, then
targeted challengers (a command sweep over the parameters that mattered). A template is the default when the task has
NPZ train and validation arrays: one call, no authored code.

**Templates (release 1.1.0).** `config.template = {"name": "quick" | "broad", "task_type"?, "preprocess"?,
"families"?, "finalists"?, "cv_folds"?, "seeds"?, "rank_by"?, "timeout_seconds"?, "max_rows"?}` replaces `command`,
`space`, `objective` and `recipe` (giving both is refused, as is a `source` bundle). The sweep runs its own recipe runner
(`templates/fit_recipe.py`, numpy; its SHA-256 is recorded) on the `train` and `validation` NPZ ports (`x`, `y`,
optional `ids`; staged as `train.npz` and `validation.npz`):

- *Recipes* cross preprocessing — `none`, train-fitted `standardize`, `per_example_center`, `per_example_standardize`
  — with model families: `linear` (ridge regression, or L2 multinomial logistic regression), `knn`, `kernel` (RBF kernel
  ridge), `mlp` (one hidden ReLU layer, Adam), and `svm` (RBF SVC/SVR) and `gbm` (histogram gradient boosting) when
  scikit-learn is importable in the environment; otherwise those two are left out and listed under
  `template.skipped_families`. `quick` has one setting per family (4 x 6 = 24 recipes), `broad` two or three (52).
  `preprocess` and `families` restrict the cross.
- *Screening.* Every recipe is fitted on train and scored on visible validation for every seed (default: the job's
  seed), in family order. The objective is `validation_accuracy` (maximize) or `validation_rmse` (minimize), from
  `task_type` or the `task` input. The budget defaults to the template's recipe count and can be lowered; a seconds
  budget and the job's wall limit apply as for any sweep. The stopping rule must be `none`.
- *Finalists.* The `finalists` best screened recipes (default 3) are cross-validated within train only (`cv_folds`
  folds, default 3, every seed). Each finalist carries `train_cv`, `finalist_rank` and `transfer_gap` (train-CV minus
  validation, in the better direction). The winner is the best finalist by `rank_by`: `validation` (default) or
  `train-cv`. If no finalist was cross-validated (`finalists: 0`, the budget ended first, or every CV failed), the best
  screened recipe wins on validation with `cross_validated: false` (none under `train-cv`). Cross-validation within train cannot see a difference between the train and validation distributions,
  which is why validation ranks by default; a large `transfer_gap` says the validation ranking and the training data
  disagree.
- *Output.* Every attempted recipe is in `evidence_table` with `stage` `finalist` or `screened`; `best_recipe` adds the
  template and a `reproduce` entry (`fit_recipe.py train --inputs . --out <out> --seed <s> --params <recipe>`, which writes
  `validation_predictions.npz` and `receipt.json`), so the winner can be run as an ordinary project command and locked.

**How (command sweeps).** The caller gives a project command (`config.command.argv`, an argument array) and, optionally, a recipe
template (`config.recipe`, any JSON with `{"$param": name}` entries, written to `{recipe}`). Each recipe is run once per
seed and per fold (`config.seed_policy`) in its own private working directory. The command reads its parameters from
`{param.NAME}`, `{params_json}` or `{recipe}`, the fold from `{fold}` of `{folds}` (the command owns how visible data is
split into folds), and writes numeric metrics to `{out}/receipt.json` (or `command.metrics_file`). Recipes are drawn
from `config.space`: a grid (Cartesian product, sorted parameter names, evaluated in that order) or a random search with
a required seed (the same seed draws the same recipes). A repeated draw is skipped.

**Limits and stopping.** `config.budget` must bound the search (`max_trials` recipes and/or `max_seconds`). The rule in
`config.stopping` is `none`, `patience` (`patience` recipes in a row without improving the best by more than
`min_delta`) or `target` (a recipe reaches `target`). A seconds budget ends the search at a time, so a re-run with only a
seconds budget need not evaluate the same recipes.

**Records.** Every evaluated recipe, including those that failed, appears in `evidence_table`
(`recipe-sweep-table/1`) with its parameters, per-seed and per-fold records, the mean and standard deviation of each
metric, and its rank (best first; ties break to lower standard deviation, then earlier recipe). A recipe that does not
complete all its seeds and folds is failed and unranked. `best_recipe` (`recipe-sweep-best/1`) names the winner or
`status: none`. The receipt's `evidence_digest` covers everything recorded except wall seconds, so re-runs of a
deterministic project give the same digest.

**What it refuses.** Only the ports `source`, `train`, `validation` and `task` are read; a test or held-out split has no
input port. An objective named after a test or held-out split is refused, and a command that reports such a metric is
recorded as a failed evaluation. An unbounded search is refused.

**What each evaluation can touch.** Project files come from a `source` bundle or, without one, from the plain file names
in `command.copy`; nothing is copied implicitly, and a file named after a test or held-out split is refused either way
(a bundle may still carry code named that way, such as `test_helpers.py`). Each evaluation runs in a scrubbed environment
(`PATH`, locale, thread and device settings; `HOME` and `TMPDIR` are its own directories), so inherited credentials do not
reach it. Failure text in the table is redacted, including any value of a variable it did not inherit, before it is cut
to a tail. The only command output ever published is the redacted tail of a failing command's stderr.

**Processes.** Each evaluation is its own process group. On a timeout, a stop request, cancellation of the job, or a normal
exit that left workers behind, the whole group gets SIGTERM, then SIGKILL after 2 seconds, and the sweep waits until it is
gone before the next evaluation starts. A timeout can therefore run up to about 2 seconds past the seconds budget. A
stop request ends the search with `stop_reason: stop-requested` and a table of what was evaluated.

**What it is not.** It makes no claim and is given no test data. Choosing the best of many recipes on validation makes the
winner's validation score optimistic: this is selection, not evidence about new data. Lock the winner, then confirm it
independently on data the search never saw. The command's metrics are self-reports, as for `eidos.project.command`, and
a command can still read any file its operating-system permissions allow, by absolute path, and a child that calls
`setsid` leaves the group: the sweep guards its ports, its copied files, its environment and its reported metrics, not
the project's own code. Use a sealed profile when the project's code is not trusted.
