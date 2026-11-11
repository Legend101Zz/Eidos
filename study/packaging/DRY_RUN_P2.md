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
