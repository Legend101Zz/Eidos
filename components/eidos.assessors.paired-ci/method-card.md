# eidos.assessors.paired-ci 1.0.0 (assessment; LAB environment; protocol-assessment/1)

**Question.** Given a frozen plan and the paired prediction rows of its arms, does the candidate arm meet
the plan's *charter-derived* success rule? The assessor chooses no threshold.

**Input.** `evidence`: `{"plan": <frozen plan body>, "arms": {arm_id: {"role", "rows": [paired rows]}}}`.
Rows carry `pair_id, cluster, reference_logit, variant_logit, reference_label, variant_label`.
Per-unit metrics: `both_correct_rate` (reference AND variant correct; score > 0 is class 1),
`reference_accuracy`. Unit = pair, paired across arms by `(cluster, pair_id)`, cluster = training seed.

**Method.** `eidos.domain.evidence.evaluate_success_rule` (cluster-robust paired difference, the rule's
decision standard and guardrails). Non-finite interval ends (fewer than two clusters) are written as
`null` and listed in `unbounded_fields`; the outcome is then inconclusive.

**Refuses.** Arms not in the plan or with a different role, duplicate units, unreadable or non-finite
rows, metrics it cannot compute from rows.

**Does not.** Check execution conformance or evidence health (the server combines those), or establish
that the effect is causal or general.

**Qualification.** `controls/suite.json` holds planted-effect and null controls (literal, deterministic,
plus two 40-repetition stochastic controls expanded by the qualification runner). They were written by
the assessor's author: author tests, not independent qualification.
