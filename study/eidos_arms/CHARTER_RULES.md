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

### Conclusion claim states and the `eidos-answer/0.2` mapping (ADR 0021)

The lab records a conclusion's claim state from its evidence and assessor state; the model cannot select it.
`arm4.map_claim_state` maps that state explicitly onto the answer. The independent evaluator still decides; this
mapping only states what the lab's evidence shows.

| Conclusion claim state | `status` | `claim_type` | `scope` |
|---|---|---|---|
| `protocol-passed` (conformant, healthy assessor pass) | `improvement_claimed` | `recipe_improvement` | prefixed with the rule and assessment |
| `candidate-submitted`, positive point estimate | `improvement_claimed` | `recipe_improvement` | ends with `pending independent confirmation` |
| `candidate-submitted`, no positive estimate or no measurement | `inconclusive` | `no_positive_claim` | unchanged |
| `not-supported` (failed assessment) | `no_improvement_found` | `no_positive_claim` | unchanged |
| `null-reported` (the model reports a null) | `no_improvement_found` | `no_positive_claim` | unchanged |
| `abstained` (the model abstains) | `abstained` | `no_positive_claim` | unchanged |
| `no-claim`, or a conclusion with no claim state | the model's own fields | | |

Each mapped answer records the state and the model's status in `deviations`, and `selection_receipt.json` keeps the
full claim state. A submitted candidate is not an improvement confirmed: its point estimate, uncertainty, population
and unfulfilled obligations stay on the ledger, and only an assessor pass gives `protocol-passed`.

`assess_candidate` goes from a locked candidate, baseline and this rule to a launched plan. It refuses selection data
(`data_provenance.split: selection`) and any labels the charter does not attest as held-out, so visible validation
remains selection evidence: charters in this pilot attest no investigator-reachable held-out labels, and arm 4
concludes with a candidate submitted for confirmation unless an assessor passes.

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
