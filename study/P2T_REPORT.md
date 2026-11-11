# P2T outputs

The 10:45 IST deadline was missed. All reported controls are development tooling
qualification; no model calls were made.

1. Packaging: D03–D06 each export exactly seven allowlisted files; train and
   validation only, no test split or reference recipe. Hashes and metric direction
   checked for all four cards.
2. Practical bars: D03 +0.01 accuracy; D04/D05 0.5 RMSE reduction; D06 +0.30
   accuracy. All four require fresh initialization in the common D02 sentence.
3. Local grading: all six planted/baseline/invalid controls pass, including fresh
   seeds 23/37. Rules, package allowlist and float reload tests: 23/23 pass.
4. Charters: D03/D06 classification-report accuracy with paired-classification-report
   and paired-ci@1.2.0, respective bars 0.01/0.30. No built-in RMSE assessor exists;
   D04/D05 exploration-only gap is documented in eidos_arms/CHARTER_RULES.md.
   Ten installed-runtime arm contract tests pass; all seven card charters validate.
5. Launcher: full design reports 12 randomized paired blocks, 48 assignments.
   Two concurrent blocks, each four arms; arms 3/4 retain UUID-scoped projects.
   Default plan remains nine pilot-1 blocks. Separate pilot-2 fixed wheels built
   successfully; no pilot-1 lab was started or changed.
6. GitHub: all six final hosted controls pass qualification; exact run IDs and
   metrics are in ../eidos-grading/QUALIFICATION.md. Private grader tests: 8/8 pass.
7. Baseline sanity: packaging/QUALIFICATION_P2.md records all four baseline
   metrics and reference gains at seeds 11/23/37. Every reference seed clears its bar.
8. Dry run: D03-r1 and D04-r1 both ok; eight valid baseline grades, zero gain, zero
   improvement successes, two plots. See packaging/DRY_RUN_P2.md.

Exact controller commands (including a separate runtime, plan, concurrent launch,
local grading, analysis and private GitHub submission/fetch): PILOT_2_COMMANDS.md.

Public research tree unchanged from origin/task/merge-1.2. Root filesystem
remained above 14 GiB free. Git commits use Legend101Zz's required identity;
only task/pilot-2-tooling and grading main were pushed. Final SHAs are reported
in the handoff. Optional MiniMax smoke was not run.
