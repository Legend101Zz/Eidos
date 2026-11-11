# Pilot charter rules

`charters.py` supplies the same operator charter to arms 3 and 4, before any
investigator/model work. `import_bundle` first registers its ordinary exploration
charter; we leave that immutable record intact, commit a separate prospective
charter, and use the latter's ID in `lab_import.json`, arm 3's MCP context and arm
4's `start_investigation`. The exact body, revision and digest are retained in
`charter.json` and the run record. D01 receives an exploration-only operational
charter without success rules. D02 and D12 receive this rule:

```json
{
  "id": "practical-accuracy-gain",
  "metric": "paired-report/1:reference-accuracy",
  "min_effect": 0.02,
  "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
  "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
  "guardrails": []
}
```

The metric direction is `increase`, denominator `held-out examples`. The population
is evaluator-owned held-out examples; visible validation is selection evidence
only. The comparator is the supplied baseline from fresh initialization, seed 11;
the candidate recipe must reproduce from fresh initialization. This information
is in the actual charter, not just this document. The synthetic smoke also uses
this rule to verify prospective charter integration, without claiming access to
hidden examples or an improvement.

The rule is supported by `eidos.assessors.paired-ci@1.0.0`, whose outcome is
`protocol-passed` only when the paired difference's **lower 95% confidence bound
is at least .02**. Below-bar upper bounds fail; overlapping intervals are
inconclusive. This is more conservative than the evaluator's point gain bar.
It follows the paired-CI/minimum-effect approach in the A8 Sonnet evidence.

There are material gaps from the practical bar:

* The built-in assessor consumes paired-report rows, not classification NPZ files
  or the scorer's `per_example` output. It recognizes `reference-accuracy`, not a
  bare `accuracy` metric. For ordinary (including multiclass) accuracy, an evidence
  component must align prediction IDs to allowed reference labels and encode each
  example with `pair_id=id`, `reference_label=1`, and `reference_logit=+1` for a
  correct prediction or `-1` otherwise. Set variant fields identically (unused by
  this rule); use the same example IDs in both arms and an explicit training-seed
  `cluster`. Its output slot must be `predictions`, since AssessmentWorkflow reads
  that slot. The investigator can author such a component through the ordinary
  lab interface. We supply no new scientific scorer or hidden labels.
* The numeric rule cannot prove fresh initialization, enforce baseline seed 11,
  prove that labels were held out, or check the candidate's recipe reproduction.
  These are charter provenance obligations and independent evaluator checks.
  Reusing visible validation in a frozen plan does not make it held out.
* Paired seed clusters require matching baseline/candidate seeds. A seed-11
  baseline alone cannot establish variation across fresh training seeds. The
  evaluator retains its supplied seed-11 comparison plus independent recipe checks.
* macOS same-user isolation is not a qualified hostile-code sealed profile. A
  development evidence plan needs an explicit isolation waiver, retained in its
  receipt. No charter or assessment upgrades the pilot into a confirmatory study.

The shipped integration prepares both projects identically: register the lab
environment; install the built-in paired-CI assessor, planning template and
qualification runner; execute the assessor's shipped seven controls and the
existing journey template qualifier's planted/null/threshold-override controls;
then promote the passing releases for `improves`. Actual qualification records
are saved in `protocol_qualification.json`. These are **author controls, not
independent qualification**. The independent assessor is a separate service from
the investigator and uses the frozen charter, not investigator-chosen thresholds.
The automatic freeze policy admits only qualified/approved protocols.

Commands/interfaces inspected and used:

```text
lab charter commit PROJECT charter.json
lab charter show PROJECT CHARTER_ID
lab cmd commit_study_charter --payload payload.json -p PROJECT
lab cmd register_control_suite --payload payload.json -p PROJECT
lab cmd qualify_protocol --payload payload.json -p PROJECT
lab cmd promote_protocol --payload payload.json -p PROJECT
lab cmd draft_evidence_plan --payload payload.json -p PROJECT
lab cmd freeze_evidence_plan --payload payload.json -p PROJECT
lab cmd launch_evidence_plan --payload payload.json -p PROJECT
```

The adapter invokes these same public commands through `LabClient.command`.
`draft_evidence_plan` pins `charter_id`, revision and `success_rule_id`; the service
copies the rule into the plan. Threshold overrides are rejected. Both arms can
file a claim, draft/freeze/launch an evidence plan and read the assessor verdict.
No evidence plan or assessor is required for an honest abstention.

Source references: `src/eidos/domain/charter.py` (rule types),
`src/eidos/domain/evidence.py` (compilation and CI verdict),
`src/eidos/application/handlers/{study,evidence}.py` (commands),
`components/eidos.assessors.paired-ci/paired_ci/assess.py` (supported metrics),
`src/eidos/orchestration/activities.py` (evidence collection),
`docs/dev-runs/e2e-journey.md` and `docs/release/evidence/a8-sonnet-*.json`.

## Pilot 2
D03 and D06 use classification-report/1:accuracy with eidos.task.paired-classification-report and paired-ci@1.2.0, at 0.01 and 0.30. The CI lower bound must reach the card bar. D04 and D05 require baseline RMSE minus candidate RMSE >= 0.5, including fresh seeds. No built-in assessor supports RMSE reduction (paired-ci supports classification and paired reports). Their charters remain exploration-only; local and GitHub evaluators enforce RMSE and reproduction. No classification surrogate is substituted. This is a material arm-4 capability gap.
