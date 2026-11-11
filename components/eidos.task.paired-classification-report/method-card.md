# eidos.task.paired-classification-report 1.0.0 (artifact-op, implements paired-classification-report/1)

**Question.** None by itself: it is the evidence adapter that lets an ordinary (including multiclass)
accuracy rule be assessed. Given the baseline's held-out predictions, the candidate's held-out
predictions and the labels, it checks that they describe exactly the same examples and emits one arm's
paired rows for `eidos.assessors.paired-ci@1.2.0`, metric `accuracy` (`classification-report/1:accuracy`).

**Inputs.** `baseline`, `candidate` (predictions) and `labels`: JSON records `[{"id", "prediction"}]` or
columns `{"ids", "predictions"}` (labels: `label`/`labels`/`y`), JSONL, CSV, or NPZ `ids` + `predictions`
(labels `y`). Config: `role` (`baseline` | `candidate`, the arm whose rows this run emits), `cluster`
(the training seed both predictions come from), optional `id_field` and `classes`.

**Output.** Slot `predictions` (the slot the assessment workflow reads), JSONL rows
`{example_id, cluster, role, label, prediction}`. The assessor recomputes correctness from the label and
the prediction and pairs baseline and candidate by `(cluster, example_id)`.

**Refuses (fails closed).** Any id in one input and not in another; duplicate ids; null, boolean, empty,
non-integral numeric or non-finite ids, labels or predictions; values outside `classes` when given; an
empty input; a missing `role` or `cluster`.

**Use.** One job per arm and training seed, both on the same three artifacts. With several training seeds,
give each seed's job its seed as `cluster` and plan `cluster_by: "seed"`: the interval then reflects
training-seed variance. With one seed per arm the interval reflects held-out example sampling only and the
conclusion is about these trained models. `eidos.templates.improves-under-budget@1.2.0` plans this from a
`classification` request (see its card). ADR 0018.

**Limits.** It does not establish that the labels were held out, that the predictions came from the
claimed recipes or seeds, or that the two arms had matched budgets: those are plan, charter and
evaluator obligations. Rows contain the labels and inherit the labels artifact's visibility. Fixtures
are author tests, not independent qualification.
