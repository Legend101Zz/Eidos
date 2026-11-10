# eidos.templates.improves-under-budget 1.2.0 (planning, implements evidence-plan/1)

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

**Classification path (1.2.0, ADR 0018): two training jobs plus held-out predictions -> assessment.**
For ordinary (including multiclass) accuracy under a charter metric with identity
`classification-report/1:accuracy` (for example rule `min_effect: 0.02`, `paired-difference-ci` at 0.95):

1. Run the baseline and candidate training jobs so that each writes held-out predictions
   (`[{"id", "prediction"}]` JSON, JSONL, CSV, or NPZ `ids`/`predictions`); register the labels as an
   artifact (`[{"id", "label"}]`, ...). Note each predictions artifact id (`get_job` -> `outputs`).
2. Run this template with `{claim, success_rule_id, assessor: {ref: "eidos.assessors.paired-ci@1.1.0"},
   required_isolation, classification: {labels: <artifact>, predictions: {baseline: {"11": <artifact>},
   candidate: {"11": <artifact>}}, classes?, id_field?}}`. Use one entry per training seed; both arms
   need the same seeds.
3. Pass the draft to `draft_evidence_plan`, then `freeze_evidence_plan` and `launch_evidence_plan`. Each
   arm runs `eidos.task.paired-classification-report@1.0.0` once per seed on the same three artifacts
   (it fails closed on any id mismatch, duplicate or non-finite value) and the assessor reports
   `protocol-passed` only if the lower confidence bound of the accuracy gain is at least `min_effect`.

With several seeds the plan has `cluster_by: "seed"`, so the interval reflects training-seed variance.
With one seed it has `cluster_by: null`: the interval reflects held-out example sampling only, which is
disclosed and listed as an unresolved alternative. The predictions predate the frozen plan (disclosed);
the charter rule must predate them. Retraining inside the plan with this path is not yet supported.

**Limits.** Sample size, tuning budget and total research cost are *not* decided here; missing seeds
become an open obligation (the plan cannot be frozen until they are resolved). The plan states matched
*planned* budget only; realized budget is checked from execution receipts.

**Qualification.** Fixtures are author tests. Authoritative use needs executed planted/null controls
for the template-plus-assessor pairing (see the qualification runner).
