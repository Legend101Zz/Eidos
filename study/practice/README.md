# Offline practice harness

Read the frozen [manifest](../../docs/experiments/practice-family/MANIFEST.json)
and [exposure ledger](../../docs/experiments/practice-family/EXPOSURE_LEDGER.md)
before scoring. These are exposed practice/development measurements. Pipeline C
is unavailable in step 1; no investigator or prospective improvement claim is
established. The design was informed by exposed N01/N04 data.

Use the pinned numpy, scikit-learn and joblib research environment, plus torch
for public development baselines. Source the orchestrator SSD environment, set
`EIDOS_RESEARCH_PYTHON`, cap OMP/MKL/OPENBLAS/VECLIB threads at 1, and use
`nice -n 10` for long work. Never use sealed cards or a grading directory.

## Protected epochs and budgets

The controller has registered epoch 2 in the authenticated fixed ledger at
`/Volumes/Mrigesh SSD/eidos-v1/claude-orch/practice-ledger/`. Its authorized data
roots are recorded in `claude-orch/v2/step1-r5/epoch2.json`. All fourteen protected
generators have new seeds and fresh data. Epoch 1 is retired: all old registrations
and burns remain intact, and new scoring/candidate verdicts on that epoch fail.
Wine still shares a finite source despite new splits and nuisance draws.

Candidate evaluations have **two burns per protected generator per epoch** and
at most eight programme primaries. Frozen comparator validation has its own
**two-registration harness-validation budget per protected generator per epoch**.
Replaying the same frozen registration spends no additional burn. No validation
or reference computation decrements candidate burns. A changed identity cannot
reuse an evaluation ID.

The controller calls `ledger.open_epoch(2, data_roots=[FULL, COLD], reason=...)`
once, after building data and before scoring. This appends retirement plus the
new generator/seed descriptors, absolute roots, index digests, per-card content
hashes and comparator source digest. It never resets the ledger. Further epochs
require another explicit controller transition with fresh data. An absent,
changed or copied index/bundle/controller, wrong role or different test-label
path fails authorization before loading held labels. A candidate must use the
actual authorized full epoch-2 root, not a clone or an old r2/r4 data directory.

`run_gate.run` defaults to `kind='harness-validation'` and refuses
`kind='candidate'` before registering: it scores A0–A3 only. Its validation digest
must equal `ledger.comparator_digest()` and the epoch-pinned source/configuration.
Validation refuses custom recipes, fold/seed settings and pre-CV transforms;
changed source invalidates that validation authorization. `references` and
`controls` use the same validation identity/root. Independent `recompute` uses
a second validation registration. Construction is limited to trusted
design/calibration/control generators and cannot emit a candidate verdict.

For step 2, register the candidate directly with
`ledger.register(..., kind='candidate', data_root=EPOCH2_ROOT)` and pass that
receipt to candidate scoring and `guard.evaluate`. Candidate receipts support
custom recipes. Candidate inference requires the exact registered generator
set, trusted roles and a durable ledger verdict. Harness diagnostics require
`harness_validation=True` and append separate `harness-validation-result` events
with `candidate_verdict=false`; they cannot emit candidate verdicts.

## Commands and controller boundary

For a newly authorized epoch (the real epoch-2 transition has already occurred):

```sh
python -m study.practice.build_cards --out /external/practice/data
# Trusted controller: build a separate instances=1 cold root, then open_epoch.
# FROZEN_DIGEST is the comparator_digest recorded in the epoch-open event.
python -m study.practice.run_gate --data /external/practice/data --out /external/practice/gate --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST --kind harness-validation
python -m study.practice.run_gate --data /external/practice/data --out /external/practice/gate --fresh --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST --kind harness-validation
python -m study.practice.run_gate --package-development --data /external/practice/development --out /external/practice/calibration --evaluation-id calibration-v1 --candidate-digest FROZEN_DIGEST --kind construction
python -m study.practice.references --family /external/practice/gate/gate.json --data /external/practice/data --out /external/practice/evidence --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST
python -m study.practice.controls --data /external/practice/data --references /external/practice/evidence --out /external/practice/evidence --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST
python -m study.practice.operating --family /external/practice/gate/gate.json --out /external/practice/evidence/operating.json --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST
```

Data and evidence live outside the repository. `bundles/` contains visible
train/validation data and a functioning baseline. The controller-only index,
labels, oracle sidecars and references must never enter candidate mounts. The
sandbox receives one bundle, selected source and label-free test inputs.

The ledger is explicitly provisioned once by `ledger.initialize()`; scoring
never recreates missing state. It appends sequenced, hash-linked, HMAC-authenticated
JSONL under a lock and pins the head separately. Every append also fsyncs its
sequence/head into `HEADS.log`; the orchestrator witnesses heads separately.
Previous entries are never rewritten. Same-account replacement of all histories,
keys and checkpoints remains outside the guarantee: local tail-plus-head rollback
or total reset cannot be detected solely from replaced state.

The controller's random 32-byte `practice-secrets/row-token-salt` is outside all
checkouts, evidence and bundles. HMAC row IDs are deterministic only with that
secret and cannot be recovered from public seed streams. Preserve trusted state
for reproducibility; never regenerate or copy the salt into evidence.

## Scoring and inference

A0 is the supplied baseline, A1 the unchanged broad v1 validation sweep, A2 strong
CV defaults, and A3 a fixed EDA proxy. Each table retains five-fold scores, OOF
digests and test scores for every recipe. Choices freeze before held labels open.
Classification uses accuracy; regression uses negative RMSE. `selection_replay`
derives selection-band and prior/permuted-prior diagnostics without refitting.
`--fresh` executes three seeds through the no-network evaluator, checks reload
prediction equality, 64 MB checkpoints and the 120-second candidate limit. A
cache hit is not evidence of a new cold measurement or fresh seed execution.

`compute_table` poisons held-fold features and permutes held-fold labels on the
actual fit path, then compares fitted objects once per recipe (first fold).
`controls` runs an actual supervised pre-CV leakage fault and generic prediction
fault repairs. This does not guarantee detection of every later-fold-only or
audit-evading leak. References are empirical attainable measurements, not Bayes
ceilings; synthetic references use independent large samples. The cold evidence
compares every recipe's fold/CV/validation/test scores, OOF digest and frozen
choices for at least one independently recomputed card per generator.

Generators are inference units. Transfer uses an exact weighted sign-flip test
and a modified Hartung–Knapp random-effects lower 90% bound across machinery
holdouts, plus a minimum +.5 paired-test-SE gain. Operator holdout classes cannot
borrow machinery generators to support a class claim. The per-generator harm
screen uses .5 SE and Holm .05. Separate zero-margin pooled loss screens cover
design/operator-class generators together and each role. The risk screen's
one-sided alpha .25 (upper 50% interval) was calibrated on exposed r3 variance;
it is not a 95% confidence claim. Conventional one-sided .05 loss tests remain
separate. Finite-source repeats have effective n=1 and prior source-SD floors
of 1.04 (wine), 4.1 (rotated digits) and 32 (offset digits).

The one-primary power target is ≥80% at +.9 paired-test SE at the observed
profile. +.8, higher-variance helpful power and eight-primary power are
sensitivity measurements. Neutral acceptance and mixed-harm targets also cover
observed variance multipliers .5, 1 and 1.5, with Wilson bounds. The additional
per-generator screen rejects a one-sided 97.5% upper t bound below −3 SE; its
neutral familywise false-rejection target remains ≤5%.

**Reliable single-generator −4-SE detection is not a hard criterion (d) target**
under the r5 orchestrator decision. Four instances cannot support it; the
reviewer measured acceptance up to 84%. Every single-loss cell and credible-loss
screen remains visible as a diagnostic. Named acceptance means positive transfer
and no detected material harm, not per-generator equivalence. Missing in-fold,
null-claim or resource evidence rejects. `acceptance --help` describes the five
criteria; `claim_audit.py` audits visible structured claims and evidence references.

## Coverage and fingerprint limits

Large-n resource coverage is deferred. The r4 6,000-row full-sweep attempt took
959.53/959.18 seconds against the unchanged 840-second limit. Current gen-011
uses 1,500/500/1,000 Hastie rows with structured nuisance; it does not exercise
large-n cost/checkpoint pressure. A scalable full-pool design or a separate
resource stress suite needs validation before restoring that claim. The r4
design-only index revision API remains available; it authenticates previous
index bytes and forbids any protected entry, generator-set or role change.

The N04 statistical fingerprint reads visible train/validation rows only. It
can over-flag **moons plus 18 noise columns**. It returns early for
non-classification tasks, non-binary targets, fewer than 16 columns, or fewer
than 200 visible rows. Its mixture branch also skips data with fewer than 90%
mostly continuous columns (>half of visible values unique); the earlier
Gaussian/rank branch can still flag them. Thus 12/14-column and multiclass N04
variants can escape this statistical check. Executed-factory dial checks are
separate and can catch exact N04 calls regardless of column/class counts. A
lint flag is a review prompt; passing is not proof of independence.
