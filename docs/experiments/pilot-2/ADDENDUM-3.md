# Development pilot 2, addendum 3: the provider quota ran out, and a client placeholder in the model-id guard

Written on 11 November 2026 at about 15:05 IST. No arm, prompt, envelope, success rule or Eidos source changed.

## What happened
- **Provider quota.** At about 14:48 IST, MiniMax began rejecting every request: HTTP 429, "Token Plan usage limit reached … (2056)". This is the owner's MiniMax token plan's usage window, which pilot 1, the round-2 preparation and pilot 2 together used up.
  - Blocks D04-r2 and D05-r2 started during the outage. No arm received a model token, and every arm ended within about 3.5 minutes.
  - D04-r3 had run about 3.5 minutes when the quota ran out.
  - All three are **provider incidents** under the charter.
- **D03-r3.** It started during the outage and was stopped by the controller after 37 s. Its earlier attempt had already been recorded as an incident for the same reason.
- **Client placeholder.** Claude Code labels the messages it generates itself (here, the surfaced API error) with the model id `<synthetic>`. The block version guard counted `<synthetic>` as a served model, so a run with a single surfaced error message would trip the guard even if the run then finished normally. It is now excluded from the guard (`study/launcher.py`, `CLIENT_PLACEHOLDER_MODELS`). Only arms 1–3 can emit it, and real provider failures are still caught by the incident rule (addendum 1).

## Charter consequence
- **Completed and graded before the outage:** D03-r1, D03-r2, D04-r1, D05-r1, D05-r3, D06-r1, D06-r2, D06-r3 (32 runs).
- **Re-run whole when the provider quota is available again:** D03-r3, D04-r2, D04-r3, D05-r2. Following the charter, the shared model is **not** substituted. A controller loop probes the provider every 10 minutes and resumes automatically.
- **If the quota does not return before the paper deadline,** the paper reports pilot 2 as truncated by a provider outage: the 8 completed blocks, with the 4 interrupted blocks listed explicitly. The protocol requires an "unscheduled provider outage" to stay explicit; these blocks are not imputed as agent failures.
