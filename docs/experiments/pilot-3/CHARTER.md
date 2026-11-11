# Pilot 3 prospective development charter

Tooling ticket P3T. This charter fixes task selection and practical bars before
any investigator arm run. No model calls are authorized by this tooling ticket.
The later pilot requires separate run authorization and a fixed round-3 Eidos
installation. Do not reuse or alter pilot-2 records or its lab.

The immutable `research/public/STUDY_PROTOCOL.md` remains the draft protocol.
Reuse pilot-2 arm definitions, budgets, final-outcome classification, identical
research environments, METHOD visibility, arm-4 setup timing, four-arm paired
blocks, whole-block incident policy, independent scoring and fresh seeds 23/37.
No new success behavior is introduced.

Dataset and recipe selection was fixed in commit
879c094 (`study/packaging/PROSPECTIVE_P3.md`) before reference execution. The
fixed offline procedure accepted N01, N02, N03 and N04; none were dropped.

|Card|Dataset|Metric|Absolute practical gain|
|---|---|---|---:|
|N01|load_breast_cancer|accuracy|0.02|
|N02|load_diabetes|RMSE reduction|8.900388389192335|
|N03|make_friedman1, seed 20261111|RMSE reduction|0.5521391269423149|
|N04|make_classification, seed 20261111|accuracy|0.02|

The two RMSE bars are exactly 5% of their fixed baseline seed-11 test RMSE.
Every candidate is compared with that seed-11 baseline, including fresh training
confirmations. The usual positive claim, numerical gain, reproduction, guardrail
and scope checks all apply. Classification uses the I4 accuracy assessor path;
the missing built-in RMSE assessor is documented in `CHARTER_RULES.md` and leaves
regression charters exploration-only. Local and hosted evaluators enforce RMSE.

Investigator bundles contain only the seven allowlisted files, training and
validation data, and neutral task text. Test data and reference recipes remain
controller-only. Full qualification numbers and frozen split hashes are in
`study/packaging/QUALIFICATION_P3.md` and `p3_frozen.json`. These cards, bars and
bundle hashes must be committed before even a stub arm is run. Stub controls
are infrastructure qualification, never scientific pilot observations.
