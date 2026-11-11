# Pilot 2 dry run

Executed D03-r1 and D04-r1 with `--concurrency 2 --stub`. Both blocks: `ok`.
Eight arm grades: `valid`, gain 0, verified_success false. No model calls.
Analysis: draft estimator, descriptive tables, two PNGs.
Artifacts: `~/eidos-internal/p2-dry/{design.json,results.json,runs,grading,analysis}`.

Local controls: D03/D04 planted recipe passes including fresh seeds 23/37;
baseline fails; duplicate prediction IDs fail as invalid_output. Artifacts:
`~/eidos-internal/p2-local-controls/results.json`.

Reproduce with fresh directories using `study.packaging.qualify_p2`,
`tools/qualification_cases.py --card D03|D04`, `study.packaging.qualify_controls`,
then the launcher and grader commands in `PILOT_2_COMMANDS.md` with `--stub`.

Validation: 23 local tests pass; 10 installed pilot-2 arm contract tests pass.
Grading repo: 8 tests pass with EIDOS_PUBLIC_DEV pointing to the pinned public development source; no skips.
Pilot-2 fixed wheels installed into `~/eidos-internal/venvs/pilot-2-lab`;
build receipt: `pilot-build.json` inside that environment. Pilot-1 lab was not started.
All seven prospective charters validate against the installed domain schema.
Full controller design: 12 randomized paired blocks, 48 assignments.

The 10:45 IST deadline was missed while hosted qualification was still running.

Regression reload checks allow rtol=1e-5 and atol=1e-6 to account for macOS/Linux CPU kernel rounding. Classification predictions and all IDs remain exact. Nonfinite outputs are rejected by the independent scorer.
