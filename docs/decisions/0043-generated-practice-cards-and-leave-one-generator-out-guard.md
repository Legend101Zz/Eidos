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

## Decision (r2 exact-source review fixes)

The first implementation was rejected in `V2_STEP1_REVIEW.md`. Its large-n generator reproduced N04's mechanism, its noisy guard had no useful power, and its leak control was a hand-built trace. This revision replaces those claims and their evidence.

The family now contains **24 generators × 4 instances = 96 cards**. Six design generators cover rotated moons, Hastie radial classification, Friedman 2/3, curved Student-t classes, rotated digits and large-n Hastie (20k train rows, 30 columns). The large-n generator has correlated, near-duplicate and nonlinear nuisance columns; it never calls `make_classification`. Twelve machinery holdouts comprise circles/quantiles, low-rank/sparse regression, wine/nuisance, and nine additional mechanisms: checkerboard, spiral phase, ordinal thresholds, sawtooth additive, rational response, max-affine, multiplicative response, absolute additive and threshold interactions. The original periodic/heteroskedastic and grouped/high-cardinality generators remain **separate operator holdout classes**. Grouped classification has genuine group-level latent variation and a .6 threshold, avoiding the former ≥96% majority saturation. Two controls have noise/weak labels. The N04 and additive-offset replicas exist **only in calibration**.

The executed-factory lint in `independence.py` checks the seven public development recipes. It compares N04's mechanism parameters regardless of padding/sample size/seed, detects reused real datasets and unchanged digit shifts, and checks D04's conditioned linear fingerprint. A planted disguised factory must fail it. This is a regression safeguard for known recipes, not proof of universal mechanism independence.

Only bundles are candidate-visible. Controller labels, oracle sidecars, references and the index remain separate. Row IDs are random 63-bit tokens from an independent stream, never hashes of source indices. Determinism, uniqueness, inverse-index rejection and label-association tests cover every generator. Public reproducibility is not cryptographic secrecy: code plus a published seed can reconstruct synthetic data. A trusted index still maps sequential card numbers to generators; do not provide it with the bundles.

A0 is the supplied baseline; A1 the unchanged broad v1 validation sweep; A2 strong CV defaults; A3 a fixed EDA proxy, **not a demonstrated coding-agent ceiling**. Pipeline C remains step 2. Choices freeze before test labels open. Tables retain fold scores, OOF digests, measured baseline/fit wall times and compute-time versions. CV-selected recipes report CV minus test as optimism; validation-selected recipes report validation minus test.

### Inference and operating characteristics

Generators are the inference units. The primary named-transfer rule requires an exact inverse-variance-weighted conditional sign-flip test across the **machinery holdouts**, with Holm adjustment over registered primary verdicts, and a positive lower 90% **modified Hartung–Knapp** random-effects interval (DL heterogeneity, t(k−1), variance floor max(.01, .1 × median within-generator variance)). The count-based sign p remains descriptive; the primary sign-flip weights depend only on sign-invariant within-generator variance and enumerate all 2^k assignments (2–16 generators). Under independent symmetric neutral effects its conditional size is ≤.05; one arbitrary generator can at most double this to .10. This uses unequal precision more efficiently than a count-only sign test. Within-generator summaries use t intervals rather than the former four-observation percentile bootstrap. Wine/digits resplits share source rows: use effective instance n=1 and a variance floor of 1.04² (the largest observed moderate instance SD, preventing identical correlated repeats from acquiring spurious precision), retain their finite-source size (178/1,797), and widen uncertainty instead of asserting four independent datasets.

No-harm means no statistically detected material regression: one-sided per-generator harm tests at margin **epsilon=.5 test-SE**, familywise Holm .05. This margin is about half the observed moderate SD (.66–1.04); it does not certify per-generator equivalence at n=4. A pooled point gain of at least **+.5 SE** (the same practical margin), positive pooled transfer and this harm screen permit `accept-named`; wording no longer claims proven no regression on every generator. `accept-class` uses only explicitly declared operator holdout-class generators and their own inference; machinery generators cannot supply a class claim. Two existing operator classes cannot support one. At least five positive independent generators are needed even to attain sign-test p≤.05; the default named family has twelve.

The declared helpful Monte Carlo target is **+.8 test-SE**: below r2 moons +1.064 in paired-test-SE units (the old marginal-SE estimate was +.896); it does not borrow the much larger N04 calibration gap. `operating.py` evaluates neutral, harmful −.5, generic helpful +.8, and a patch helping only one holdout (+2), across SD .1/.3/.66/.8/1.04 and the observed heterogeneous instance SDs. It also stresses fully correlated fixed-source instances, records Monte Carlo intervals, and discloses the power cost of eight simultaneous primaries. Its vector arithmetic is tested against `evaluate`. This power target is for one preregistered primary; eight-primary sensitivity is reported separately. Results and any unmet targets must be read from `step1-r2/operating.json`, not inferred from this ADR.

All checks fail closed: missing in-fold, null-claim, tolerance or resource evidence rejects. Null and weak-signal controls use the grader's **D12 branch**: every positive claim is false even if its numeric fresh gains pass. Fresh reproduction still checks all three seeds, fresh-process prediction equality, checkpoint size and the 120 s candidate limit. Comparator-only runs without fresh claim evidence cannot receive an acceptance verdict.

### Controls and leakage

`guard_selftest` has noisy positive, neutral, harmful and single-generator effects, never constant vectors. `controls.py` additionally measures real pipeline-output fault controls on actual family data: remove label jitter / regression output noise generically; compare equal independent jitter for neutrality; reverse the repair for harm; repair only gen-002 for a card-specific patch. These deliberately planted fault controls are distinct from the modest +.8-SE simulation and are not pipeline C evidence.

`check_in_fold` reruns the **actual FoldWorkflow** with held-fold features poisoned and held labels permuted; the fitted object must remain byte-identical. `compute_table` runs one poison replay per recipe on its first fold; all folds use that same fit path. This includes any pre-CV transformation. A real `SelectKBest(f_classif)` fitted on full training data before CV inflates noise-label CV and fails this behavioural check. The old ID dict check survives only as a supplemental trace-boundary unit test. The behavioural check does not claim to detect code that explicitly branches to evade an audit or every possible later-fold-only leak.

### References, determinism and programme burns

`references.py` estimates independent attainable references for every generator (20k synthetic training / 2k selection / 5k test; 40k training for large-n). Finite sources use train+validation refits and effective instance n=1. Model choice uses reference validation, not test. These are empirical achievable scores, not Bayes ceilings; independently sampled latent regression coefficients make headroom a generator-distribution estimate. The acceptance report lists reference minus A1 for every generator, and requires multiple near-reference generators **and** measurable holdout headroom beyond the recipe pool.

Criterion (b) needs all generators referenced, at least three named generators within two SE and .02 accuracy / 5% RMSE of reference, at least two holdouts with >two-SE headroom, and A1 mean ≥A3 on at least three named generators including two holdouts. Criterion (c) needs rebuilt byte-identical data, stable semantic replay, and a separate cold score recomputation of at least one card per generator, comparing every recipe's fold/CV/validation/test scores, OOF digest and frozen choices. Cache hits alone fail it.

The protected ledger is fixed at `claude-orch/v2/programme-burns.json`, outside `--out`. File locking and mode 0600 protect writes. Registrations bind evaluation ID, candidate digest, primary verdict and declared generators; unrelated harness/source edits spend no burn. Candidate changes cannot reuse an evaluation ID. At most two evaluations per declared generator/class and eight primaries remain. The two historical construction registrations are reset with an explicit migration receipt, not silently charged to step 2. Descriptive construction/calibration has no candidate acceptance and spends no evaluation burn. Replacement following actual candidate failures still requires a different seed stream and independent review.

## Verification and limits

The report `claude-orch/logs/V2_STEP1_REPORT.md` maps F1–F14 to code, exact validation and evidence. JSON plus a short summary live under `claude-orch/v2/step1-r2/`. No model/provider call, sealed card, Keychain action or grading-directory access is authorized. This is exposed development evidence; a later prospectively sealed pilot is required for held-out or investigator claims. Both pilot arms receive the same components.

### r2 measured acceptance

All five criteria passed on 96 family cards and seven public development cards. Independent cold recomputation of 24 cards (one per generator, 1,494 recipes) matched every compared score, fold, OOF digest and frozen choice. The maximum measured family instance wall was 780.55 s, below 840 s.

At +.8 paired-test-SE with one primary, observed-variance Monte Carlo accepted the helper 89.48%, neutrality .38%, harm 0%, and a one-generator patch 3.17% (20,000 trials/cell; Wilson bounds meet the targets). With eight primaries the same +.8 effect has only 70.41% power; that limitation is retained. An unchanged-guard/family alternative targeting **+1.0 paired-test-SE**, still below the observed non-dev moons +1.064, reaches 85.79% acceptance with eight primaries and 85.08% under fully correlated finite-source repeats. Its false-acceptance bounds also meet the targets. `programme-power.json` records this separately; no +.8 eight-primary power claim is made.


### r3 re-review fixes (supersede r2 ID, harm and ledger descriptions)

M1: Loss outside machinery transfer now has independent pooled modified-HK tests over all design plus operator-class generators, and each role separately. The conventional one-sided .05 test uses a zero loss margin. An additional one-sided **.15 loss-risk screen**, upper 70% interval below zero, rejects at the same zero margin. This deliberately sensitive screen is calibrated jointly with neutral acceptance and helpful power; it is not described as 95% evidence. In exploratory r2-variance construction probes, .05 retained about 20% mixed −1 losses, .10 about 10%, and .15 about 6%, with about 89% helpful power. This calibration used exposed data and is disclosed; the full r3 Monte Carlo, not these probes, determines acceptance. Mixed cells help machinery +.8 SE and harm all design/class generators by −.5, −1 and −2 SE. Targets are ≤10% at −1 and ≤5% at −2, ≥80% helper and ≤5% neutral, judged by Wilson bounds. The −.5 cell is descriptive. Every actual nuisance-template patch is also simulated with a ≤10% target. All eight-primary results remain visible; no unsupported eight-primary power claim is made.

M2: Every scoring entry point requires registration, including construction. `run` and its CLI require evaluation ID and candidate digest. `compute_table` verifies the receipt and actual bundle generator against the scoring index; supplied baseline and fresh export also require receipts. Every `evaluate` call verifies the exact receipt/candidate/class set (including refusal to omit a registered generator), computes the decision privately, then appends it before returning. Failed ledger writes emit no verdict. Registrations bind evaluation, candidate, primary, generators and construction/candidate kind. Construction is logged without consuming candidate burns; pipeline C does not exist in step 1.

The ledger is **`/Volumes/Mrigesh SSD/eidos-v1/claude-orch/practice-ledger/`**, independent of checkout and evidence paths. Controller-only explicit provisioning creates the key, append-only JSONL, lock and pinned head. Full sequence/link/HMAC verification is required for registration, scoring and verdicts. Previous entries are never rewritten; the separately pinned head detects tail removal, while HMAC catches edits and recomputed unauthenticated hashes. Missing state fails closed. This protects integrity under the controller trust boundary, not against the owner replacing all secrets and checkpoints. No old candidate burn was erased; r2's construction-only history remains historical, and r3 construction receipts use the new chain.

M3: gen-016..024 have nine different nuisance structures; none is shared by more than one of those nine. Across all twelve machinery holdouts the maximum shared template is two (no nuisance). The original common-template assumption is removed. Tests generate each structure and cap the template share at one third; Monte Carlo evaluates every actual template group, not an arbitrary single-generator proxy.

M4: All generated practice row IDs now use HMAC-SHA256 with a random secret salt stored only at `/Volumes/Mrigesh SSD/eidos-v1/claude-orch/practice-secrets/row-token-salt`, outside checkouts and bundles. Public seeds plus source row order cannot regenerate tokens. Tests repeat the reviewer's wine/digits token lookup and recover no matching held token. Reproducibility requires the same trusted salt; never copy it into evidence or candidate bundles.

N04 lint: Factory comparisons remain, and an independent statistical check detects an exact redundant rank deficit plus Gaussian-noise column count and class-conditional moment signature. It catches a perturbed N04 copy generated before instrumentation (class_sep .83, flip_y .025, 30 columns), even with no observed sklearn factory call. This is a conservative known-recipe lint, not a universal independence theorem.

All r3 evidence and the full operating table with Wilson intervals live in `claude-orch/v2/step1-r3/`. The r3 report records actual targets and any unmet criteria; the historical r2 acceptance claim above does not establish the new mixed-harm targets.


The final r3 guard also corrects source uncertainty in the pooled loss screen. A second exposed r2 calibration probe with perfectly correlated finite-source repeats found 15.3% acceptance of mixed −1 harm when digits inherited the generic 1.04 SD floor. Increasing only the risk level to .20 still retained about 10.2%. The retained rule instead uses **source-specific prior observed SD floors**, rounded up (wine 1.04, rotated digits 4.1, offset digits 32), with effective n=1. This preserves the .15 risk level and matches uncertainty to the already observed finite-source variation. A separate r2 probe (seed 777, 4,000 trials) then accepted mixed −1 at 5.55% under independent instance noise and 6.30% under full source correlation, with helper power 89.48% / 88.40%. These are exposed calibration figures, not the final r3 Monte Carlo. `r3-frozen-policy.json` records the exact policy digest before that final table. Scalar/vector parity tests cover the source-specific floors.


### r3 measured outcome

Calibration, reference/headroom and determinism criteria pass. All 24 independent cold cards (1,494 recipes) match; all 32 selected null-control exports verify three seeds (96 aggregate seed receipts). Real helpful fault repair is accepted; neutral, harmful, card-specific and actual pre-CV supervised leakage are rejected.

The frozen **+.8-SE helpful-power target is NOT MET**: observed power 77.145% (Wilson 95% 76.558–77.722%); fully correlated finite-source power 77.265% (76.679–77.841%). Neutral acceptance is 1.07% / .935%. Mixed −1 elsewhere acceptance is 4.75% / 5.485%; mixed −2 is .045% / .060%, with Wilson bounds meeting the requested harm targets. Every actual nuisance-template cell meets its one-primary bound at this size.

A disclosed unchanged-rule/family **+.9-SE sensitivity** meets every one-primary statistical target: observed helper power 85.470% (84.975–85.952%); correlated-source 84.925% (84.422–85.414%). It was measured after the frozen +.8 miss and is not a retrospective pass. The pre-existing +1.0 alternative increases power but its stronger no-nuisance-template patch misses 10% (10.325%, Wilson upper 10.754%). All full tables, including eight-primary limitations, are retained.

The timing criterion is also **NOT MET**: maximum 1,223.33 s against 840 s. The limit is unchanged. Overall step-1 acceptance remains NOT MET; this commit does not claim an all-green acceptance or pipeline C readiness. A final authentication-only bundle-binding correction leaves every Monte Carlo count/bound unchanged under exact rerun. See `step1-r3/SUMMARY.md`, `ALTERNATIVES.md`, JSON receipts and the report's r3 section.
