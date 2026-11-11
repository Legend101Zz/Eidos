# Pilot 3 prospective qualification

Procedure fixed before any reference execution in commit `879c0942128f76e69a5a48d04ce228338d4a2551`. See [PROSPECTIVE_P3.md](PROSPECTIVE_P3.md) for the exact generators, split rounding, baseline and fixed references. No recipe was changed after seeing results. No cards were dropped. No model calls.

Execution: read-only research Python; CPU, one thread; macOS `sandbox-exec` with `(deny network*)` around the entire qualification process. CPython and library versions are recorded in [p3_runtime.json](p3_runtime.json). Raw metrics, all timings and every train/validation/test array hash are in [p3_frozen.json](p3_frozen.json). Hashes encode `str((shape, dtype.str))` followed by contiguous array bytes. Test arrays were never packaged.

Baseline: raw features, single linear layer, Adam lr=0.01, 80 full-batch epochs. The default load_diabetes representation is used without extra scaling. The gain comparator is always baseline seed 11; same-seed baseline diagnostics are also recorded. Seeds 11/23/37 are fresh initializations; deterministic references legitimately produce identical metrics across seeds.

|Card|Train / validation / test|Metric|Seed-11 baseline|Absolute bar|
|---|---|---|---:|---:|
|N01|341 / 114 / 114|accuracy|0.91228070175438591|0.02|
|N02|265 / 88 / 89|rmse|178.0077677838467|8.9003883891923348|
|N03|1200 / 400 / 400|rmse|11.042782538846298|0.55213912694231493|
|N04|1800 / 600 / 600|accuracy|0.68833333333333335|0.02|

Classification bars are +0.02 accuracy; regression absolute bars are exactly 0.05 × baseline seed-11 test RMSE. Every reference below passes its frozen bar.

|Card|Seed|Fixed baseline comparator|Diagnostic baseline at seed|Reference|Gain|Baseline process seconds|Reference fit/predict seconds|Pass|
|---|---:|---:|---:|---:|---:|---:|---:|---|
|N01|11|0.91228070175438591|0.91228070175438591|0.97368421052631582|0.061403508771929904|1.374918292|0.017424833|yes|
|N01|23|0.91228070175438591|0.83333333333333337|0.97368421052631582|0.061403508771929904|1.141724625|0.001796000|yes|
|N01|37|0.91228070175438591|0.85087719298245612|0.97368421052631582|0.061403508771929904|1.095244250|0.001667125|yes|
|N02|11|178.0077677838467|178.0077677838467|52.011559852970734|125.99620793087597|1.101176916|0.007216709|yes|
|N02|23|178.0077677838467|177.77617924501973|52.011559852970734|125.99620793087597|1.091185875|0.001069125|yes|
|N02|37|178.0077677838467|178.07065590835157|52.011559852970734|125.99620793087597|1.087791250|0.000933083|yes|
|N03|11|11.042782538846298|11.042782538846298|1.4304120554527633|9.6123704833935353|1.097057333|0.075175000|yes|
|N03|23|11.042782538846298|11.023737510584956|1.4304120554527633|9.6123704833935353|1.356372917|0.070279916|yes|
|N03|37|11.042782538846298|10.864258361476159|1.4304120554527633|9.6123704833935353|1.209007000|0.070304084|yes|
|N04|11|0.68833333333333335|0.68833333333333335|0.87|0.18166666666666664|1.155740625|0.046359666|yes|
|N04|23|0.68833333333333335|0.69666666666666666|0.87|0.18166666666666664|1.102703875|0.042914458|yes|
|N04|37|0.68833333333333335|0.69833333333333336|0.87|0.18166666666666664|1.111739333|0.042683208|yes|

All 12 baseline training processes completed in under 60 seconds; measured wall time includes Python startup. References used only the train split for fitting. The train-only StandardScaler/SVC, StandardScaler/Ridge and fixed histogram gradient booster are specified in the prospective record.

Packaging: committed bundles under `bundles/N01`–`bundles/N04` have exactly `brief.md`, `project.py`, `train.npz`, `validation.npz`, `requirements.lock`, `task.json`, `answer_schema.py`. Each task manifest hashes the other six files, gives the metric direction, and excludes the test split and reference recipe. The answer schema is copied unchanged from public development.

Prospective freeze: the cards, numeric bars, split hashes, baseline, and packaged investigator bundles are fixed by the commit containing this record before any arm run, including infrastructure stubs. The earlier recipe-selection commit predates all reference qualification. Later qualification evidence may be appended without changing cards, recipes or bars.

Local study wiring: accepted N01–N04 use the unchanged D02–D06 success rule and mandatory fresh confirmations at 23/37. N01/N04 use the I4 classification accuracy path. N02/N03 document the existing missing RMSE assessor and remain exploration-only in arm 4.

The card/bar/bundle freeze commit is
`5bedf94b66da08c00610d742d261abcd715774f0` (2026-11-11 16:58:50 IST),
pushed on `task/pilot-3-tooling` before the first stub arm started.
Reference selection was fixed by
`879c0942128f76e69a5a48d04ce228338d4a2551` (16:54:15 IST), before
scientific qualification. No investigator/model arm has run.

Local plan output: `Planned 12 randomized paired blocks, 48 assignments.`
Exact design: [evidence/p3/plan.json](evidence/p3/plan.json).

Local controls: `PASS: N01/N03 reference passes, claimed baseline fails at zero
gain, invalid output is a candidate failure`. All six results are in
[evidence/p3/local-grader-controls.json](evidence/p3/local-grader-controls.json).
Both positive controls pass reload equality and independent fresh training at
23/37; baseline controls have exactly zero gain; invalid controls deliberately
hash-mismatch the model and are `invalid_output`, never an incident.

Dry run output: `DRY RUN PASSED: 8 valid baseline candidates, zero gain, zero
successes, zero tokens, 2 plots; no model calls`. One completed N01-r1 block and
one completed N03-r1 block; each contains all four arms. Successful retry blocks
started 2026-11-11 17:02:37 IST. All eight outcomes are primary-eligible and
valid; all fresh-process reloads match; no improvement is claimed. The analysis
reports success [0,0,0,0], arm-4 minus arm-2 = 0, exploratory interval [0,0],
8 assigned primary observations, 0 missing observations and 8 unknown billed
cost entries. Unknown cost is retained, not invented as zero. The two generated
PNGs are `success_by_arm_card.png` and `resources_by_arm.png` in the SSD dry-run
analysis directory. Committed results, estimator, transcript audit, run receipts
and block history are under [evidence/p3](evidence/p3).

Recorded implementation/operational failures, preserved on the SSD:

* First stub attempt: an inherited `EIDOS_STUDY_PYTHON` referenced an absent
  internal-disk pilot-1 build receipt; arm 3/4 failed, and both complete paired
  blocks correctly became incidents. The dry-run helper now selects the valid
  SSD pilot-1 receipt explicitly (override: `EIDOS_P3_STUB_PYTHON`). Stubs never
  use or start that lab.
* First retry and control setup: the unchanged 12.5 GiB internal free-space
  guard stopped preparation with 13,087,112 KiB available (20,088 KiB below its
  13,107,200 KiB threshold). Relocated the existing 399 MiB npm cache from
  `~/.npm/_cacache` to `/Volumes/Mrigesh SSD/eidos-v1/p3-artifacts/relocated-npm-cache`
  and preserved its old path with a symlink. Free space then read 13,493,100 KiB.
  No guard was bypassed or changed. The launcher's `--replace-incident` archived
  both earlier paired-block attempts before the successful retry.
* First authored positive-control attempt lacked a required validation
  measurement, so answer-schema validation correctly rejected it. Added actual
  validation baseline/candidate measurements and a hashed evidence receipt.
* Wrapping the entire controller in Seatbelt also prevented its existing
  `/bin/ps` process cleanup. Moved the no-network wrapper to reference training
  subprocesses; candidate execution retains the existing no-network Seatbelt
  adapter. The original prospective scientific qualification itself completed
  entirely under `(deny network*)` and was never rerun or tuned.
* An early optional grader test ran before N03 control generation completed
  and failed because its answer template did not yet exist. It passed once
  all controls completed; no candidate, recipe or bar changed for that test.
* The initial grading-main push was rejected because other candidate commits
  had arrived. Fetched and rebased the single tooling commit without altering
  those commits, then pushed normally. No force pushes or other branches.

Verification: study suite 26/26 passes; grading suite 10/10 passes; all four
charters pass the actual Eidos validator. Hosted prepare is byte-identical to
the committed bundle for every card and emits inference x/ids only. A broader
21-test arm contract/parity suite has four failing assertions within the
existing provider-recovery test: it expects recovered HTTP errors to remain
incidents, while the checked-in final-outcome policy accepts a successful final
result. The identical four failures reproduce on pristine base `7e6641d`; this
ticket leaves that behavior and test unchanged. Initial environment-only errors
(missing local source imports and an inherited invalid lab path) were resolved
by setting the local package PYTHONPATH and valid SSD receipt. Logs are committed
under `evidence/p3`; no model API calls were made by these fixtures.

Hosted qualification results follow; all `grade.json` files were fetched and checked.
The grader implementation is `f186ff2dbd9fc7f8186a5c9ad40bff266b940cd8` on main;
the candidate asset commits do not change executable code. N01–N04 trusted
source is byte-pinned to the Eidos freeze commit; the public scorer/validator
remains pinned to `ee46a03beaf9b8679c1cf4b19b1424cc68611d42`.

Hosted qualification completed on 2026-11-11. All six independent run/score
workflows succeeded, and their fetched grades match all expected candidate outcomes.

|Card|Control|GitHub run|Status / grading status|Success|Baseline|Candidate|Gain|
|---|---|---|---|---|---:|---:|---:|
|N01|baseline|[43013552767](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013552767)|valid / scored|false|0.91228070175438591|0.91228070175438591|0|
|N01|invalid|[43013577941](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013577941)|invalid_output / candidate_failure|false|—|—|—|
|N01|reference|[43013476014](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013476014)|valid / scored|true|0.91228070175438591|0.97368421052631582|0.061403508771929904|
|N03|baseline|[43013651544](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013651544)|valid / scored|false|11.042782576839159|11.042782530686575|4.615258397677735e-08|
|N03|invalid|[43013686502](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013686502)|invalid_output / candidate_failure|false|—|—|—|
|N03|reference|[43013615358](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013615358)|valid / scored|true|11.042782543382733|1.4304120554527633|9.6123704879299705|

|Card|Control|Fresh seed|Baseline|Candidate|Gain|
|---|---|---:|---:|---:|---:|
|N01|baseline|23|0.91228070175438591|0.83333333333333337|-0.078947368421052544|
|N01|baseline|37|0.91228070175438591|0.85087719298245612|-0.061403508771929793|
|N01|reference|23|0.91228070175438591|0.97368421052631582|0.061403508771929904|
|N01|reference|37|0.91228070175438591|0.97368421052631582|0.061403508771929904|
|N03|baseline|23|11.042782576839159|11.023737385883512|0.019045190955646163|
|N03|baseline|37|11.042782576839159|10.864258299856257|0.17852427698290185|
|N03|reference|23|11.042782543382733|1.4304120554527633|9.6123704879299705|
|N03|reference|37|11.042782543382733|1.4304120554527633|9.6123704879299705|

Both references pass fresh-process test/reload equality, validation export equality,
guardrails and independent fresh-training confirmations at seeds 23/37. Both claimed
baselines fail numerical and confirmation requirements. N03 has a cross-CPU rounding
gain of 4.615258397677735e-08 RMSE units, far below its 0.5521391269423149 bar;
the existing pilot-2 floating tolerance was reused unchanged. Both intentionally
hash-mismatched model declarations are `invalid_output` / `candidate_failure`,
with no infrastructure incident. The invalid fixtures fail artifact verification
before inference; additional N01/N03 scorer tests reject NaN serialized predictions
as candidate failures, with the exact nonfinite-prediction reason.

Every fetched grade and its workflow event SHA, run ID, candidate archive hash,
creation/completion timestamp and URL is committed under `evidence/p3/hosted-*`.
The workflow labels came from pinned offline scikit-learn loaders/generators in
its separate score VM, never from the candidate VM. No candidate checkpoint/code
was imported or deserialized by that score VM. No qualification control is a
comparative pilot observation. No model calls were made.

Controller commands: [../PILOT_3_COMMANDS.md](../PILOT_3_COMMANDS.md). The later
pilot needs its separately authorized, fixed round-3 Eidos installation; this
ticket did not read or change round-3 worktrees, pilot-2 output, or its lab.
`research/public` is unchanged from base 7e6641d. All artifacts, temporary files,
candidate-store clone and caches for this work are on the SSD, apart from small
grading source/qualification files in the user-specified grading repository.
