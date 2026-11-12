# ADR 0043 — A thin generated practice harness and a generator-level acceptance guard that can say no

Status: Accepted for implementation (step 1)

Accepted design revision 2 and orchestrator decisions, 2026-11-13. Step 1 implements the offline harness; pipeline C and lab preparation remain steps 2–3.

## Context

The development cards (N01-N04, D02-D04) are public, few and already exposed to every design decision. Two of the three persistent arm-4 gaps
were diagnosed on their own train and validation data (discovery-gap report 3.1, 3.3); D03 is closed by a detector whose fixture is named after D03's
mechanism. N04 is `make_classification(n_informative=8, n_redundant=4, n_clusters_per_class=3)` and N03 is `make_friedman1` (`study/packaging/p3_data.py`).
The v2 library's operators descend from comparison-arm winners on N04. The sealed pilot cards, the reserve set and `eidos-grading/` are out of bounds.

Revision 1 of this ADR proposed 19 generators in eight classes, four of which were the dev cards' own mechanisms; its mixture generators were all Gaussian
(so a GMM win on a "holdout" held by construction); its null control tested the held-out score of a pure-noise recipe (chance whatever selection did); its
acceptance bootstrapped across six instances of one generator; and nothing showed the rule could say no. The independent review (findings 1-4) is right on
each point. Tier 1's `tier1-no-regression/1` has tolerance 0 on one seed per card; with 114 validation rows one example is 0.0088 accuracy against a
binomial standard error of 0.016, so it cannot arbitrate either.

## Decision

Build a **thin** harness first, in parallel with the library, and grow it only when a gate needs a class-level claim.

Generators live in `study/practice/generators.py` (trusted offline code, never copied into a bundle). Instances are written outside the repository in the
existing bundle format (`train.npz`, `validation.npz`, `task.json`, supplied baseline `project.py`); an oracle sidecar (mechanism ground truth, a
large-sample reference score) is never in a bundle; ids are neutral. Fifteen generators, four instances each (about 60 cards):

- Eleven **unlike the dev cards**: two-moons with rotated nuisance columns; concentric circles and Gaussian quantiles (with an easy variant on which strong
  defaults are near the reference); `make_hastie_10_2`; `make_friedman2` and `make_friedman3` (regression); `make_regression(effective_rank, tail_strength)`
  and sparse-uncorrelated (regression); heavy-tailed (Student-t) class conditionals with curved means, on which a Gaussian mixture is wrong; periodic
  features with heteroskedastic noise; the bundled wine data with nuisance columns at small n; digits under a rotation shift (not D03's additive offset);
  extreme imbalance with high-cardinality one-hot columns and group ids (exercising the `groups` port); and a large-n generator (n_train 20k) on which the
  cost, checkpoint-size and 120 s guards bind.
- Two **controls**: planted pure-noise labels (true gain 0) and planted weak signal (0.5 x the practical bar).
- Two **re-enactments** (an N04-like and a D03-like generator), labelled calibration-only and never cited as evidence of generalisation.

`MANIFEST.json` fixes before any v2 pipeline exists which generators are design and which are holdout, and names two **holdout classes** (periodic/heteroskedastic;
imbalanced/high-cardinality/grouped) that no design step may touch. Constants (support thresholds, the paired-band multiple, the complexity order) are frozen on
design generators before any holdout generator is scored.

The gate (`study/practice/run_gate.py`) makes no model calls. Per instance it computes the fold-by-recipe score table and the test score of every recipe once
and derives argmax, multiples {0.5, 1, 1.5, 2}, with/without priors and complexity-permutation ablations by replay. Pipelines: A0 baseline; A1 v1 sweep winner;
**A2 strong defaults** (CV-selected LR/SVC/ET/HGB, no structure search); **A3 EDA-aware proxy** (the coding arms' families: screen + MLP ensemble, per-class GMM by BIC,
PCA-whiten + SVM, CV-selected); C the v2 core. It also emulates the grader's hard gate: every fresh seed retrains the exported candidate under
`candidate_command(timeout=120)` with OMP/MKL/OPENBLAS/VECLIB set to 1 and must reach the practical bar, and fresh-process predictions must equal the locked ones.

Metrics are per-generator paired effects on the test metric in standard-error units and raw units (not a normalised gain whose denominator can be zero; any
normalisation floors it at twice the test SE of the reference); selection regret; optimism; export eligibility; and **claim-level null metrics** that mirror
`study/grade_block.py success_rule`: on control and weak-signal instances, `visible_claim_rate` (the playbook rule would permit an improvement claim because the
paired validation interval against the baseline excludes 0) and `false_positive_claim_rate` (a visible claim that the grader-equivalent rule, with fresh seeds and fresh-process
equality, would not verify). Pipeline C is compared with A2 on exactly these.

**Generators are the unit of inference.** Each generator's effect is reported with an instance-level bootstrap used only for that generator's precision. Across generators the
verdict uses a sign test and a random-effects (DerSimonian-Laird) interval. A class-level claim needs at least three holdout generators; below that the verdict is worded "no
regression on these named generators" (the lower 90% bound of each named generator's effect is above -epsilon). Each change declares one primary verdict before running; Holm
adjustment applies across the primary verdicts of the programme (at most eight); everything else is descriptive. At most two holdout burns per class are allowed; replacements
come from a different seed stream and are reviewed by someone who has not seen the failure.

**The guard has its own negative controls** (`guard_selftest`): a planted card-specific patch (an operator or constant tuned on one design generator) must return
`design-only`; a planted harmful operator must be rejected under the no-harm rule; a planted leak (screening on all of train before CV) must be caught by the in-fold test.
The guard is not used until it says no to all three. The harness also replays the dev cards through A0-A3 as calibration: A1-versus-A3 should reproduce the known gaps
(N04 about 0.07-0.09 gain; N01 about 3 test examples), and some generators are chosen so that A1 is already near the reference, giving v2 something to regress.

A change is accepted when, for its declared class: the motivating-generator effects clear the verdict rule above; no other named generator regresses beyond epsilon; pipeline C's
false-positive-claim rate is no worse than A2's by more than a pre-registered tolerance; wall and export limits hold. A neutral-elsewhere operator is acceptable; the guard asks for
no harm elsewhere and gain on a generator unlike the one that motivated it, not for gain everywhere.

Hygiene: a lint fails if any file under `components/`, `src/eidos/investigator/` or `study/prompts/` names a **generator id, a card id or an oracle field name** (operator and hypothesis
names such as `gmm_class` are legitimate and are not on the blocklist); the lint is tested with a planted violation. The model-facing identifier audit list gains the practice ids.
Practice cards are never added to `PRIVATE_*` registries. The exposure ledger (`docs/experiments/practice-family/EXPOSURE_LEDGER.md`) records that N01 and N04 train/validation data were
inspected, that the library and priors copy comparison-arm winners on N04 (arm 1 r2 correlation screen + MLP ensemble; arm 2 r1 per-class GMM; arm 1 r1 PCA-whiten + SVM; arm 2 r3 screen
+ MLP ensemble) and D2's per-image centring on D03, every holdout burn, and that no sealed or reserve card and nothing under `eidos-grading/` was used.

## Rejected alternatives

- The 114-card, 19-generator family as a first build: expensive, and its diversity was by construction (it returns if a class-level claim is needed).
- Leave-one-card-out, and instance-level bootstrap as the unit: instances of one generator share a mechanism.
- Tuning on N01-N04 and D02-D04, or any sealed or reserve card.
- Held-out test score of the chosen recipe on pure-noise cards as the null metric: chance by construction.
- Decisions by live Tier 1 alone.

## Verification and limits

Tests: generator determinism (same seed, same bytes); no test labels or oracle fields in any bundle; split sizes and neutral ids; the lint with a planted violation; claim-level metric
arithmetic against `success_rule`; the sign-test and random-effects arithmetic on synthetic deltas; the denominator floor; `guard_selftest`. Acceptance for the harness itself: calibration
within tolerance on the dev cards; A1 not uniformly below A3; digest-stable reruns; all three self-test controls reject; per-instance wall within 2x of the estimate (about 7 minutes of one core).

Limits: synthetic, small-scale data; with 15 generators the class-level power is low (hence the wording rule); the design remains informed by N04 and N01 and by comparison-arm recipes
(recorded); wins here are development evidence, not held-out evidence and not evidence about the investigator, because the gate removes the model. Confirmation needs a later, separately
sealed prospective pilot that binds the build before any model call; under the two-arm pilot, arm 2 shares the components (orchestrator decision Q1), so the harness says nothing about the
arm-4-minus-arm-2 contrast.
