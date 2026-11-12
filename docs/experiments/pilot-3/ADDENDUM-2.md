# Development pilot 3, addendum 2: an arm-4 behavioural stop was mislabelled as a provider incident

Written on 11 November 2026 at about 19:40 IST, after block N01-r2 finished and before it was graded or re-run. No arm, prompt, envelope, success rule, card or Eidos source changed.

## What happened
- **The investigation:** in N01-r2, arm 4's investigation hit a MiniMax rate limit early (error 2062, HTTP 429; receipts 3–5 of 23) and recovered. It then made 17 more successful model calls over 8 completed turns.
- **The stop:** at 19:34 IST the investigation stopped on its own rule, `needs_input: refused-proposals` ("repeated refused proposals after bounded retries"), without a conclusion.
- **The mislabel:** the arm-4 status rule (`study/eidos_arms/common.py`, `classify`) called a run a provider incident whenever *any* provider failure was recorded and the investigation had not concluded. It therefore labelled this behavioural failure an incident, and the launcher marked the whole block as an incident to be re-run.

**The rule conflicts with the charter.** The charter defines an incident as "a provider 429/5xx or quota failure that **ends** a run without a valid final result". Arms 1–3 apply exactly that rule: their final result must itself be the API error (pilot-2 addendum 1). **Re-running the block would have discarded a real arm-4 failure**, which is a re-roll in Eidos's favour and is forbidden by the charter.

## Fix
- **The rule:** a provider failure is an incident only if it is the **terminal** provider interaction, meaning the last usage receipt is the failed request. Otherwise an unconcluded investigation is a `crash`, as before.
- **Tests:** a regression test covers this case, plus a terminal-failure counterpart. A stale test that still encoded the pre-pilot-2-addendum-1 rule, and failed on the unmodified branch, now encodes the amended rule. All arm and study tests pass.
- **`study/eidos_arms/reclassify.py`** re-applies the rule to a finished block from its saved records. It keeps every previous value under `*_before_reclassification`.

## Audit of earlier pilots
- **Every arm-4 incident in pilots 1 and 2 was checked against the corrected rule.** Each one ended on a failed turn (`turn-failed`, the quota exhaustion) or mid-turn when the block was stopped.
- **Re-applied to the records**, the pilot-2 incidents D04-r2, D04-r3 and D05-r2 stay incidents.
- **No earlier result changes.**

## Charter consequence
- **N01-r2:** arm 4 becomes `crash` (its own statuses: arms 1–3 `ok`, arm 4 `crash`). The block is ordinary and is graded as it stands. Arm 4 counts as a failure; there is no re-run.
- **N02-r1 and N03-r1** had already started under the old rule. If either ends as an incident, the same re-classification is applied before any re-run decision.
- **Every later arm-4 run** uses the corrected rule at source.
