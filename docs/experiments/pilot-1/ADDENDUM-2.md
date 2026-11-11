# Development pilot 1, addendum 2: an arm-4 incident misclassification and the aborted re-roll

Written on 11 November 2026 at about 04:52 IST, after block K=4 (D12-r1) and before K=5 started. No arm, prompt, envelope or success rule changed.

## What happened
- Block K=4 (D12-r1) finished at 04:45. The launcher recorded it as an incident: "provider/infrastructure failure".
- The study's arm-4 classifier (`study/eidos_arms/common.py`) searched the **whole failure JSON** for `\b(?:429|5\d\d)\b`. That pattern also matches token counts such as `"output_tokens": 541`.
- The actual failure was a plain `ModelTimeoutError` (one investigator request exceeded the 300 s request timeout, with no provider status). After it, arm 4 ran until its 1800 s deadline.
- The charter's incident rule covers only provider 429/5xx or quota errors, lab or infrastructure crashes, and model-id changes. Arms 1–3 use a stricter text rule (`study/run_arm.py`, `incident_text`) that needs an HTTP or status context. The two arms' classifiers therefore disagreed. Left in place, this would have re-rolled blocks only when arm 4 had an error, which biases the comparison towards arm 4.

## What was done
1. **Classifier fixed** (commit `2fce1db`). Arm 4 now classifies only error strings and HTTP status codes, with the same rule as arms 1–3. A request timeout without a provider status is the system's own behaviour, not an incident.
2. **The automatic re-roll was aborted** two minutes after it started (04:46–04:48):
   - its launcher and arm processes were stopped;
   - its arm-4 investigation was stopped through the lab;
   - its partial directory is kept under `pilot-1/runs/superseded/D12-r1-attempt2-aborted-misclassified-reroll/`;
   - no grade was produced from it.
3. **K=4 attempt 1 is the outcome of record.**
   - Arms 1–3 keep their own statuses (`pre_block_status`).
   - Arm 4 is re-classified with the fixed rule: `timeout`.
   - The as-run incident label is kept in each run record (`block_incident_reason_as_run`).
4. Blocks K≥5 run with the fixed classifier.

## Effect on reporting
- The paper reports K=4 from attempt 1, with this addendum.
- No other block was labelled an incident by the old rule. K=1 is replaced for the separate adapter defect of addendum 1.
