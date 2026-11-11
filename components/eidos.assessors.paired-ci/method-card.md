# eidos.assessors.paired-ci 1.2.0 (assessment; LAB environment; protocol-assessment/1)

**Question.** Given a frozen plan and the paired prediction rows of its arms, does the candidate arm meet
the plan's *charter-derived* success rule? The assessor chooses no threshold.

**Input.** `evidence`: `{"plan": <frozen plan body>, "arms": {arm_id: {"role", "rows": [paired rows]}}}`.
Rows carry `pair_id, cluster, reference_logit, variant_logit, reference_label, variant_label`.
Per-unit metrics: `both_correct_rate` (reference AND variant correct; score > 0 is class 1),
`reference_accuracy`. Unit = pair, paired across arms by `(cluster, pair_id)`, cluster = training seed.

**Ordinary accuracy (1.1.0, ADR 0018).** Metric `accuracy` (identity `classification-report/1:accuracy`,
for any number of classes) reads the rows of `eidos.task.paired-classification-report`:
`example_id, cluster, role, label, prediction`. The assessor recomputes correctness (prediction equals
label, compared in canonical form) and pairs the arms by `(cluster, example_id)`. It fails closed when
the two arms do not cover exactly the same examples in every cluster, when they disagree on a label,
when a row's `role` differs from its arm's, on null, boolean, empty or non-finite labels or predictions,
and when rows from several training seeds arrive under a plan without `cluster_by`. An arm whose execution is
incomplete (a failed report job) is paired on the clusters both arms have; with no rows the evaluation is
inconclusive (no paired units), and the server's conformance check makes the assessment inconclusive.
A plan with one seed per arm and no `cluster_by` makes a conclusion about those trained models. A rule and
its guardrails must use one row family.

**Interval (1.2.0, ADR 0018 "F3b").** Every training seed is scored on the same held-out examples (the
same `example_id` or `pair_id` in every `cluster`), so seed clusters are correlated through the examples.
1.0.0 and 1.1.0 clustered by seed only and could pass an effect carried by a handful of shared examples
(three seeds improving on the same 3 of 100 examples: lower bound 0.03). 1.2.0 treats the design as
crossed, training seed (`cluster`) x example (unit id), for both row families and whether or not the plan
has `cluster_by`: variance = one-way cluster-robust variance by seed + one-way cluster-robust variance by
example (the Cameron-Gelbach-Miller two-way estimator without subtracting the intersection term, so it is
conservative and never smaller than either one-way variance), with a Student t critical value on
Welch-Satterthwaite degrees of freedom from (seeds - 1, examples - 1). One seed: seed variance is not
estimable (`seed_variance_estimated: false`) and the interval is the plain paired t-interval over examples.
The effect reports `method`, `seeds`, `examples`, `variance_components` and `df`; the evaluation reports
`interval`. Recorded 1.0.0 and 1.1.0 results are unchanged: they call the original method.

**Method.** `eidos.domain.evidence.evaluate_success_rule(..., interval="two-way-seed-example-t")` (the
rule's decision standard and guardrails; guardrails use the same interval). Non-finite interval ends (fewer
than two examples) are written as `null` and listed in `unbounded_fields`; the outcome is then
inconclusive. A lab whose domain code lacks the two-way method makes this release fail closed.

**Refuses.** Arms not in the plan or with a different role, duplicate units, unreadable or non-finite
rows, metrics it cannot compute from rows, and (accuracy) unmatched example ids across arms.

**Does not.** Check execution conformance or evidence health (the server combines those), or establish
that the effect is causal or general.

**Qualification.** `controls/suite.json` holds planted-effect and null controls (literal, deterministic,
three literal controls for `accuracy` (planted gain, identical arms, small noisy sample: inconclusive),
plus two 40-repetition stochastic controls expanded by the qualification runner). They were written by
the assessor's author: author tests, not independent qualification.
