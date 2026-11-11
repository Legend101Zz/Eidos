# eidos.search.recipe-sweep — method card

**Question.** Which recipe of a project's own training command does best on *visible validation*? Search is the
purpose; it is not confirmation.

**How.** The caller gives a project command (`config.command.argv`, an argument array) and, optionally, a recipe
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

**What it is not.** It makes no claim and reads no test data. Choosing the best of many recipes on validation makes the
winner's validation score optimistic: this is selection, not evidence about new data. Lock the winner, then confirm it
independently on data the search never saw. The command's metrics are self-reports, as for `eidos.project.command`, and
a command can still read any file its environment allows: the sweep guards its ports and its reported metrics, not the
project's own code.
