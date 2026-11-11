# Development pilot 2, addendum 1: transient rate-limit notices were treated as incidents

Written on 11 November 2026 at about 13:35 IST, after block D05-r1 finished and while D03-r2 and D04-r1 were running. No arm, prompt, envelope, success rule or Eidos source changed.

## What happened
- With two blocks running at once (eight agents), MiniMax occasionally returned HTTP 429.
- Claude Code retries those automatically (`{"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10, "error_status": 429}`).
- The study runner for arms 1–3 treated any event text matching its incident rule as an incident **and stopped the run immediately**. In D05-r1, arms 2 and 3 were stopped about 60 s in, after a single retry notice, and the block was recorded as an incident.
- The arm-4 classifier had the mirror-image problem: one recovered 429 anywhere in the receipts made the whole run an incident.
- Left in place, most blocks would have become incidents, and arm runs would have been cut short by a tooling decision, not by the provider.

## Fix (study tooling only; symmetric across arms)
- **Arms 1–3** (`study/run_arm.py`): provider error text no longer stops a run in progress, and the client's own retry notices (`system/api_retry`) are ignored. A run is an incident only if it then ends **without a valid final result** and a provider 429/5xx/quota failure was observed.
- **Arm 4** (`study/eidos_arms/common.py`): a concluded investigation with a valid answer is `ok`, even if a request hit a 429. A provider failure is an incident only if the investigation ended without concluding.
- **Both:** timeouts and token-budget stops keep their own status. The model-id-change rule is unchanged.
- **Tests:** `study/eidos_arms/test_contract.py` (arm 4); the arms-1–3 decision was replayed on the D05-r1 events.

## Charter consequence
- D05-r1 was stopped by the defect, and D03-r2 and D04-r1 were stopped by the controller to apply the fix. All three are infrastructure interruptions; they are archived under `pilot-2/runs/aborted-addendum1/` and **re-run whole** under the charter's incident rule. No outcome of these partial runs is used.
- The fix applies to every block from the restart on.
