# 0018: An evidence path for ordinary classification accuracy

Status: accepted (2026-11-11, post-pilot improvement I4; the running development pilot does not use it).
Amended 2026-11-11 after review R4 (fix F3b): section 6 replaces the seed-only interval with a two-way
(training seed x held-out example) interval in `eidos.assessors.paired-ci@1.2.0`, for both metric families.
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

### 3. Assessor `eidos.assessors.paired-ci@1.1.0` (superseded by 1.2.0, section 6)

Releases are immutable, so the behaviour change is a new release. 1.1.0 was never released on `main`; its
seed-only interval is the flaw section 6 fixes, so the path below uses 1.2.0, which keeps every check listed here. For `accuracy` the assessor, in addition to the
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

With several training seeds per arm, rows carry the seed as `cluster` and the plan has `cluster_by: "seed"`
(paired seeds must match across arms). In 1.1.0 the interval was then clustered by seed only, which ignores that
every seed is scored on the same examples; 1.2.0 accounts for both (section 6). With one seed per arm
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
 "assessor": {"ref": "eidos.assessors.paired-ci@1.2.0"},
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

### 6. Interval: training seed x held-out example (review R4, fix F3b)

**Finding.** Every training seed is scored on the same held-out examples (one labels artifact; in the
train/evaluate path, the adapter's evaluation pairs drawn from a fixed `data_seed`). Clustering by seed alone
treats the seed means as independent replications, but they share the example sample, so the interval omits the
example-sampling variance. Reviewer's example: three seed pairs improve on the same 3 of 100 examples. Every seed
mean difference is exactly 0.03, the seed-only variance is 0, and 1.1.0 reports CI [0.03, 0.03]: `protocol-passed`
against `min_effect` 0.02. Over the 100 examples the lower bound is about -0.004.

**Options considered.**
- *Two-way cluster bootstrap* (resample seeds and examples independently, Owen's pigeonhole bootstrap). Known to
  be conservative, but with 2 to 5 seeds the seed resampling has only a handful of distinct outcomes, so its
  quantiles are coarse; it needs a pinned random stream for reproducibility, and it is slow for large example
  sets in a dependency-free assessor.
- *Two-way cluster-robust variance* (Cameron, Gelbach and Miller 2011): `V = V_seed + V_example - V_seed∩example`.
  Approximately unbiased, but the subtraction can make `V` negative (it does for small samples), and the usual
  repair is ad hoc.
- *Chosen:* the same two one-way terms **without subtracting the intersection**, `V = V_seed + V_example`, with a
  Student t critical value on Welch-Satterthwaite degrees of freedom. This is the ticket's "per-example differences
  averaged over seeds with an interval over examples, plus a seed-variance component".

**Definition** (`eidos.domain.stats.two_way_paired_difference`). `d` = candidate minus baseline per (seed,
example) unit; estimate = mean of `d`. `V_seed` and `V_example` are one-way cluster-robust variances of the mean,
clustered by seed (`cluster`) and by example (`example_id` or `pair_id`), each with the `G/(G-1)` factor. In a
balanced design they equal `var(seed means)/S` and `var(example means over seeds)/N` exactly. Degrees of freedom:
`V^2 / (V_seed^2/(S-1) + V_example^2/(N-1))`, which lies between `min(S-1, N-1)` and their sum, so a few seeds
widen the interval (t with 2 df is 4.30 instead of 1.96).

**Coverage properties.** Under the crossed additive model `d = mu + a_seed + b_example + e`:
- the true variance of the mean is `σa²/S + σb²/N + σe²/(SN)`;
- `E[V_seed] = σa²/S + σe²/(SN)` (the shared example effects cancel within the seed means' spread) and
  `E[V_example] = σb²/N + σe²/(SN)`;
- so `E[V]` exceeds the true variance by `σe²/(SN) ≥ 0`. **In expectation, under the additive model,** the variance
  estimator is biased upward, never negative, and never smaller than either one-way variance, so it is never less
  conservative than 1.1.0's seed-only variance. The cost is at most a factor √2 in standard error, when the
  seed-by-example interaction dominates. This is a statement about the estimator's bias, not about the interval's
  finite-sample coverage: see section 7 (F5) for designs where coverage is well below nominal.
- If the unit ids do not denote the same examples across seeds (independent evaluation draws that reuse ids
  `0..n-1`, or nested ids), the crossing is spurious but the estimate stays conservative: the example term then
  re-counts within-seed sampling noise that the seed term already contains.
- The interval is a normal-theory approximation. Exact coverage is not guaranteed for binary differences on very
  small samples; the t quantile and the overcount bias it upward.

Monte Carlo (`scripts/research/two_way_interval_coverage.py`, 400 replicates each, fixed seed, nominal 95%):

| design | seed-only (1.1.0) | two-way (1.2.0) |
|---|---|---|
| additive, seed sd 0.02, example sd 0.3, interaction 0.3, 3 seeds x 100 | 0.605 | 0.953 |
| same, 5 x 200 | 0.585 | 0.953 |
| same, 10 x 200 | 0.525 | 0.945 |
| additive, no example effect (seed 0.05, interaction 0.3), 3 x 100 | 0.802 | 0.948 |
| binary accuracy, gain on a fixed 5% of shared examples, 3 x 100 | 0.787 | 0.993 |
| same, 3 x 400 | 0.780 | 0.985 |
| same, 5 x 400 | 0.838 | 0.988 |

**One seed.** The seed term is not estimable (`seed_variance_estimated: false`). With `cluster_by: null` (the
template's one-seed classification plan) the interval is exactly the plain paired t-interval over examples. With
`cluster_by` declared the plan asks for a conclusion across training seeds, so the interval stays unbounded and the
outcome inconclusive, as in 1.0.0 and 1.1.0.

**Where it applies.** `evaluate_success_rule(plan, per_unit, interval=...)` gains the method
`two-way-seed-example-t`; its default stays `seed-clustered-normal`, so assessor releases that call it without the
argument (1.0.0, 1.1.0) reproduce their recorded results. `eidos.assessors.paired-ci@1.2.0` uses the two-way
method for the target metric and the guardrails, for **both** row families (`accuracy` and the paired-report
`both-correct-rate` / `reference-accuracy`), and treats distinct `cluster` values as seeds whether or not the plan
declares `cluster_by` (an undeclared second seed can only widen the interval). The evaluation records `interval`,
and the effect records `method`, `seeds`, `examples`, `variance_components` and `df`. A lab whose domain code lacks
the method makes 1.2.0 fail closed.

Reviewer's example under 1.2.0 as first released (F3b): estimate 0.03, CI [-0.0040, 0.0640] on 99 df:
`inconclusive` (both families). Since F5 (section 7) every seed has the same mean there, so the seed variance is
estimated as zero and the lower bound is unbounded: still `inconclusive`.
A broad improvement still passes: the `accuracy` planted fixture (3 seeds x 300 examples, gain 0.196) has lower
bound 0.143 (1.1.0: 0.168) on 19.6 df.

**Does the pre-existing path have the flaw? Yes.** `eidos.assessors.paired-ci@1.0.0` with
`paired-report/1:reference-accuracy` on `main` (ee46a03): the template's train/evaluate path sets `cluster_by:
"seed"` and `cluster: str(seed)` (`improves_template/expand.py` lines 94, 98, 151 there), and `eidos.pytorch.evaluate`
hands the adapter a split whose pairs come from `data_seed`, independent of the training seed
(`examples/switch-order-project/eidos_adapter.py` lines 98-100; `examples/image-project/eidos_adapter.py` line 85,
`data_seed` 180). The same `pair_id`s are scored by every seed. Running `main`'s 1.0.0 `assess.py` (the domain code
is unchanged from `main`) on the reviewer's example as paired-report rows (3 seeds x 100 pairs, baseline wrong on the
same 3 pairs in every seed, metric `reference_accuracy`, `min_effect` 0.02) gives `protocol-passed`, CI [0.0300,
0.0300], 3 independent units. The fix for that metric is the same 1.2.0 release; 1.0.0's results are not changed.
Plans pinned to 1.0.0 with several seeds should be re-assessed with 1.2.0 (a new assessment; the old one stays on
record).

### 7. Review fixes (F5, review of 40cb1f6)

**B1: a collapsed variance or too few seeds never passes.** Bounded outcomes allow discrete seed effects. Reviewer's
design: each seed gives +1 on every example with p = 0.4 and -1 with p = 0.6 (population effect -0.2). Three seeds
are all +1 with probability 0.064; every difference is then 1, both variance components are exactly zero, and
1.2.0 (F3b) reported the point interval [1, 1], `protocol-passed` against `min_effect` 0.02. No variance-based
interval can tell that sample from a population without seed variation, so `two_way_paired_difference` now fails
closed when seed variation is part of the interval (two or more seeds):
- fewer than `MIN_SEED_CLUSTERS` = 3 seeds (`unbounded_reason: too-few-seed-clusters`): the seed component then
  has one degree of freedom and is zero or near zero far too often;
- the seed component is zero (all seed means equal, `seed-variance-collapsed`), or the whole variance is zero
  (`variance-collapsed`).
In these cases the estimate, `se`, `df` and the components are still reported, the **lower bound is unbounded**
(no pass) and the upper bound is the usual one, so identical arms (every difference zero) still fail a rule that
needs an improvement. A collapsed estimate can therefore end `protocol-failed` or `inconclusive`, never
`protocol-passed`. The author controls are unchanged; the reviewer's R4 example is now inconclusive with
`seed-variance-collapsed` (as are identical arms with several seeds, which still end `protocol-failed`). The price
of keeping the upper bound: in the mirrored discrete design (true effect above `min_effect`, every seed
unfavourable by chance, 6.4% with 3 seeds, 1% with 5) a collapsed sample can end `protocol-failed`. That error is
in the fail-closed direction and is accepted.

**Finite-sample coverage, reported honestly** (`scripts/research/two_way_interval_coverage.py`, 4000 replicates
each, nominal 95%, plans with `cluster_by`; "false pass" = `ci_low >= min_effect` while the true effect is below
`min_effect`; "fail-closed" = share of samples with an unbounded lower bound):

| design | S x N | coverage | false pass | fail-closed |
|---|---|---|---|---|
| discrete seed effect ±1 on every example, p_fav 0.4 (effect -0.2), `min_effect` 0.02 | 3 x 100 | 0.775 | 0.0000 | 0.288 |
| same | 5 x 100 | 0.920 | 0.0000 | 0.089 |
| same | 10 x 100 | 0.943 | 0.0010 | 0.007 |
| near-discrete: a seed gains (p_fav 0.4) or loses on a random 10% of examples (effect -0.02), `min_effect` 0.02 | 3 x 100 | 0.746 | **0.0555** | 0.003 |
| same | 5 x 100 | 0.901 | 0.0107 | 0.000 |
| same | 10 x 100 | 0.938 | 0.0020 | 0.000 |
| near-discrete, p_fav 0.45 (effect -0.01), `min_effect` 0 | 3 x 400 | 0.743 | **0.0843** | 0.001 |
| same | 5 x 400 | 0.936 | 0.0160 | 0.000 |

Coverage counts an unbounded lower bound as covering, so it overstates protection for the fail-closed share. The
exact discrete design no longer passes falsely. **The near-discrete designs with 3 seeds still do, at 5.5% and
8.4% (nominal one-sided 2.5%).** When every seed happens to be favourable (probability `p_fav^S`, up to `0.5^S` for
an effect just below the threshold) the seed means are close but not equal, and the sample carries no sign of the
unfavourable seeds. No interval computed from three seeds can rule this out without assuming the shape of the seed
distribution. **This is a known limitation of 1.2.0 with few seeds.** Plans that rely on seed-level conclusions
should use 5 or more training seeds (false pass 1.1–1.6% in these designs). We tried capping the degrees of freedom
at `S - 1` (the cluster-count rule). It brings the 3 x 100 near-discrete false pass to 1.0% but not the 3 x 400
one (still 8.4%), and it turns the 3- and 4-seed planted-effect author controls inconclusive. It was not adopted
in this round.

**B2: unmatched units are inconclusive (pre-existing on `main`).** `evaluate_success_rule` paired the arms by
intersecting their (cluster, unit) keys, so a selective loss of units passed silently: drop the candidate's failed
pairs from its report and the remaining pairs look like a clear gain. The flaw predates this ADR: it was on `main`
(ee46a03) for the paired-report metrics, and the classification path of 1.1.0 only caught it for the `accuracy`
family. Now any mismatch between the arms' units makes the outcome `inconclusive`, with `reason` and `unit_mismatch`
(counts and up to five examples), for every interval method. Re-running 1.0.0 or 1.1.0 on mismatched evidence
therefore now gives `inconclusive` where it may have given a pass. That is a deliberate fail-closed change: matched
evidence gives the same results as before. An incomplete arm (a failed report job) still reaches the evaluation
with its missing clusters, and so does a mismatch, which is inconclusive like the conformance check.

### 8. Review fix F5c (review R8 #4): no decision standard passes on a collapsed interval

Section 7 claimed that a collapsed variance or too few seeds "never passes". That held for the `interval` decision
standard only. `threshold` is also a supported charter decision standard, and `evaluate_success_rule` decided it on
the point estimate alone, so the reviewer's three-favourable-seed sample (every difference 1, interval unbounded
below) still ended `protocol-passed` under a threshold rule with `min_effect` 0.02.

**Decision: B1's guarantee holds for every decision standard.** With the two-way interval
(`two-way-seed-example-t`, paired-ci@1.2.0), evidence whose interval is unbounded below (`unbounded_reason`:
`too-few-seed-clusters`, `seed-variance-collapsed` or `variance-collapsed`; or `variance-not-estimable`, the
interval unbounded both ways because the plan demands seed variance and there is one seed, or there are fewer than
two examples) is decided by its interval under every
standard, exactly as under `interval`. It is `protocol-failed` if the upper bound is below `min_effect` (identical
arms, so the null author controls still fail), otherwise `inconclusive`. It is never `protocol-passed`. A threshold
rule on evidence with a bounded interval still compares the point estimate, as before. The result surfaces the
distinction: `unbounded_reason`, `decided_by` (`interval-upper-bound` when it failed, otherwise `none`) and, when
it is inconclusive, `reason`. Making it `inconclusive` even when the upper bound fails was rejected: the null author
controls (identical arms) must fail, and the upper bound is the same fail-closed rule the `interval` standard uses.

The original interval method (`seed-clustered-normal`, paired-ci 1.0.0 and 1.1.0) keeps its recorded behaviour under
`threshold`. B1 does not apply to it: it has no collapse rule.

The residual false-pass rates of section 7 (near-discrete designs with three seeds, 5.5% and 8.4%) concern
samples whose interval does not collapse. They are unchanged and apply equally to a threshold rule, which passes on
the point estimate whenever the interval is bounded. A threshold rule is the weaker standard by design: it carries no
interval requirement for samples that do not collapse.

**Tests.** `tests/unit/test_two_way_interval.py`, section 11: the reviewer's case under `threshold` and `interval`,
too few seeds, identical arms (failed by the upper bound), a Monte Carlo run of the discrete design under a
threshold rule (no collapsed sample passes), the scope of the change (a bounded two-way interval and the original
method keep the point-estimate rule).

## Qualification

The assessor's author suite gains three literal controls for `accuracy`: planted gain (`protocol-passed`, lower
bound 0.143 under 1.2.0 for a planted 0.20 gain, 3 seeds × 300 examples), identical arms (`protocol-failed`), and a
small noisy sample (`inconclusive`, 3 seeds × 10 examples). The fixtures add an id mismatch (refused) and a one-seed,
unclustered planted gain (2000 examples, passes). The component and template ship their own fixtures. These are
author controls, not independent qualification. A project still has to qualify and promote the 1.2.0 assessor and
1.2.0 template before a plan using them can be frozen.

For 1.2.0 the paired-report controls were resized, not weakened, because the conservative interval needs more data
for the same decisions: the planted fixture 3 x 12 -> 3 x 200 pairs (at 3 x 12 the reference-accuracy guardrail,
margin 0.05, cannot be decided), the guardrail-violation control 4 x 15 -> 6 x 60, and the stochastic controls
6 x 80 -> 8 x 120 (at 6 x 80 the planted control detects 35/40, below the gate's Wilson bound of 0.85; at 8 x 120 it
detects 40/40 and the null control 0/40).

## Consequences and follow-ups

- Charters that should use the new path name the metric `accuracy` (identity `classification-report/1:accuracy`).
  The pilot's `paired-report/1:reference-accuracy` rule still works with the ±1 adapter.
- Not done: retraining inside the plan on this path. Each report job needs the other arm's predictions, and plan
  references are same-arm only; adding cross-arm references to `_advance` is a separate change.
- Not done: stochastic (repeated, generated) `accuracy` controls in `eidos.qualification-runner`; the literal
  controls cover planted, null and inconclusive behaviour. The generator's paired rows use nested pair ids per
  seed, so the stochastic paired-report controls do not exercise shared examples; the regression tests do
  (`tests/unit/test_two_way_interval.py`).
- The two-way variance estimator is biased upward under the additive model (section 6); that does not make the
  interval conservative in finite samples (section 7). A less biased estimator (full Cameron-Gelbach-Miller with a
  non-negativity repair, or a pinned two-way bootstrap) could recover up to a factor √2 in standard error; it would
  be a new assessor release with its own coverage evidence.
- ~~The domain's silent intersection of unmatched units remains for the paired-report metrics~~: fixed in F5
  (section 7).
