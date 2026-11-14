# Registered rule grammar

These definitions are the contract for a scorecard comparison tool. Registration
stores canonical copies of the selected definition and VETOES, with SHA-256 hashes.
`python -m study.exp.ledger rule DIR` returns the authoritative definition with the
registered overrides applied, plus `extension` and the embedded `vetoes` definition.
Live rule edits affect future registrations only. Every selected clause must pass;
a clause that cannot be evaluated counts as NOT passed. Missing data, unknown names,
empty pairs, zero reference standard error, or an unavailable check never imply success.
The ledger preserves evidence and checks lifecycle prerequisites; it does not calculate
statistics or certify that these clauses passed. The comparison tool and independent
result review must adjudicate them before a keep decision.

## Scorecard fields and metrics

The following field names define the comparison input contract. Numeric aggregates
are in `aggregate.<metric>`. Per-run values are in `individual[].<metric>` and are
paired on `(card_id, replicate)` between incumbent and candidate, within the declared
tier, pool, mode and look. All required pairs must exist. Use equal run weights unless
a future registered rule explicitly defines weights. Failed runs stay in the data.

| Metric | Meaning and scorecard source |
| --- | --- |
| `vrg` | Verified reference-normalised gain, `individual[].vrg`; its run mean is `aggregate.vrg`. Verification and reference normalisation are performed by the scorer before comparison. |
| `mrg` | Measured reference-normalised gain before verification, `individual[].mrg`; mean in `aggregate.mrg`. |
| `verified_rate` | Fraction of runs with `individual[].verified_success == true`, `aggregate.verified_rate`. |
| `error_ratio` | Error against the reference, `individual[].error_ratio`; mean in `aggregate.error_ratio`. Lower is better. |
| `uplift_autopilot` | Gain above the matched scripted autopilot, `individual[].uplift_autopilot`; mean in `aggregate.uplift_autopilot`. |
| `novelty_wins` | Count of verified wins attributable to a novel candidate, `aggregate.novelty_wins`, backed by `individual[].novelty_win` booleans. |
| `false_claims` | Total false verification claims, `aggregate.false_claims`, backed by `individual[].false_claims` nonnegative counts. |
| `dead_ends` | Total failed investigation paths, `aggregate.dead_ends`, backed by `individual[].dead_ends` counts. |
| `tokens` | Total billed input plus output tokens, `aggregate.tokens`, backed by `individual[].tokens` counts. |
| `target` | Alias for `plan.target.metric`: read that numeric field from each run (and its aggregate). The plan declares `direction` (`up` or `down`), positive `amount` in that metric’s units, and `tier`. |

## Statistics and operators

`op` is one of `>=`, `<=`, `==`; `equals` means exact typed equality.
`value` is a literal or `{"plan":"dotted.field"}` resolved against the frozen plan.

| `stat` | Computation |
| --- | --- |
| `mean_delta` | Arithmetic mean of candidate minus incumbent values of the named per-run metric across matched pairs. |
| `signflip_p` | One-sided exact paired sign-flip test: enumerate all sign assignments to the paired candidate-minus-incumbent deltas; p is the fraction with mean at least the observed mean, including equality. `method: exact_paired` requires complete enumeration. |
| `min_card_delta_reference_se` | For each card, mean candidate-minus-incumbent VRG divided by incumbent `individual[].reference_se` (the same positive reference SE for that card); take the minimum, excluding only `plan.named_cards`. |
| `success_count_delta` | Candidate count minus incumbent count of true `individual[].verified_success`; despite metric name `verified_rate`, this threshold is in runs, not rate units. |
| `count` | Sum the named per-run count, checked against `aggregate.<metric>`. Vetoes apply to candidate counts across all tiers. |
| `improvement` | Mean delta of the target metric, multiplied by +1 for direction `up`, -1 for `down`; compare with `plan.target.amount`. |

## Checks

Each check uses the candidate scorecard’s `aggregate` block unless stated otherwise.
An explicit boolean is required for boolean checks. Their underlying test reports,
CI run, and comparison output must be linked in the recorded evidence/review.

| `check` | Required source and meaning |
| --- | --- |
| `tests_green` | `aggregate.tests_green`: all declared tests passed. |
| `ci_green` | `aggregate.ci_green`: every required CI job succeeded for the reviewed code. |
| `default_treatment_golden_unchanged` | `aggregate.default_treatment_golden_unchanged`: golden comparison found no default treatment change. |
| `code_review` | Latest recorded code review’s `verdict`, compared with `ACCEPT`; this is ledger evidence, not a numeric scorecard field. |
| `practice_generators_no_harm` | `aggregate.practice_generators_no_harm`: every declared practice generator comparison showed no loss in its registered metric. |
| `guardrails_clean` | `aggregate.guardrails_clean`: all VETOES clauses passed at that tier, including the plan limits. |
| `heldout_identifier_matches` | `aggregate.heldout_identifier_matches`: nonnegative count of held-out identifier matches; must equal zero. |
| `new_failure_class` | `aggregate.new_failure_class`: true iff a candidate failure class is absent from matched incumbent runs. |
| `cost_ratio` | Candidate `aggregate.cost.usd` / incumbent `aggregate.cost.usd`. Both zero means 1; positive candidate cost with zero incumbent cost means infinity. Missing cost fails evaluation. |
| `within_plan_cost_and_time` | `aggregate.within_plan_cost_and_time`: totals `aggregate.cost.usd`, `aggregate.live_runs`, `aggregate.quota_weekly_percent` and `aggregate.time_seconds` remain within `plan.budget` limits (`time_seconds` must be declared when used). |

## Selection and registered parameters

`tier` selects T0, T1, T2 or T3. `all` covers every recorded tier, and `planned`
requires each tier listed in `plan.tiers`; neither means an arbitrary convenient tier.
`pool: evaluation_rotation` selects plan tier records with that exact `pool`;
`mode: autopilot` selects scripted autopilot runs (`individual[].mode`). Missing
selection evidence fails the clause. Other scorecards cannot satisfy that selection.

`when.extension` resolves against the registered rule choice’s boolean `extension`.
`when.model_free_suffices`, if used, resolves against the plan and defaults to false.
Missing booleans do not disable non-inferiority: R-enable always has a VRG clause.
With `plan.model_free_suffices: true`, its effective VRG clause instead requires
T1 autopilot delta VRG >= -0.01. Otherwise it requires delta VRG >= -ni_margin at
each planned tier (default ni_margin 0.10). The target clause always also applies.

`each_look: true` requires the threshold separately at the initial T3 evaluation
and the single pre-declared extension, if taken. A look is one complete planned
card/replicate batch, identified by `individual[].look` (`initial` or `extension`).
`except_plan_named_cards: true` permits only ids in frozen `plan.named_cards`
(default empty). `unless: plan_declared` on cost_ratio permits a pre-registered
`plan.cost_ratio_exception` object with numeric `max_ratio` and nonempty `reason`;
the declared ratio remains a bound and planned cost/time vetoes still apply.

`override` names a registered value allowed by `overridable`; unknown names,
nonfinite numbers, booleans and values outside inclusive min/max are refused.
`override_scale: -1` converts the positive R-enable margin into a negative floor.
R-behav delta is 0.10–0.50 (default 0.15); R-disc delta 0.05–0.30 (default 0.08);
R-enable ni_margin 0.02–0.15 (default 0.10). R-mech and R-infra allow no overrides.
`inherits: ["VETOES"]` requires every clause of the registered veto snapshot.
