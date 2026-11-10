# 0018: An evidence path for ordinary classification accuracy

Status: accepted (2026-11-11, post-pilot improvement I4; the running development pilot does not use it).
Extends the evidence-plan and assessment design (`src/eidos/domain/evidence.py`, `eidos.assessors.paired-ci`,
`eidos.templates.improves-under-budget`).

## Context

The development pilot gives the investigator a prospective charter rule: held-out accuracy gain of at least 0.02,
decided by `eidos.assessors.paired-ci@1.0.0` at 95% (`study/eidos_arms/CHARTER_RULES.md` on `task/dev-pilot-1`).
That assessor reads only paired-report rows (`pair_id, cluster, reference_logit, variant_logit, reference_label,
variant_label`) and knows two metrics: `both-correct-rate` and `reference-accuracy`. Both describe a *reference /
variant pair of inputs* (the switch study's opposite-order pairs). For ordinary multiclass accuracy, an investigator
first had to author an adapter that aligns prediction ids to labels and encodes each example as a fake pair
(`reference_label=1`, `reference_logit=+1` if correct else `-1`, variant fields copied). In a 30-minute envelope no
investigator reached an assessor verdict, so the prospective rule plus independent assessor, Eidos's central feature,
was out of reach for the most common task.

Three properties of the existing machinery shape the design:
- The assessment workflow (`AssessorActivities.assess_plan`) reads every job output in slot `predictions` of each
  plan arm and attributes the rows to that arm's role. The assessor pairs baseline and candidate units by
  `(cluster, unit)` (`evaluate_success_rule`), and silently drops units present in only one arm.
- Pipeline references inside a plan resolve only within the same arm (`_refs` in `handlers/evidence.py`); a job
  input may also be a literal artifact id.
- With `cluster_by` set and a single cluster, the cluster-robust interval is infinite, so the outcome is always
  inconclusive. That is correct for seed clustering, but it means a one-seed comparison can never pass.

## Decision

### 1. Metric: a new, clearly named `accuracy`, not a reuse of `reference-accuracy`

Options:
- **Reuse `paired-report/1:reference-accuracy`** with the ±1 encoding. No assessor change and the pilot's charter
  rule would work unchanged. But the identity would then mean two different things: "accuracy on the reference input
  of a reference/variant pair" in one study and "accuracy" in another, and the records (charter, plan, assessment)
  would say `reference-accuracy` for an experiment that has no reference/variant pairs. The encoding also discards
  the label and prediction, so the assessor cannot check them: it trusts a sign.
- **Add `accuracy`** (identity `classification-report/1:accuracy`) with its own row format. Chosen.

The rows (`classification-pair-rows/1`) are `{example_id, cluster, role, label, prediction}`. The assessor
recomputes correctness itself (prediction equals label, both in canonical form: integers, integral floats and
strings that spell an integer such as CSV `"1.0"` as decimal integers, other strings trimmed; null, boolean, empty, non-integral and non-finite values are refused). Any number
of classes works. A charter defines the metric as, for example,
`"accuracy": {"identity": "classification-report/1:accuracy", "direction": "increase", "denominator": "held-out
examples"}`. The assessor resolves metrics by the identity's last segment (`accuracy`), as before; a rule and its
guardrails must be computable from one row family (mixing `accuracy` with `reference_accuracy` is refused).
`reference-accuracy` and `both-correct-rate` behave exactly as before.

### 2. The adapter: `eidos.task.paired-classification-report@1.0.0`

A built-in, standard-library component (lab environment; NPZ inputs need numpy and are refused with a clear message
where it is missing). Inputs `baseline`, `candidate` (held-out predictions) and `labels`, each as JSON records or
columns, JSONL, CSV or NPZ (`ids` + `predictions` / `y`, the convention of `eidos.task.score-classification`). It
requires the three id sets to be identical and refuses duplicates and invalid values (it fails closed: no example is
ever dropped). Config `role` (`baseline` | `candidate`) selects whose rows it emits, and `cluster` is the training
seed both predictions come from.

Each arm runs it once per seed on the *same three artifacts*. Why not one job that emits both arms' rows? The
assessment attributes rows by the arm whose job produced them, and the rows of an arm must come from that arm's
jobs. Emitting one role per job keeps that attribution honest without changing the assessment workflow, and running
the full three-way alignment in both arms means each arm's evidence was checked against the other's. The assessor
then checks the same thing again independently (below). The output slot is `predictions`, the slot the workflow
reads.

### 3. Assessor `eidos.assessors.paired-ci@1.1.0`

Releases are immutable, so the behaviour change is a new release. For `accuracy` the assessor, in addition to the
existing checks (arms in the plan with the planned role, duplicate units):
- refuses unless baseline and candidate cover exactly the same `(cluster, example_id)` units (the domain's silent
  intersection never applies to this metric), and refuses label disagreements between the arms;
- refuses a row whose `role` differs from its arm's role;
- when an arm's execution is incomplete (a report job failed, so the arm lacks rows or whole seeds), pairs only the
  clusters both arms have, still requiring exact id equality inside them. The assessment is then recorded: with no
  paired units it is `inconclusive`, and the server's conformance check (`incomplete`) forces `inconclusive` in any
  case. Refusing instead would leave the plan in `awaiting-assessment` with no recorded verdict;
- refuses rows from several training seeds under a plan without `cluster_by`: treating thousands of examples from a
  few trained models as independent would understate the variance.

### 4. Clusters and seed variance

With several training seeds per arm, rows carry the seed as `cluster`, the plan has `cluster_by: "seed"`, and the
cluster-robust interval reflects training-seed variance (paired seeds must match across arms). With one seed per arm
there is no seed variance to estimate; the plan has `cluster_by: null`, examples are the independent units, and the
interval reflects held-out sampling only. The conclusion is then about these two trained models. The template
records this in `seed_policy.note`, `disclosures.seed_variance` and `alternatives_unresolved` ("gain specific to these
trained models"). We did not change the existing train/evaluate path, which keeps `cluster_by: "seed"` (one seed
remains inconclusive there).

### 5. One usable path: `eidos.templates.improves-under-budget@1.2.0`, request `classification`

From "two training jobs plus held-out predictions" to a verdict:

```json
{"claim": {"kind": "improves", "statement": "...", "scope": {...}},
 "success_rule_id": "practical-accuracy-gain",
 "assessor": {"ref": "eidos.assessors.paired-ci@1.1.0"},
 "required_isolation": {"profile": "dev:same-user", "controls": [], "waiver": "..."},
 "classification": {"labels": "<labels artifact>",
                    "predictions": {"baseline": {"11": "<artifact>"}, "candidate": {"11": "<artifact>"}}}}
```

The template plans one report job per arm and seed. The investigator then calls `draft_evidence_plan`,
`freeze_evidence_plan` and `launch_evidence_plan`, and the assessment runs on completion. The template refuses
unpaired seeds and, as before, any success bar in the request. The new component's and the template's catalog
summaries (`search_components`) and method cards describe the path, and `lab project create` installs the component
with the other lab-environment built-ins.

The predictions on this path predate the frozen plan. The plan pins their artifacts, and the draft discloses
`predictions_predate_plan`. The rule is prospective only if the charter rule was committed before the predictions
existed (in the pilot, the operator commits the charter before any investigator work). The numeric rule cannot
prove fresh initialization, held-out labels, or recipe reproduction; those remain charter provenance and evaluator
obligations (as `CHARTER_RULES.md` says). Rows contain the labels, so they inherit the labels artifact's visibility.

## Qualification

The assessor's author suite gains three literal controls for `accuracy`: planted gain (`protocol-passed`, lower
bound 0.168 for a planted 0.20 gain, 3 seeds × 300 examples), identical arms (`protocol-failed`), and a small noisy
sample (`inconclusive`, 3 seeds × 10 examples). The fixtures add an id mismatch (refused) and a one-seed, unclustered
planted gain (2000 examples, passes). The component and template ship their own fixtures. These are author controls,
not independent qualification. A project still has to qualify and promote the 1.1.0 assessor and 1.2.0 template
before a plan using them can be frozen.

## Consequences and follow-ups

- Charters that should use the new path name the metric `accuracy` (identity `classification-report/1:accuracy`).
  The pilot's `paired-report/1:reference-accuracy` rule still works with the ±1 adapter.
- Not done: retraining inside the plan on this path. Each report job needs the other arm's predictions, and plan
  references are same-arm only; adding cross-arm references to `_advance` is a separate change.
- Not done: stochastic (repeated, generated) `accuracy` controls in `eidos.qualification-runner`; the literal
  controls cover planted, null and inconclusive behaviour.
- The domain's silent intersection of unmatched units remains for the paired-report metrics (their 1.0.0 behaviour);
  making it strict there would change existing assessments and is left for a separate decision.
