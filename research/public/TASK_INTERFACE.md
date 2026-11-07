# Task interface 0.2 — public research contract

Status: runnable local development reference package; Eidos/agent integration and hostile-code isolation are unqualified. This is an external task interface, not a change to Eidos architecture.

## Package and visibility

A task has `task.json`, `brief.md`, a project source tree, environment lock, `train.npz`, `validation.npz`, and optionally inference-only `test_inputs.npz`. Each dataset has stable unique IDs, input arrays and shape/type metadata. Training and validation may contain targets. The final evaluator separately owns test labels, case definitions, qualification controls and reference solutions. Agent-visible final inputs may be delivered only during isolated inference, depending on the task's threat model. Input IDs are opaque; never encode labels in IDs.

The public development directory deliberately includes generators, known references and development-test targets. Its test split is a reproduction/qualification check, not a secret final evaluation. Do not copy a development generator with new seeds and call it an unfamiliar final project. Related cards remain in one project cluster.

Task manifest required fields: schema version, task/project/family IDs, source revisions and notices, role, input hashes, baseline model/recipe hashes, metric/direction/denominator, guardrails, allowed interventions, practical task gain, claim types, split/group policy, device/runtime requirements, every budget limit, validation feedback policy, confirmation repetition plan, output schema, grader revision and qualification state. A missing final study margin or campaign budget must block confirmatory admission. Development fixture acceptance thresholds are separate and explicitly labeled.

## Permitted work

Unless a task narrows the scope, candidates may change training code, preprocessing, loss, optimizer, weights, architecture, train-data sampling, lawful train-only augmentation and instruments. They may use a permitted simulator or bounded model-backed generator, with all calls/data lineage/cost recorded. The recipe must remain an ordinary runnable PyTorch project with a declared factory and inference path. Alternative valid approaches are eligible; references are examples only.

Do not read or derive hidden labels from grader files, query the private evaluator during search, alter supplied scoring inputs, patch the scorer, substitute test data or reach unrelated host files. External internet and caches have a frozen access policy shared by all arms. No external posting, benchmark submission or paid API use is implied. Candidate code is untrusted in a real final run: use a qualified separate identity/VM, read-only inputs, restricted network/process access and no evaluator credentials. The development runner does not implement these protections.

## Lifecycle

1. **Qualify/import:** verify hashes, input schema, splits and example behavior; execute baseline in a fresh process; record hardware/software and known exposures.
2. **Research:** start the clock and counters; allow declared train/validation feedback. Record every attempted intervention, failed job, generated datum and cost. Store resumable state only if supported.
3. **Select:** submit exactly one locked candidate (or abstain) per run, with hashes and common answer JSON. Selection after hidden feedback is prohibited.
4. **Confirm:** trusted evaluator launches candidate inference with inputs only, then scores serialized predictions in a different label-owning process. Candidate source is not imported by the scorer. Reproduce from the locked recipe on fresh initializations when the claim concerns a recipe/architecture. Score no-change and relevant positive/null controls independently.
5. **Recover:** a declared failure policy governs interruption. Keep parent checkpoints immutable; restore class, lost work, retries and inherited cost are explicit. Recovery does not reset the research budget.
6. **Close:** produce immutable submission, grade, cost and audit receipts. Invalid, missing, excluded, inconclusive and no-improvement outcomes remain distinct.

The final evaluator owns launch timing and resource accounting. Candidate self-reports supplement receipts but do not establish budget compliance. CPU core-seconds, accelerator device-seconds/type, elapsed time, memory/disk, model calls/tokens/cost and recovery are separate fields. Block final admission until actual enforcement is tested. Local development uses CPU, one torch thread and 120-second reference-call limits where subprocesses apply; these are fixture settings, not an authorized investigator budget or a memory/network sandbox.

## Prediction and model artifacts

Numerical prediction file: NumPy `.npz` with `ids` (unique integers) and `predictions` (one finite numeric scalar per expected ID); `allow_pickle=False`. Classification predictions must be integer class IDs in range. Extra/missing/duplicate IDs, nonfinite values, wrong rank and invalid classes fail. Row reordering is allowed if IDs match. Metrics use the fixed label-defined denominator. Other modalities require an explicit schema version and independent parser qualification.

Model bundle: code/factory, constructor config, weights and buffers, preprocessing, dependency lock, expected inputs/outputs and inference command. Training continuation additionally needs optimizer, scheduler/scaler when present, RNG, loader cursor, custom component state, model mode, committed step and restoration class. A weights file alone is insufficient. Use only trusted local fixture checkpoints with `weights_only=False`; never deserialize arbitrary candidate pickle in a trusted scorer.

## Runnable development commands

From `development/`, using Python 3.12 with `requirements.lock` installed:

```sh
python project.py --project digits --variant baseline --seed 11 --out /tmp/eidos-dev-baseline
python project.py --project digits --variant reference --seed 11 --out /tmp/eidos-dev-candidate
python fresh_check.py /tmp/eidos-dev-candidate
python grade.py --project digits --baseline /tmp/eidos-dev-baseline/test_predictions.npz --candidate /tmp/eidos-dev-candidate/test_predictions.npz
python qualify.py --out /tmp/eidos-development-qualification
```

These commands execute public references without Eidos. Integration requires mapping the same task inputs and output artifacts to each arm, with no requirement that external baselines implement Eidos's internal ledger.

## First public release and investigator bundles

Public release `research-public-v0.1.0` uses task interface `eidos-task/0.2` and answer interface `eidos-answer/0.2`. Migration from the earlier unpublished drafts: regenerate predictions and inputs because public identifiers changed; do not mix old/new IDs. Common answers add prediction/environment hashes, explicit confidence outcomes and stricter structures. The bundle wrapper is independently versioned `eidos-task-bundle/0.1`. Capability cards are descriptive reference cards, not complete final-admission manifests; unresolved task budgets and final fields block final admission.

D01, D02 and D12 have a restricted default investigator export:

```sh
python package_task.py --task D02 --out /tmp/eidos-D02-inputs
cd /tmp/eidos-D02-inputs
python project.py train --inputs . --out /tmp/eidos-D02-baseline --seed 11
```

Give the investigator only the exported directory plus the common contracts. It contains ordinary baseline source, train/validation arrays, task/brief and dependency lock. Test inputs are delivered separately at candidate inference; labels and graders remain with the evaluator. `project.py`, `data.py`, `qualify.py`, `check_bundles.py`, `lifecycle.py` and `qualification/` in the parent development directory are **known-development reference material** and excluded from that default export. Other cards currently have reference qualification, not restricted autonomous bundles. Builder access to the public repository still makes references accessible: enforce input/tool restrictions for an investigator run and record any reference-assisted repair. This separation is a packaging contract, not a security sandbox or contamination guarantee.

Regression run outputs belong outside this immutable source tree. Select the D01/D02/D12 smoke bundle checks for import, functioning-model improvement and null reporting; run lifecycle checks when changing restoration/recovery, and the full collection for research-contract changes. No requirement to run everything on each implementation edit.
