# Real-task card pools

A pool is a directory of real tabular tasks in the sealed-set layout served by
`study/private_seal.py`. Data, caches, checkpoints and generated pools are never
committed. Tools live in `study/exp/cards/`; they require the research interpreter,
numpy, torch and scikit-learn, with one CPU thread. No model/provider calls occur.

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
0.05 times the seed-11 baseline test RMSE. Qualification requires the reference to
clear this bar at seeds 11, 23 and 37, baseline null checks, exact seed-11 baseline
reproduction, disjoint splits, a leakage screen, retraining within 120 seconds,
checkpoints at most 64 MiB and verified file hashes. Failed cards remain available
for diagnosis but are excluded from assembled pools.

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

## Rebuild outside the repository

```sh
source "/Volumes/Mrigesh SSD/eidos-v1/improvement/env.sh"
C="/Volumes/Mrigesh SSD/eidos-v1/improvement/cache"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.select --seed 20261114 --count 60 --cache "$C" --out "$C/task_catalog.json"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.intake pmlb KEY --cache "$C"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.package --source-dir "$C/raw/pmlb/KEY" --card-id R001 --task-type classification --pool-root "$C/staging"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.reference --card-dir "$C/staging/seal/R001" --controller-dir "$C/staging/controller/R001" --budget-seconds 480
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.qualify --card-dir "$C/staging/seal/R001" --controller-dir "$C/staging/controller/R001"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.pools assign --catalog "$C/task_catalog.json" --seed 20261114 --private-seed-file "$C/private/assignment.seed" --out "$C/public_assignment.json"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.pools build --pool-id D --cards "$C/staging/seal/R001" --pool-root "$C/pools/D"
"$EIDOS_RESEARCH_PYTHON" -m study.exp.cards.pools verify --pool-root "$C/pools/D"
```

Substitute each assigned card and its task type; pass every assigned qualified card
to `build`. Do not publish E/H source material, controller directories or assignment
seeds. Source licence metadata is retained for review before redistribution.
