# eidos.behavior.suite 0.1.0 (`behavior-suite/1`)

**What it does.** Scores a CheckList-style behaviour suite: invariance tests (the prediction must not change under a
transformation) and direction-of-change tests (the score must increase, decrease, not increase or not decrease),
per test and per group, against **raw predictions** that were already recorded. It never runs a model, so a later
scorer release can re-score the same predictions.

**Inputs.** `cases` (`behavior-cases/1`: `{"suite", "tests": [{"name", "type", "capability", "on", "tolerance",
"min_pass_rate", "expect"?, "pairs": [{"original", "transformed", "group"}]}]}`) and `predictions`
(`raw-predictions/1`: rows `{"id", "label"?, "score"?}`).

**Authoring.** Domain authors supply the transformations and groups; `eidos_behavior.templates`
(`invariance`, `direction`, `suite`) builds the transformed examples and the case table. Predict once over originals
and transformed examples with the project's evaluation component, then run this scorer.

**Output.** `behavior-report/1`: per test `n`, `passed`, `missing`, `pass_rate`, `status` (`pass`, `fail`,
`incomplete`), `by_group` rates, the first failing pairs, and failures per capability.

**Limits.** A passing suite shows the model behaves as the author's transformations expect on these examples; it
says nothing about transformations nobody wrote. Transformations that change the true label belong in direction
tests, not invariance tests. Missing predictions make a test `incomplete`, never a pass.
