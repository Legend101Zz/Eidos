# Capability-card smoke runs through Eidos (`research-public-v0.1.0`)

Status: **hosted-Linux development runs plus reference-assisted builder checks.** The release qualifies only macOS
arm64 CPU; this Linux CPU profile is unqualified. Nothing here is a research result, an autonomous-investigator run
or a capability/superiority claim. No LLM, fixture provider or investigation was involved: every Eidos job executes a
*public reference command* from the release. Labelled data, `grade.py`, `fresh_check.py` and `lifecycle.py` equality
checks are **reference-assisted builder checks**: they ran as subprocesses of `scripts/research/cards.py`, outside any
investigator input path, reading only a copy of the release and writing only under
`/workspace/.eidos-dev/h2/research/` (the immutable `research/public/` tree was not written).

## Setup

- Lab: own instance `EIDOS_HOME=/workspace/.eidos-dev/h2/lab` (API 8875, PG 5655, Temporal 7950), project `cards`.
- Environment: `lab project register-env cards --python <research-dev python 3.12, torch 2.15.0+cpu> --root /workspace/.eidos-dev/h2/research/devcopy`;
  `devcopy` is a copy of `research/public/development` (source hashes match `qualification/results.json`).
- Components installed: `eidos.project.command@1.0.2`, `eidos.task.score-classification@1.0.0`, `eidos.task.score-regression@1.0.0` (new, this branch).
- Adapters: `scripts/research/cards.py` (driver, card table), `scripts/research/adapters/*.py` (instrument, branch and exit-17 wrappers,
  delivered to the job as a `source-bundle/1` containing the release's `.py` files unchanged plus the adapter), `scripts/research/builder_checks.py`.
  Generic helpers (card → exploration-only charter, run config) are in `src/eidos/tasks/cards.py`; they contain no task IDs or methods.
- Each card gets an exploration-only charter (`<id>-card`, e.g. `d02-card`), derived from the card's declared limits and operations; no success rule is claimed.

## Release's own checks (builder-side, no Eidos), hosted Linux

| Check | Result | Wall |
|---|---|---|
| `qualify.py --out .../research/qualify` | 27/27 reference cases pass (9 projects × seeds 11/23/37), 77 numeric/grader checks, 3 lifecycle seeds; exit 0 | 111.8 s (80 s self-reported) |
| `check_analysis_and_answer.py` | 5 estimator, 13 malformed-analysis, 3 valid-answer, 39 malformed-answer controls passed | 0.2 s |
| `check_bundles.py --out .../research/smoke` | `bundle_cases_passed: 3, negative_controls_passed: 2` | 16 s |

Lifecycle checks (`lifecycle.py qualify`) are part of `qualify.py` and passed (3 seeds). Linux-vs-macOS: classification scores are
identical to the release's recorded `qualification/results.json`; 5 of 27 regression rows (conditioning ×3, forecast ×2) differ at
1e-9…2e-7 absolute (see task-issues note 2).

## Rerun

```sh
EIDOS_HOME=<started lab> .venv/bin/python scripts/research/cards.py --setup --project cards \
    --out /workspace/.eidos-dev/h2/research/run-all --cards all        # all 14 cards, seeds 11,23,37; ~9 min
EIDOS_RESEARCH=1 EIDOS_HOME=<started lab> .venv/bin/python -m pytest tests/research -q   # cheap subset D01 D07 D12 D04, seed 11; ~85 s
```

`--cards cheap` = D01, D07, D12, D04. Environment overrides: `EIDOS_RESEARCH_ROOT`, `EIDOS_RESEARCH_PYTHON`.

## Results

Executed through Eidos: all 14 cards (all `eidos.project.command` jobs; plus Eidos-side scoring jobs for the 9 pair cards).
Result files (outside the repo): `/workspace/.eidos-dev/h2/research/run-all/results.json` (D01–D07, D12, D14; 532 s total for the first
full invocation incl. lifecycle), `run-instr/results.json` (D08, D13), `run-lc/results.json` (D09–D11, 79 s). The `run-all` D08/D13 entries
failed on an input-schema mismatch (see below) and were re-run in `run-instr`; the D09–D11 final code path is `run-lc`.
Per card ≈ 50 s for 3 seeds × (2 training jobs + 2 scoring jobs), ≈ 4.5 s per training job (interpreter/torch start dominates).

| Card | Kind | Through Eidos | Reference grade (`grade.py`, test split) | Eidos validation score (scorer job) vs `grade.py --split validation` | `fresh_check.py` | Card wall |
|---|---|---|---|---|---|---|
| D01 iris import | pair | yes | gain 0 on 3 seeds (identical predictions; no improvement claimed) | match, exact | exact ×3 | 49 s |
| D02 digits improvement | pair | yes | gains 0.119/0.139/0.164 (bar 0.02) | match | exact ×3 | 51 s |
| D03 digits shift | pair | yes | gains 0.256/0.258/0.211 (bar 0.01) | match | exact ×3 | 51 s |
| D04 conditioning | pair, regression | yes | RMSE gains 1.607/1.302/1.605 (bar 0.5) | match (new regression scorer) | exact ×3 | 50 s |
| D05 forecast | pair, regression | yes | RMSE gains 0.918/0.919/0.918 (bar 0.5) | match | exact ×3 | 50 s |
| D06 order | pair | yes | gains 0.496/0.500/0.494 (bar 0.3) | match | exact ×3 | 48 s |
| D07 weights | pair | yes | gains 0.80/0.90/0.80 (bar 0.5) | match | exact ×3 | 46 s |
| D08 digits instrument | instrument | yes | n/a (no gain); slice counts sum 359 = validation n, weighted aggregation = mean error 0.0557103… (equals `qualification.json`) | n/a | n/a | 3 s per job |
| D09 resume | lifecycle | yes (jobs), equality check builder-side | exact full-state equality of resumed-vs-uninterrupted 40-step checkpoint, 3/3 seeds | n/a | n/a | see lifecycle |
| D10 branch | lifecycle | yes (jobs) | parent sha256 identical before/after, child differs, 3/3 | n/a | n/a | |
| D11 exit 17 | lifecycle | partly | worker rc=17, checkpoint == clean step-17 checkpoint, new attempt equals uninterrupted run, 3/3 | n/a | n/a | |
| D12 null | pair | yes | accuracy 0.5000 → 0.5000, gain 0 on 3 seeds (the card's own pass rule) | match | exact ×3 | 49 s |
| D13 forecast instrument | instrument | yes | slice counts sum 256, aggregation checked, mean error 0.0192131… (equals `qualification.json`); same `instrument.py` sha256 `3dfbfd39…` as D08 | n/a | n/a | 3 s |
| D14 synthetic | pair | yes | gains 0.486/0.477/0.486 (bar 0.1); `synthetic_generated`=1536, `examples_seen`=276480 recorded in the project receipt | match | exact ×3 | 49 s |

Eidos-side scoring cross-check: for every pair row, the Eidos scorer job's validation accuracy/RMSE equals `grade.py --split validation`
within 1e-9 (27/27 rows). Baseline/reference scores through Eidos equal the standalone `qualify.py` scores bit-for-bit for all 27 rows.

### Per-seed pair jobs (baseline job / reference job)

| Card | Seed | Eidos jobs | `grade.py` test baseline → reference | Eidos validation score baseline → reference | fresh_check |
|---|---|---|---|---|---|
| D01 | 11 | `job_da6a998370da4365` / `job_4d5b159edbb94d13` | accuracy 0.8333 → 0.8333 (gain 0.0000; bar 0) | 1.0000 → 1.0000 | exact |
| D01 | 23 | `job_483b543a811f4edb` / `job_63fe2743ee3c4cdc` | accuracy 0.9000 → 0.9000 (gain 0.0000; bar 0) | 0.9667 → 0.9667 | exact |
| D01 | 37 | `job_11032ceddc2e4fb5` / `job_bb97b7c574924e43` | accuracy 0.8333 → 0.8333 (gain 0.0000; bar 0) | 1.0000 → 1.0000 | exact |
| D02 | 11 | `job_2ed52112e2ba4698` / `job_e63c12d8a7af4a9f` | accuracy 0.8583 → 0.9778 (gain 0.1194; bar 0.02) | 0.8384 → 0.9443 | exact |
| D02 | 23 | `job_6640d1ceda2b447b` / `job_ebe29e34ddd04751` | accuracy 0.8444 → 0.9833 (gain 0.1389; bar 0.02) | 0.8719 → 0.9499 | exact |
| D02 | 37 | `job_17ef850a963b4c47` / `job_587f96e2ecdb4285` | accuracy 0.8167 → 0.9806 (gain 0.1639; bar 0.02) | 0.8301 → 0.9526 | exact |
| D03 | 11 | `job_58aea32db2a246d4` / `job_bd5be4e1de5e48ed` | accuracy 0.6917 → 0.9472 (gain 0.2556; bar 0.01) | 0.6852 → 0.9359 | exact |
| D03 | 23 | `job_7a87067638664cf5` / `job_22e7fcf722d0424f` | accuracy 0.6917 → 0.9500 (gain 0.2583; bar 0.01) | 0.6936 → 0.9387 | exact |
| D03 | 37 | `job_18cedc712c034c51` / `job_823acbd155d74aa9` | accuracy 0.7361 → 0.9472 (gain 0.2111; bar 0.01) | 0.7047 → 0.9387 | exact |
| D04 | 11 | `job_c83629639cba4ee3` / `job_aabd451dfe714545` | rmse 2.1139 → 0.5068 (gain 1.6072; bar 0.5) | 2.0013 → 0.5316 | exact |
| D04 | 23 | `job_c3534a37dfd24766` / `job_61eb0ac894c64cd3` | rmse 2.1240 → 0.8220 (gain 1.3020; bar 0.5) | 2.0074 → 0.8241 | exact |
| D04 | 37 | `job_73b90b244e2549e7` / `job_7e5d96f0b8324dab` | rmse 2.1082 → 0.5030 (gain 1.6052; bar 0.5) | 1.9997 → 0.5091 | exact |
| D05 | 11 | `job_9e0e5000fbd14317` / `job_174852abfa614bca` | rmse 0.9404 → 0.0227 (gain 0.9177; bar 0.5) | 0.9277 → 0.0241 | exact |
| D05 | 23 | `job_c12a12ddb3834904` / `job_5ddb53f7c7f643bd` | rmse 0.9404 → 0.0218 (gain 0.9185; bar 0.5) | 0.9277 → 0.0225 | exact |
| D05 | 37 | `job_b9ce5574b7294f07` / `job_275376aabaee446d` | rmse 0.9404 → 0.0222 (gain 0.9182; bar 0.5) | 0.9277 → 0.0248 | exact |
| D06 | 11 | `job_263faefb5a7a43ca` / `job_67fca03a65f44c09` | accuracy 0.5039 → 1.0000 (gain 0.4961; bar 0.3) | 0.4805 → 1.0000 | exact |
| D06 | 23 | `job_3d594e79e70c450a` / `job_71b796f2cc0b412a` | accuracy 0.5000 → 1.0000 (gain 0.5000; bar 0.3) | 0.4805 → 1.0000 | exact |
| D06 | 37 | `job_c79ec3824ed64093` / `job_38dce70d4a2348b1` | accuracy 0.5059 → 1.0000 (gain 0.4941; bar 0.3) | 0.4766 → 1.0000 | exact |
| D07 | 11 | `job_c7037c97a20d41f0` / `job_3cf9bad8eba14336` | accuracy 0.0333 → 0.8333 (gain 0.8000; bar 0.5) | 0.0000 → 1.0000 | exact |
| D07 | 23 | `job_2b57cbb7a2034a31` / `job_935128e698ca49ce` | accuracy 0.0000 → 0.9000 (gain 0.9000; bar 0.5) | 0.0000 → 0.9667 | exact |
| D07 | 37 | `job_5a2740dcb0bb4167` / `job_51c19d7499bf4a4d` | accuracy 0.0333 → 0.8333 (gain 0.8000; bar 0.5) | 0.0000 → 1.0000 | exact |
| D12 | 11 | `job_3b71b6b5112c4f18` / `job_dcf0ac9d685a4529` | accuracy 0.5000 → 0.5000 (gain 0.0000; bar 0) | 0.5000 → 0.5000 | exact |
| D12 | 23 | `job_ab24ed4403434a57` / `job_207653c5cf874097` | accuracy 0.5000 → 0.5000 (gain 0.0000; bar 0) | 0.5000 → 0.5000 | exact |
| D12 | 37 | `job_2e6c6c70158c4034` / `job_20cea367d00b43db` | accuracy 0.5000 → 0.5000 (gain 0.0000; bar 0) | 0.5000 → 0.5000 | exact |
| D14 | 11 | `job_86bbfcf0b70f4406` / `job_84d245ee81144a22` | accuracy 0.5137 → 1.0000 (gain 0.4863; bar 0.1) | 0.5195 → 0.9922 | exact |
| D14 | 23 | `job_eb215810a6824cfc` / `job_4c1c9927f9a74d66` | accuracy 0.5137 → 0.9902 (gain 0.4766; bar 0.1) | 0.5195 → 1.0000 | exact |
| D14 | 37 | `job_1b648a3af9604116` / `job_33942205a18b484d` | accuracy 0.5137 → 1.0000 (gain 0.4863; bar 0.1) | 0.5195 → 0.9883 | exact |

### Instrument and lifecycle jobs

| Card | Seed | Eidos jobs |
|---|---|---|
| D08 | 11 | reference `job_1d3f6cbfee92436e`, instrument `job_c196c710b6524882` |
| D13 | 11 | reference `job_4f7fb4fc717843a1`, instrument `job_9fddc092b1f34a4a` |
| D09 | 11/23/37 | full 40 `job_12d15ac000c04831`/`job_10e9b5996ff443cb`/`job_207b310b909742fd`; parent 17 `job_98f2b5ebb3a7482f`/`job_8208471fd00449e6`/`job_fc478a38f3384dcf`; resume `job_6bcac8d1bdd24b21`/`job_82cbd58d5fe74ed4`/`job_24b843beb23e41d4` |
| D10 | 11/23/37 | branch `job_6e14c9ec704b47dd`/`job_90d7924c789543a5`/`job_18934dedb0b249e1` (parent = D09 parent jobs) |
| D11 | 11/23/37 | exit-17 worker `job_306e80da362b4389`/`job_3259770b46424c17`/`job_8cec06905acb4b2d`; new attempt `job_e496a6432ba240a2`/`job_45883b69ff134610`/`job_cfe0cfc46a1a4055` |

## Lifecycle cards: what ran through Eidos and what did not

The release's `lifecycle.py` fixture is a CPU single-process trainer that writes a complete checkpoint (model, optimizer, three RNGs, data RNG,
counters). Eidos has no native restore of this format (`eidos.pytorch.weight-edit` operates on `eidos-checkpoint/1`, a different format), so:

- **D09 (exact resume):** the uninterrupted run, the step-17 parent and the resumed run are three separate Eidos jobs (`lifecycle.py worker`
  unchanged); the parent checkpoint travels as a content-addressed Eidos artifact into the resume job. Exact equality (all state, not just
  weights) is checked builder-side with the release's own `lifecycle.equal`. Eidos did not itself perform the restore or know the format.
- **D10 (private branch + weight edit):** a source-bundle adapter calls the release's `restore`/`advance`/`capture`, applies the explicit `+0.2`
  weight edit to the first parameter and trains to step 40. The parent artifact is read-only input; its sha256 is identical before and after
  (recorded in the job and re-hashed from the stored artifact), and the child differs from the unedited run. The edit is not an Eidos
  weight-edit component call.
- **D11 (exit 17 after checkpoint → new attempt):** `lifecycle_crash.py` runs the release worker with `--crash` in a subprocess and asserts return
  code 17 and a committed checkpoint, then finishes normally so Eidos stages the checkpoint. A second Eidos job is the new attempt and matches the
  uninterrupted run exactly. **This is a subprocess death inside one Eidos job, not the death of an Eidos worker.** A job whose command exits non-zero
  stages nothing (`eidos.project.command` raises before staging), so a literally crashing worker's checkpoint cannot be recovered through that
  component. Eidos's own worker-death/retry semantics were not exercised here.
- The release's own `lifecycle.py qualify` (3 seeds incl. negative controls for missing RNG/optimizer state) ran standalone inside `qualify.py` and passed.

## Defects and frictions seen

- `run-all` D08/D13 failed with `RESOLUTION_ERROR: input 'validation' of eidos.project.command@1.0.2 needs task-array/1, artifact provides prediction-array/1`.
  `eidos.project.command` declares no `prediction-array/1` input port. The driver re-registers the identical bytes as `task-array/1`. Needed product change:
  add an optional prediction-array input (or a generic `artifact` port) to `eidos.project.command` (not edited: existing component directories are out of scope).
- In project-root mode `eidos.project.command` copies every small top-level file into the job work directory, so the reference-only `grade.py`, `data.py`
  (which holds the public labels) are present in the work dir of these builder runs. That is intended for builder checks and is why these runs are labelled; an
  investigator run must use the restricted bundle (`package_task.py`), which excludes them.
- The charter commit for each card is exploration-only (`commit_study_charter`); runs were not made inside an investigation, so the ledger records no
  investigator decisions for these runs.

Package-side issues are in `research-task-issues.md`.
