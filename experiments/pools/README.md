# Real-task card pools

A pool contains real tabular tasks in the sealed-set layout served by
`study/private_seal.py`. Data, generated pools, caches and checkpoints are never
committed. The tools in `study/exp/cards/` use numpy, torch and scikit-learn, one
CPU thread, and no model/provider calls. Default commands are lab-independent.

```
POOL/pool.json
POOL/seal/commitment-opening.json
POOL/seal/R1234/manifest.json
POOL/seal/R1234/{test_inputs,test_labels,baseline_test}.npz
POOL/seal/R1234/bundle/{brief.md,project.py,train.npz,validation.npz,
                        requirements.lock,task.json,answer_schema.py}
POOL/controller/R1234/{provenance.json,reference.json,qualification.json,
                       reference_project.py,reference_spec.json}
```

The commitment remains SHA-256 of a fresh 32-byte salt followed by canonical JSON
(`sort_keys=True`, separators `(',', ':')`) of all relative sealed-file digests,
excluding the opening itself. Verification uses PrivateSeal's algorithm and
rejects additional files. Card manifests and task bundles both record `nclasses`.
This change does not alter PrivateSeal's scorer; its separate multiclass update
is required before scoring multiclass cards. `build --max-classes 2` can restrict
construction to binary classification; the default is 10.

The bar is an absolute accuracy gain of 0.02, or an absolute RMSE reduction of
0.05 times the seed-11 baseline test RMSE. Cheap qualification runs before reference
search: file hashes, exact baseline reproduction, planted-null baseline seeds
23/37, leakage, split disjointness, runtime and checkpoint size. Exact (features,
target) duplicates are removed before splitting. Regression repeats deduplication
on the float32 standardized target, refitting TRAIN statistics and splits after
each removal until packaging and the cross-split check agree. Identical features with different
targets are allowed across splits and counted as `conflicting_feature_rows`.
IDs and identical (features, target) rows must remain disjoint.

The reference searches TRAIN only, using repeated five-fold CV with preprocessing
fitted inside each fold. Families are linear, k-NN, RBF SVM below 3,001 train rows,
100-tree ExtraTrees/RandomForest with minimum leaf size two and maximum depth 20,
HistGradientBoosting and three-seed two-layer MLPs. The first fitted CV fold measures
a compressed pickle; its conservative full-train size estimate must fit 48 MiB.
At least three different families must complete; otherwise search receives one
50% budget extension and then fails explicitly. Bagged Caruana selection samples
the best 20% of configurations, includes the best single in every bag, and uses
an ensemble only if OOF performance improves. Export checks the actual 64 MiB and
120-second limits, dropping members or shrinking a lone forest as necessary and
recording every change. Fresh subprocesses retrain and evaluate all three seeds.
A reference missing the bar at any seed makes an otherwise valid card a qualified
null card. Null quotas are one per eight and two in H; surplus stays in reserve.

D is open development. E exposes scores and transcripts in rotations of eight,
with at most four decisions per rotation. H exposes aggregates, with at most three
consultations. These consultation limits are programme policy, not counters in the
construction tools. Source names occur only in D's public controller files.
SMOKE is an open diagnostic pool with the same visibility as D.

## Privacy boundary and its limits

Every build requires a separate `--private-root`. That directory holds raw source
downloads, all staging, job requests/responses/logs, per-candidate outcomes, private
seed, full assignment, reserve, E seals/controllers and H's entire pool. H has no
public manifest. Only D (or diagnostic SMOKE) is assembled under `CACHE/pools/`.
E public files are exactly `CACHE/manifests/E*.json` with `{pool, commitment,
cards: [{id, task_type, metric, bar_rule}]}`. They contain no source hints, row or
feature sizes, numerical bars, null flags or timings. D may publish full metadata.

Public state, log and stdout report aggregate counts only: processed, accepted,
qualified, rejected by reason, pool sizes, shortfall and `h_candidate_margin`.
The margin is `accepted - |D|`, a count; it does not change assembly or refuse small builds. Publication occurs at
completion; incremental candidate-level changes would reveal the ordered walk.
Full builds exhaust the catalog rather than stopping at a revealing prefix.
Resumable detailed state and elapsed time stay private. Public caches containing
legacy `raw/` or staging are refused: start a fresh public directory and transfer
needed source downloads into the private root. Do not reuse exposed older pools.

IDs are a private random permutation of `R0000` through `R9999`, shared by D/E/H
and reserve. Acceptance order and gaps carry no source ordering. Each source gets
a split seed from HMAC-SHA256(private salt, source:key); row IDs are unique random
63-bit integers. Only private state holds the salt. D controller provenance may
include its own salt-derived split seed. Changing a public seed cannot reconstruct
an E/H partition or its model-facing IDs.

Assignment uses the brief's H-first fallback. H is sampled uniformly among feasible
whole-family subsets of qualified candidates, subject to null and synthetic quotas,
without task/size balance optimization. D/E then use the public seed on the remainder, ordered by private anonymous card
slots rather than source-name ranks. The unpublished source-to-slot permutation
prevents replay of the public RNG against catalog names.
Shortfall capacity follows D, E1, H, E2, E3 priority. Consequently D/E membership
can change with the private seed. A reader can recompute stage two from public
source tables: the H candidate margin is therefore `accepted - |D|`, not catalog
size minus D. Family siblings of D members are also excluded from H, since whole
families stay together; the published margin does not subtract those siblings.
Qualification outcomes and E identities remain unpublished. Check the actual
margin before relying on a large anonymity set; small builds cannot promise one.

Literal byte equality of *all* public artifacts across different private seeds is
impossible alongside private-seed IDs, commitments to different sealed bytes, and
H-first membership: E manifests must change as well as D. The enforced boundary is
counts-only progress and opaque, minimal E manifests, with no per-source outcome
or private H/reserve ID. Tests compare counts across two fabricated builds and try
to eliminate sources using the entire public artifact set plus the catalog. Public stage-two recomputation
can narrow the candidate set to accepted minus D (and exclude D family siblings).
The probe-H regression test checks that replay against public E identifiers
recovers no more E identities than chance. This does not claim that
public task types, prior dataset knowledge or observations across many cooperating
builds convey zero statistical information.

The threat model is a cooperating session on one machine. A tool hook guards the
private root. Mode 0700 separates different users and adds nothing against another
session of the same user. This boundary is not cryptographic access control
against a hostile local user. Identifying a small
public table from its feature values can still expose its test complement. Secret
seeds and random IDs do not solve that residual dataset-identification risk.

Build temporary files are staging too: the driver creates `<private>/tmp` with
mode 0700 and wipes it at start and on resume, after checking for live previous
workers. Each worker has its own random subdirectory there. Workers override the
caller’s TMPDIR, all explicit temporary-directory calls use that private location,
and CV and candidate train/infer subprocesses receive it. Normal completion and
`_stop` remove the worker’s directory; hard driver kills leave staging private
until a safe resume cleans it. Standalone packaging/reference APIs use scratch
beneath their output roots (or private local scratch for an array-only search).

The process list still shows build options, worker phase and anonymous temporary
paths in CV/candidate commands. Worker requests and response paths travel on stdin,
so worker arguments no longer expose `jobs/<catalog index>/<action>.request.json`.
A same-user process can still inspect environments and open files; the cooperating
session boundary remains necessary. D copies use no preserved source mtimes, every
D pool file and its manifest receive a fixed epoch mtime, and D provenance retains
retrieval dates only to the day. This removes the stored per-card build schedule;
filesystem creation/change times and live write observation still reveal publication
activity, and this is not protection against active same-user filesystem monitoring.

Workers own separate process groups. Private state records their PIDs; resume
refuses while a recorded group remains alive, before wiping scratch or staging.
After a hard driver kill, allow orphan workers to finish before resuming.

## Two-stage selection and rebuilding

Stage one orders eligible PMLB tasks by public seed, interleaving types and row
bands. Optional `--count` truncates; otherwise all eligible candidates are listed.
Name exclusions and family/formula caps remain. The curated CTR23 suite supplies
OpenML regression candidates after PMLB to restore parity. Listing is serial,
one-second paced, capped at 120 new requests, and cached. Downloads are limited to
OpenML data hosts or the EpistasisLab/pmlb repository on GitHub/raw/media hosts,
including every redirect. Decompressed data is bounded at 200 MB and 150,000 rows.
Nominal ARFF features use the declared vocabulary; missing values are rejected.

Stage two verifies real sizes, minority fractions (5%, or 2% for at most four
accepted exceptions), three nonconstant train features and at least 600 train rows.
Feature-only signatures use sorted column digest multisets with canonical numeric
formatting and ignore targets, column order and row order. An overlap of at least
80% of the smaller multiset identifies a feature family: first accepted in catalog
order wins. Target-inclusive content duplicates are also checked. Likely synthetic
sources (`feynman`, `strogatz`, BNG, 2dplanes, ring, twonorm, waveform, mv, fried,
GAMETES, parity, monk, led, Hill_Valley, clean and banana) are flagged; phoneme is
real. Synthetic quotas are two per eight and three in H. All rejection details and
licence evidence stay private.

```sh
source "/Volumes/Mrigesh SSD/eidos-v1/improvement/env.sh"
# Source/listing cache is separate from a fresh public build output directory.
C="/Volumes/Mrigesh SSD/eidos-v1/improvement/cache"
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.select --seed 20261114 --cache "$C" --out "$C/task_catalog.json"
PUBLIC_ROOT=$(mktemp -d)
PRIVATE_ROOT=$(mktemp -d)
cp "$C/task_catalog.json" "$PUBLIC_ROOT/task_catalog.json"
# Copy any cached per-candidate raw downloads into "$PRIVATE_ROOT/raw/" first.
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.build --catalog "$PUBLIC_ROOT/task_catalog.json" --cache "$PUBLIC_ROOT" --want SMOKE=2 --budget-seconds 120 --workers 2 --private-root "$PRIVATE_ROOT" --allow-root
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.pools verify --pool-root "$PUBLIC_ROOT/pools/SMOKE"
```

A full build uses a durable protected private root and a fresh public cache with
`--want D=8,E1=8,E2=8,E3=8,H=12`; `--resume` requires the original catalog, options,
state and private seed. Search defaults to 480 seconds. Each worker sets niceness
and thread limits. A 25-minute per-candidate deadline stops its entire process
group; `--candidate-timeout` adjusts it. Finished resume verifies commitments.

## Build of 14 November 2026 (pools v1)

Built from `main` 340d206 with `--want D=8,E1=8,E2=8,E3=8,H=12 --workers 3` (search budget 480 s, candidate
timeout 1500 s) over the 77-candidate catalog (stage-one seed 20261114). Public counts from the build state:
processed 77, accepted at stage two 52, qualified 42, `h_candidate_margin` 44. Pools: D 8, E1 8,
E2 8, **E3 2 (shortfall 6)**, H 12 (private; no manifest). Rejections by reason: minority-class/imbalance cap 9,
qualification checks failed 10, same feature table 7, missing values 2, unsupported format 2, fewer than 600 train
rows 1 (31 rejections; the remaining 4 of 77 are candidates skipped by the regression/classification balance rule
for the OpenML supplement, which the public state does not count by name). No qualification check was relaxed to fill E3. The committed manifests are `D.json` (the open pool, full
pool metadata), `E1.json`, `E2.json` and `E3.json` (pool id, commitment, and per card only id, task type, metric
and bar rule). D's commitment is `401dd86bbbd91068c3ddaaa23ccc74b3226ad190a33ebc846eab2e5ffd68f41f`. This is
development evidence about the build, nothing more.
