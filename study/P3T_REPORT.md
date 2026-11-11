# P3T delivery report

Completed as tooling qualification only, with zero model API calls and no real investigator arm runs. Existing clean clone verified at `/Volumes/Mrigesh SSD/eidos-v1/p3`, origin `https://github.com/Legend101Zz/Eidos.git`, branch `task/pilot-3-tooling`, starting at `7e6641d9f91a9adfd30b15e2c821ce25e5eb11a4`. The grading repository is `~/eidos-internal/eidos-grading`, branch `main`.

|Step|Real output / result|
|---|---|
|1. Datasets|Exactly N01 load_breast_cancer, N02 load_diabetes, N03 make_friedman1(2000, noise=1, seed 20261111), N04 the specified make_classification(3000, 20 features, 8 informative, 4 redundant, 3 clusters/class, separation 0.8, flip 0.02, seed 20261111). Entire scientific qualification ran with network denied.|
|2. Splits|Train/validation/test counts: N01 341/114/114; N02 265/88/89; N03 1200/400/400; N04 1800/600/600. Two sklearn splits with seed 20261111; classification stratified. Test IDs disjoint; every array hash frozen.|
|3. Baseline|Ordinary PyTorch project.py train/infer CLI; raw features, linear model, full-batch Adam lr 0.01, 80 epochs, CPU, one thread. All 12 measured training processes completed in 1.087791250–1.374918292 seconds, including Python startup.|
|4. Bars|N01/N04 +0.02 accuracy. N02 8.900388389192335 RMSE reduction. N03 0.5521391269423149 RMSE reduction. Regression values equal 5% of the fixed seed-11 test baseline and are stored without display rounding.|
|5. Qualification|N01–N04 accepted at seeds 11, 23, 37; no drops, replacements, tuning or recipe changes. Fixed references were committed before first execution. Full baseline/reference/gain/timing values appear in QUALIFICATION_P3.md and p3_frozen.json.|
|6. Packaging|Four committed seven-file investigator bundles. Sizes: N01 62,885 bytes; N02 25,848; N03 82,495; N04 200,996. Manifest hashes, copied schema/lock and supplied baseline verified. No test data or reference recipe.|
|7. Study wiring|Shared D02–D06 success and fresh-seed rules; exact practical-bar sentence structure; N01/N04 I4 accuracy path; N02/N03 documented missing RMSE assessor. Actual Eidos charter validation passed all four. `launcher plan` output: `Planned 12 randomized paired blocks, 48 assignments.`|
|8. GitHub grader|All four cards supported in prepare/run/score/submit/fetch/workflow. Hosted prepare reproduces all frozen bundles byte for byte and exports only x/ids. N01 and N03 reference/baseline/invalid controls all match expected outcomes; 6/6 workflows complete successfully, both jobs each. QUALIFICATION.md updated in grading main.|
|9. Dry run|One completed N01 block and one completed N03 block, four arms each. `DRY RUN PASSED: 8 valid baseline candidates, zero gain, zero successes, zero tokens, 2 plots; no model calls`. Whole-block incidents and retries retained. Analysis: success [0,0,0,0], primary difference 0, exploratory interval [0,0], 8 assigned, 0 missing, 8 unknown billed costs.|
|10. Record|QUALIFICATION_P3.md records the full procedure, every metric and seed, split hashes, runtimes, all operational failures, all six hosted grades and a statement that cards/bars/bundles were committed before any stub arm.|

|Card|Metric|Seed-11 baseline|Reference at each of 11 / 23 / 37|Gain at each seed|Bar|
|---|---|---:|---|---|---:|
|N01|accuracy|0.912280701754|0.973684210526 / 0.973684210526 / 0.973684210526|0.061403508772 / 0.061403508772 / 0.061403508772|0.02|
|N02|rmse|178.007767783847|52.011559852971 / 52.011559852971 / 52.011559852971|125.996207930876 / 125.996207930876 / 125.996207930876|8.9003883891923348|
|N03|rmse|11.042782538846|1.430412055453 / 1.430412055453 / 1.430412055453|9.612370483394 / 9.612370483394 / 9.612370483394|0.55213912694231493|
|N04|accuracy|0.688333333333|0.870000000000 / 0.870000000000 / 0.870000000000|0.181666666667 / 0.181666666667 / 0.181666666667|0.02|

Hosted controls (fresh 23/37 confirmations pass for both references):

|Card|Reference passes|Claimed baseline fails|Invalid output is candidate failure|
|---|---|---|---|
|N01|[43013476014](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013476014)|[43013552767](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013552767)|[43013577941](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013577941)|
|N03|[43013615358](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013615358)|[43013651544](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013651544)|[43013686502](https://github.com/Legend101Zz/eidos-grading/actions/runs/43013686502)|

N03 hosted baseline gain is 4.615258397677735e-08 RMSE units, a cross-CPU rounding difference below its frozen bar. N03 hosted reference gain is 9.61237048792997; N01 gain is 0.061403508771929904. Full grade fields, confirmation values, archive hashes, workflow event SHAs and run timestamps are committed under `study/packaging/evidence/p3` and grading `qualification_p3`.

Verification: study tests 26/26; grader tests 10/10; actual charter validation 4/4; frozen bundle/hash checks 4/4; public tree unchanged; shell syntax and git diff whitespace checks pass. A broader 21-test arm contract/parity check has four failing assertions in an existing recovered-provider-error test, reproduced identically at pristine base 7e6641d. That existing behavior/test is unchanged. See committed logs and QUALIFICATION_P3.md for the initial environment errors and their corrections.

Commit pins:

* Prospective procedure, before reference execution: `879c0942128f76e69a5a48d04ce228338d4a2551` (16:54:15 IST).
* Card/bar/bundle freeze, before any stub arm: `5bedf94b66da08c00610d742d261abcd715774f0` (16:58:50 IST).
* Hosted grader executable implementation: `f186ff2dbd9fc7f8186a5c9ad40bff266b940cd8`.
* Grading main delivery/qualification record: `6fe544052e01f1abca4c6f982d45bcdd30413db7`.
* Six grading workflow event SHAs are in `study/packaging/evidence/p3/hosted-workflow-runs.json`. The final Eidos delivery SHA is the commit containing this report, printed by `git log -1 --format=%H`.

Only `task/pilot-3-tooling` and grading `main` were pushed. All created commits use Legend101Zz <96632943+Legend101Zz@users.noreply.github.com>. The concurrent grading candidate commits were preserved through ordinary rebase/push, with no force push. No research/public, pilot-2 output/lab or round-3 worktree changes. Research Python remained read-only.

Exact controller commands, including environment exports, the dedicated lab/SSD paths, design, launch, grading, hosted submit/fetch and analysis: [PILOT_3_COMMANDS.md](PILOT_3_COMMANDS.md). Non-stub execution requires separate authorization and a fixed round-3 installation; this ticket did not build or run that model/arm installation. The independently runnable tooling dry run is:

```sh
cd "/Volumes/Mrigesh SSD/eidos-v1/p3"
bash study/dry_run_p3.sh "/Volumes/Mrigesh SSD/eidos-v1/p3-artifacts/dry-run-new"
```

The requested real pilot controller sequence after the environment/preconditions in that document is:

```sh
"$EIDOS_RESEARCH_PYTHON" -m study.launcher plan --cards N01,N02,N03,N04 --runs 3 --seed 20261111 --out "$P3_OUTPUT/design.json"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher run --design "$P3_OUTPUT/design.json" --runs-root "$P3_OUTPUT/runs" --concurrency 2
for block in "$P3_OUTPUT"/runs/*/block_record.json; do
  "$EIDOS_RESEARCH_PYTHON" -m study.grade_block --block-record "$block" --grading-root "$P3_OUTPUT/grading" --results "$P3_OUTPUT/results.json"
done
"$EIDOS_RESEARCH_PYTHON" -m study.analyze --design "$P3_OUTPUT/design.json" --results "$P3_OUTPUT/results.json" --out "$P3_OUTPUT/analysis"
```

Large/cache/temp artifacts are on `/Volumes/Mrigesh SSD/eidos-v1/p3-artifacts`, with the required UV cache and TMPDIR exports. The existing npm cache was moved to the SSD with a symlink retained to satisfy the unchanged disk guard; the operation and original/final disk readings are recorded in QUALIFICATION_P3.md. No secrets were printed.
