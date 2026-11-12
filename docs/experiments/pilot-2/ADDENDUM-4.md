# Development pilot 2, addendum 4 (erratum, after the pilot): a grader defect caused arm 2's only failure

Written on 11 November 2026 at about 20:25 IST, after pilot 2 ended. It was found during pilot 3 (pilot-3 addendum 4).

## The defect
- **What happened:** both graders ran fresh-seed confirmation inference with a hard-coded `confirmation-<seed>/model.pt` instead of the candidate's declared `model_path`.
- **Where it hit:** in D03-r2, arm 2 declared `model_path: model.joblib` (a scikit-learn recipe) and was graded `invalid_output` (local and GitHub agreed).
- **The scan:** every other pilot-1 and pilot-2 grade (local and GitHub) was checked for this failure, and there were no other occurrences.

## Correction
- **The re-grade:** re-graded with the fixed local grader (`~/eidos-internal/pilot-2/regrade-addendum4/`), D03-r2 arm 2 is a **verified success**: test accuracy gain +0.303, reproduced at seeds 23 and 37 (+0.303 and +0.303).
- **On GitHub:** the arm is re-submitted to the fixed GitHub grader, the verdict of record.
- **The original grades are kept.**

## Corrected pilot-2 outcome
| | Arm 1 | Arm 2 | Arm 3 | Arm 4 |
|---|---|---|---|---|
| Verified successes | 11/12 | **12/12** (was 11/12) | 7/12 | 12/12 |

**The primary contrast arm 4 − arm 2 is 0.0, not +0.083.** Any earlier statement that the Eidos investigator verified more improvements than the coding agent with the toolkit in pilot 2 is withdrawn. The token and wall-time differences, and the D03 gain sizes, are unaffected.
