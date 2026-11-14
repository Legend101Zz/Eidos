# Real-task card pools

A pool is a directory of real tabular tasks in the sealed-set layout served by
`study/private_seal.py`. Data, caches, checkpoints and generated pools are never
committed. Tools live in `study/exp/cards/`; they require the research interpreter,
numpy, torch and scikit-learn, with one CPU thread. The default commands are
lab-independent: they never need a lab or call a model/provider.

```
POOL/pool.json
POOL/seal/commitment-opening.json
POOL/seal/R001/manifest.json
POOL/seal/R001/{test_inputs,test_labels,baseline_test}.npz
POOL/seal/R001/bundle/{brief.md,project.py,train.npz,validation.npz,
                       requirements.lock,task.json,answer_schema.py}
POOL/controller/R001/{provenance.json,reference.json,qualification.json,
                      reference_project.py,reference_spec.json}
```

Card identifiers and arrays are neutral. Source names, label maps, column
permutations and strong references stay in `controller/`, outside `seal/`.
The commitment is SHA-256 of a fresh 32-byte salt followed by the canonical JSON
(`sort_keys=True`, separators `(',', ':')`) of every relative file digest under
`seal/`, except the opening itself. The local verifier follows PrivateSeal's
algorithm without its Pilot 4 ID allowlist, and also rejects extra unlisted files.

The bar is an absolute accuracy gain of 0.02, or an absolute RMSE reduction of
0.05 times the seed-11 baseline test RMSE. Qualification checks baseline null
controls, exact seed-11 baseline reproduction, disjoint splits, a leakage screen,
retraining within 120 seconds, checkpoints at most 64 MiB and verified file hashes.
A reference that fails the bar at any of seeds 11, 23 and 37 marks a null card;
otherwise valid null cards are retained, with at most one per pool of eight and
two in H. Excess null cards remain in reserve. Other failures exclude a card.

The reference uses train-only repeated five-fold CV across regularised linear
models, distance-weighted k-NN, RBF SVM (up to 3,000 train rows), 300-tree ExtraTrees
and RandomForest, about 30 seeded HistGradientBoosting configurations with
within-fold early stopping, and two-layer MLPs averaged over three seeds.
Preprocessing is fitted inside each fold. A wall budget and subprocess timeouts
bound search. Bagged Caruana selection with replacement over the best 20% of OOF
configurations is adopted only when it improves on the best single model.
Standalone exported recipes are retrained and evaluated in fresh processes at all
three seeds; slow ensemble members are removed if retraining exceeds 120 seconds.

D is open development. E exposes scores and transcripts only, in rotations of
eight cards; a rotation serves at most four decisions. H exposes aggregates only,
with at most three consultations. Families stay in one pool. Public assignments
include source names only for D. H members and the unused reserve are written only
beside the private 32-byte seed; protect that directory. These usage limits are
programme policy, not enforced by these construction tools.

## Two-stage selection and rebuilding

Stage one orders all eligible PMLB candidates using a seed, alternating task types
and row-count bands. `--count` optionally limits that list; an infeasible count
reports the available counts. Exclusions include `fried`, family caps remain two,
and formula families have a combined cap of four. Class proportions absent from
the summary are deferred. OpenML's verified CTR23 suite supplies regression
candidates after PMLB, used only to move accepted regression cards toward parity.
Listing requests are serial, spaced by one second, capped at 120 and cached.
Nominal ARFF features use their declared vocabulary order; licences are retained
verbatim for review before redistribution.

Stage two checks each real table for missing values, class proportions (5%, or
2% for at most four accepted cards), three nonconstant features on train, at least
600 train rows after deduplication/splitting, and duplicate table content. Its
fingerprint is shape plus sorted column-wise SHA-256 digests, including the target;
it ignores column order and names and preserves row order. Rejections are recorded
and the next candidate is tried. Task imbalance is reported, never an error.

```sh
source "/Volumes/Mrigesh SSD/eidos-v1/improvement/env.sh"
C="/Volumes/Mrigesh SSD/eidos-v1/improvement/cache"
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.select --seed 20261114 --cache "$C" --out "$C/task_catalog.json"
PRIVATE_ROOT=$(mktemp -d)
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.build --catalog "$C/task_catalog.json" --cache "$C" --want SMOKE=2 --budget-seconds 120 --workers 2 --private-root "$PRIVATE_ROOT"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.pools verify --pool-root "$C/pools/SMOKE"
```

Use a durable protected `--private-root` for a full build with
`--want D=8,E1=8,E2=8,E3=8,H=12`, and add `--resume` to continue using the same
catalog, options and private root. Each worker sets its niceness and one-thread
limits. A candidate has a 25-minute hard wall deadline (`--candidate-timeout`);
its entire subprocess group is stopped if it exceeds that limit. IDs are allocated
in candidate order only after stage-two acceptance. Search defaults to 480 seconds.
The driver stops at the requested qualified count plus six reserve cards, or
exhaustion; SMOKE stops after one qualified card of each type. A shortage fills
D, E1, H, E2, E3 in that priority, without relaxing family/null constraints.

All provisional cards, detailed outcomes and subprocess logs stay under the private
root. H's seal, controllers, assignment and `pool.json` stay there too. Public
`build-state.json`, `build.log` and stdout contain candidate outcomes without
private IDs/source names, public D/E members (source names only for D), and a single
held-out-or-reserve count. Resume needs both public cache and private state. A
finished resume re-verifies commitments without retraining. Do not publish E/H
source material, controller directories or assignment seeds.
