# Practice-family exposure ledger

The manifest was fixed before pipeline C existed and before scoring any generated holdout. This harness is development evidence. It cannot establish held-out improvement or an investigator effect. Under the two-arm pilot both arms receive the same components.

## Prior design exposure

- N01 and N04 training/validation data were inspected by the discovery-report author. The accepted design and its review disclose those probes. This implementation uses the public N01–N04 and D02–D04 generators for explicitly labelled calibration, including their public test partitions in the trusted offline scorer.
- The proposed library and priors copy comparison-arm recipes on N04: arm 1 round 2 correlation screen + MLP ensemble; arm 2 round 1 per-class GMM with BIC; arm 1 round 1 PCA-whiten + SVM; arm 2 round 3 screen + MLP ensemble. D2's per-image centring on D03 is also design exposure.
- No sealed or reserve card contents and no private grading contents were used, opened or hashed. The r4 metadata-search qualification below supersedes the earlier claim about listing. No model/provider calls were made.

## r2 roles and construction exposure

The original 15-generator family and all its comparator results were exposed during the first build/review. The r2 manifest expands to 24 generators / 96 cards before pipeline C exists. gen-011's N04 mechanism is removed and replaced by large-n Hastie with structured nuisance; gen-014 is the only N04 replica and remains calibration-only. gen-010's formerly saturated accuracy mechanism is changed to meaningful group variation and a .6 threshold. All row-token streams change to eliminate source-index inversion, so r2 recomputes every score table rather than asserting the old tables are unchanged.

Design: gen-001/003/004/006/009/011. Machinery holdouts: gen-002/005/008 and gen-016 through gen-024. Separate operator holdout classes: gen-007 and gen-010. Noise/weak controls: gen-012/013. Calibration replicas: gen-014/015. New machinery holdouts were authored after seeing the review's operating-characteristic failure, to increase generator-level power; they are not independently authored prospective holdouts. Their test scores may not tune pipeline C or thresholds. The two operator holdout classes cannot borrow machinery generators to support a class claim.

[MANIFEST.json](MANIFEST.json) freezes selection constants, seed streams, the .5-SE harm margin, .8-SE generic power target, and calibration/wall tolerances. Thresholds are not chosen from r2 holdout wins. Monte Carlo uses measured exposed variance, explicitly reports homogeneous and heterogeneous profiles, and includes dependence sensitivity for fixed-source wine/digits. Attainable-reference scores and all headroom comparisons are descriptive offline evidence.

## r2 burn reset

The old `<out>/burns.json` ledgers recorded two harness-construction source changes, not two candidate evaluations. They remain intact as historical evidence. The authorized rework resets these construction-only burns in a migration receipt under `claude-orch/v2/step1-r2/primary-registration.json`. The fixed protected programme ledger is `claude-orch/v2/programme-burns.json`, mode 0600 and locked during registration. Descriptive A0–A3 construction/calibration does not consume candidate burns. A registered candidate evaluation binds its ID, candidate digest, primary verdict and generators, regardless of the output directory or unrelated source edits. Changes to an already registered candidate/verdict are refused; the two-burn/eight-primary limits remain.

## Evidence limits

A3 is an EDA proxy, not a proven coding-agent ceiling. References are empirically attainable, not Bayes scores. Finite-source resplits share rows (wine 178, digits 1,797), so effective instance n=1 and wider uncertainty are used. The first-fold poison audit observes the actual fitted workflow, including pre-CV operations, but does not prove the absence of every malicious audit-evading/later-fold-only leak. Null controls use D12's no-positive-claim semantics. Comparator runs lacking fresh claim checks fail closed; the guard cannot infer that missing evidence passed.

Only `bundles/` is candidate-visible. Sequential card numbers and the trusted index still map to generator identity; do not ship the index, controller, references or generation code. IDs are random assignments from a distinct deterministic stream; reproducibility of public synthetic data is not cryptographic concealment.

Exact runs, failures, skips, operating characteristics and all acceptance verdicts are in the report's r2 section and `claude-orch/v2/step1-r2/SUMMARY.md`. Pipeline C is not evaluated.

R2 execution scope note: the initial instruction-file discovery was accidentally rooted at the programme parent. It returned dependency AGENTS.md paths and no grading/sealed file contents, but the restricted subtree was not explicitly excluded, so absence of directory-metadata traversal cannot be certified. This scope error is recorded in the r2 report; subsequent searches were confined to the current worktree and named public inputs. No sealed data enters any generator or score.


## r3 re-review construction exposure

The r3 revision is still exposed development work. The r2 review and measured variance informed a pooled design/operator-class loss screen, calibrated on r2 variances before the final r3 operating table. A conventional one-sided .05 loss test still accepted about 20% of mixed −1 losses in an exploratory 4,000-trial probe; the additional one-sided .15 loss-risk screen reduced that to about 6% while retaining about 89% helpful power. Its 70% two-sided upper interval is labelled as such, never as 95% evidence. The final r3 table assesses the frozen rule on newly measured variance and reports every unmet target. This is not prospective validation.

The nine new machinery mechanisms now use nine nuisance structures; none spans over a third of machinery holdouts. All 96 family tables are rescored cold, and 24 cards are independently rescored in separate processes/caches. References for the 36 changed-nuisance cards are recomputed; the 60 original-mechanism references and control predictions may be reused only after comparing every feature/label array with the original source. Row tokens change under a controller-secret HMAC salt, so public stream regeneration cannot invert wine or digits labels. The secret is outside every bundle and checkout; repeat builds require the same trusted secret.

All r3 scoring, including construction, is registered in the absolute `/Volumes/Mrigesh SSD/eidos-v1/claude-orch/practice-ledger/` before work starts. Full authenticated hash-chain and pinned-head verification runs before scoring and verdict emission. Verdicts are returned only after durable append. No candidate burns from any old ledger are reset. Construction receipts are explicit and do not consume future candidate burns. Mode 0600, HMAC and the head checkpoint detect corruption/tampering within the controller trust boundary; they do not prevent the controller owner from replacing all trusted state. r2 ledger and migration receipts remain historical.

The r3 instruction-file discovery again used an overly broad programme-parent search before reading the original scope rules. It returned dependency AGENTS.md paths and no sealed file contents were opened, but absence of restricted directory-metadata traversal cannot be certified. This execution-scope error is retained in the report. Subsequent reads/searches are confined to the worktree and named brief/design/public evidence paths. No model/provider calls, Keychain operations or sibling-worktree edits are part of r3.


R3 source-uncertainty calibration: a later r2-only correlated-source probe exposed mixed −1 acceptance of 15.3% with the generic 1.04 SD floor; this failed the requested target. The final rule keeps risk alpha .15 and sets finite-source SD floors from prior observed r2 SDs, rounded up to wine 1.04 / rotated digits 4.1 / offset digits 32. An additional r2 calibration probe measured mixed −1 acceptance 5.55% independently / 6.30% correlated, helper power 89.48% / 88.40%. No final r3 Monte Carlo output was used to select this correction. The full final table and all preceding probe receipts remain in r3 evidence. The owned waiting finalizer was restarted to load each corrected policy; no scored table was altered or rebound.


## r4 authorization and exposed calibration

The orchestrator prospectively fixes +0.9 paired-test-SE for one-primary power;
+0.8 remains sensitivity. The .25 pooled loss-risk alpha is calibrated against
the exposed r3 SD profile and its x0.5/x1.5 variants, then checked on r4 variance.
This is calibration, not independent validation. The single-generator -3-SE
credible-loss check is also assessed on neutral and every -4-SE single-loss cell.
No failed target may be omitted. gen-011 is reduced to 6,000/1,000/1,000 rows;
all 96 instance walls are measured again under the unchanged 840-second limit.

The construction escape hatch is closed: only trusted design/control/calibration
registrations can be construction and they emit no verdict. Two real candidate
receipts bind the full family and independent-cold roots, spend the two holdout
burns, and share each identity across their diagnostics. They evaluate exposed
harness/comparator controls only; pipeline C does not exist here. No burn is
reset, erased or exempted. Step 2 needs independent replacements and review
before further candidate evaluation. HEADS.log witnesses each appended head;
the orchestrator separately records heads. Same-account replacement of both
local histories cannot be detected solely from those replaced histories.

The first 6,000-row r4 design attempt measured 959.53/959.18 seconds and failed.
Its scorer was stopped before scoring any holdout. The final gen-011 reduction
is 1,500/500/1,000. Authenticated design-only index revisions preserve every
protected holdout entry and both original candidate receipts/burns. Every final
family score/wall and independent-cold table is recomputed after this revision;
the failed attempt is archived. No historical wall is used as a final pass.


## r5 exposure and protected epoch transition

The practice-family design **was informed by exposed N01/N04 practice data**,
including the discovery probes and recipe comparisons described above. It is
exposed development design, not independent holdout construction.

In round 4 an executor's initial instruction-file search was rooted at the
programme parent directory. No sealed file contents were opened, but listing of
file names under the private grader directory **cannot be excluded**. This is a
metadata-exposure qualification, not a claim of no traversal. R5 searches are
confined to the worktree and named claude-orch paths.

The r5 orchestrator authorizes new seeds/data for every protected holdout in
epoch 2. Epoch 1 is retained and retired, with every burn preserved; no reset or
refund occurs. The new epoch binds generator descriptors and full/cold index
digests in the authenticated ledger and grants two candidate burns per protected
generator. Frozen comparator/reference validation has a separate recorded
harness-validation budget of two per generator; it cannot issue a candidate
verdict. Measurements expose epoch 2 for harness calibration, not candidate
threshold tuning or a prospective investigator claim. Wine's new nuisance draws
and splits still share a finite source. The single-generator −4-SE target is
now a documented limitation (reviewer acceptance up to 84%); diagnostic cells
remain visible. Large-n resource coverage remains deferred because the measured
6,000-row full sweep exceeded 840 seconds.
