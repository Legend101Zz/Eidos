# eidos.templates.improves-under-budget 1.0.0 (planning, implements evidence-plan/1)

**Question.** Does a candidate recipe (a training-rule change, a weight edit, a data change...) improve
a paired metric over a baseline recipe at a matched resource budget, across independently trained seeds?

**What it produces.** A *draft* evidence plan: two arms (`baseline`, `candidate`), a per-seed list of
jobs (train or branch -> optional post-steps such as a weight edit -> evaluate), `seed_policy`
(independent training seeds shared across arms), `analysis_unit: "pair"`, `cluster_by: "seed"`, planned
budget, tuning/splits placeholders, unresolved obligations and open alternatives.

**What it never produces.** A success bar. The minimum useful effect, guardrails and decision standard
are materialised from the pinned charter rule named by `success_rule_id` when the server compiles the
plan. A request containing `min_effect`, `threshold`, `guardrails`, `decision_standard`, ... is refused,
and the output is checked for the same keys at any depth.

**Request.** `{claim: {kind: "improves", statement, scope}, success_rule_id, assessor?, seeds: [..] |
{count, start}, evaluation: {split, data_seed, pairs}, baseline: {train?, post?}, candidate: {train?,
post?}, parent?: {checkpoint_ref}, common_train?, common_evaluate?, budget?, alternatives_unresolved?}`.
Job `inputs` refer to other jobs' outputs as `"<job id>.<slot>"`, or to the parent checkpoint ref.

**Limits.** Sample size, tuning budget and total research cost are *not* decided here; missing seeds
become an open obligation (the plan cannot be frozen until they are resolved). The plan states matched
*planned* budget only; realized budget is checked from execution receipts.

**Qualification.** Fixtures are author tests. Authoritative use needs executed planted/null controls
for the template-plus-assessor pairing (see the qualification runner).
