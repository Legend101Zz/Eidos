# ADR 0044 — Diagnostics v2: the steering fix first, a reduced profile later, and incumbent error analysis

Status: Accepted for implementation (step 0). Extends ADR 0024.

Number 0043 is reserved for the practice-harness draft being implemented separately; this ADR keeps its coordinated draft number 0044. Only the step 0 steering fix is implemented here. Step 5 remains future work.
Resolves review findings 2 (wording), 5, 25 and 29. The steering fix is step 0; the profile and `incumbent-errors` are step 5, after the first live smoke.

## Context

In five development gates arm 4 never surfaced the structure of N04: no correlation screen, PCA, importance-based selection, mixture model or MLP ensemble appeared in any run, while
coding runs that did look reached 0.230-0.273 gain against 0.162-0.202 (discovery-gap report 3.1). `eidos.diagnose.baseline-errors` tests train-to-validation transfer shift with ridge and
k-NN probes. On N04 and N01 it ranked `no-transfer-shift-detected` first and `per-example-gain-change` second in all five gates, and the investigator spent rounds on row-wise features.
The accurate statement is that it never surfaced structure hypotheses: row-wise preprocessing genuinely helps N04 by about 3 points (report 9), so "steers wrongly" overstates it. In
`baseline_errors._hypotheses` the no-shift entry scores `min_effect`, so unsupported hypotheses are still listed behind it with their raw scores.

The independent review found rev 1's 13-hypothesis profile the most card-shaped element with the weakest causal link to the observed gain: the portfolio, not the profile, brings
screening and mixtures; ranking by effect divided by SE tracks power; shadow-importance flags are power-limited at n = 150-350; a 5% null rate per hypothesis is about 49% family-wise
across 13; and its cost estimates were optimistic (ET permutation importance with shadows over 80 columns x 10 repeats on 750 rows took 21.5 s; a 100-permutation ET domain test would
take about 335 s [reviewer-measured, synthetic data]).

## Decision

**Step 0: `baseline-errors` 1.1.0.** Keep 1.0.0 byte for byte as `components/eidos.diagnose.baseline-errors@1.0.0/` (ADR 0024 addendum). In 1.1.0, when the top hypothesis is
`no-transfer-shift-detected`, hypotheses with `supported: false` are not listed as steering candidates and the receipt carries `next`, pointing the investigator at the portfolio's class
contrasts (and at the profile once it exists). The invariance probes stay: they are the only part that separates an additive offset from a gain change, which D03 depends on.

**Step 5: `eidos.diagnose.data-profile` 1.0.0, reduced.** Read-only, numpy and scikit-learn, inputs `train`, `validation`, optional `task`, `predictions`, `groups`; no other port, and an array
name with a word beginning `test`, `holdout` or `heldout` is refused before a file is opened. Hard cap 60 s; rows at most 1,500 for the importance section. Every section reports
`status` (`done` or `skipped-budget`); a skipped section is never ranked and never read as "not supported". Sections:
- shadow-importance noise columns (ExtraTrees, 100 trees, permutation importance, five repeats, row-permuted shadows of up to 40 columns). The statement is "not distinguishable from shadow
  noise at this n" with `low_power: true` below n = 500; it is never worded "uninformative";
- effective rank (correlation eigenvalues above the Marchenko-Pastur edge, count below 1e-6) and redundancy clusters of columns with abs(rho) >= 0.9 (distance 1 - abs(rho) <= 0.1);
- train/validation shift (logistic domain classifier on standardised features, out-of-fold AUC with a DeLong interval; per-feature KS with Benjamini-Hochberg; an ET AUC point estimate only; at
  most 20 label permutations, on the logistic model), implemented in `eidos_recipes/shift.py` and shared with the portfolio's `scheme: auto`;
- class balance (a fact, not a hypothesis) and the selection noise floor `se_floor`.
Cut from rev 1 as low value for the first build: count-like and heavy-tailed column typing, the label-noise estimate, label shift. The linear-versus-nonlinear gap, the additive-versus-interaction gap
and per-class multimodality move out of the pre-model profile: round 1 computes them as by-products with better evidence (`class_contrasts` in the portfolio table, ADR 0046).

The profile does not rank hypotheses. A pre-model ranking by z-statistic is power-driven; the ranking that steers the investigator is the post-round-1 `class_contrasts`, which estimate achievable
gain in the task metric with a paired interval. `hypotheses[]` keeps the `baseline-diagnosis/1` field names (`cause`, `score`, `statement`, `prediction`, `falsifier`, `suggested_next`, `evidence`,
`rank`, `id`, `supported`) so existing readers work, with `score` an effect size in natural units and `rank` a fixed evidence tier. False support is controlled family-wise: four tested sections at
alpha 0.01 with Holm, and the acceptance target is at most 5% of null cards with any supported finding, measured on the harness's control generators. Priors only size grids; they never remove a class.

**Step 5: `eidos.diagnose.incumbent-errors` 1.0.0.** Read-only (`observe`), no refit. From `recipe-oof/1` (stored from step 2), train and validation it reports per-class and per-slice error of the incumbent,
disagreement among the top-k recipes, rows wrong under every recipe (label-noise candidates, labelled as candidates), residual-feature relations for regression and calibration. It is the post-round-1
analogue of the coding agents' exploratory loop and the source of refinement hypotheses. Jobs of components under `eidos.diagnose.` are observations and are excluded from challenger accounting (ADR 0046).

## Rejected alternatives

- Extending `baseline-errors` in place for structure discovery (its question is narrower and its release is digest-pinned).
- Using the profile to prune the portfolio (a mis-profile would be unrecoverable; the only diagnostic we had misdirected the search).
- A max-correlation screen as the noise detector: N04-shaped, and it would flag every column of `make_friedman1` (N03).
- Building the full profile before the first live smoke: the largest validation burden for the smallest expected discovery gain.

## Verification and limits

Step 0 tests: a no-shift fixture with positive-scoring unsupported hypotheses (not listed as steering; `next` set); `planted-offset` unchanged; the 1.0.0 digest unchanged; held-out guard fixtures. Acceptance: an
offline run of 1.0.0 and 1.1.0 on the public N04, N01 and D03 train/validation shows no unsupported steering candidates on N04/N01 and an identical list on D03; no model calls.
Step 5 tests: each section against planted ground truth; section status and caps; family-wise false support on controls; the held-out guard; the time cap; planted error structure for `incumbent-errors`. Acceptance on
holdout generators: noise-column recall and precision against the oracle, rank error where known, shift detection with a false-trigger rate, and the family-wise null target. These thresholds are proposals fixed
in the method card before any holdout generator is scored.

Limits: quick probes on bounded rows can miss structure only a deep model expresses; support statements are not exclusive; the profile adds cost (up to 60 s) and one decision; whether any of it improves discovery
end to end is a hypothesis that step 4's live smoke and step 5's optional rerun can only describe.

## Step 0 implementation choices

The no-shift sentinel remains in `hypotheses` with `supported: false`; it describes an observation, not a steering candidate. All other unsupported entries are omitted only when that sentinel ranks first. Both the report and receipt carry the same human-readable `next`; raw numerical evidence remains in `invariance_probes`. On a supported top cause the entire list is unchanged. The hint uses the existing recipe sweep immediately and names portfolio class contrasts and the profile conditionally because those components ship in later steps. No schema revision or new execution path is needed.
