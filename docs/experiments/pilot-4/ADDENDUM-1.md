# Pilot 4 Addendum 1: earlier admission window

The owner directed this change on 13 November 2026 at approximately 09:45 IST
(04:15 UTC), after registration R
`f40f03c0dd522da39f27d8ed6852a0dcb087da97` and before any Pilot 4 model call,
quota read, lab start or admission. The admission-window start moves from
**2026-11-16T00:00:00+00:00** to **2026-11-13T05:00:00+00:00**. The end remains
**2026-11-19T18:29:59+00:00**, inclusive. The containing commit is R2, recorded
externally to avoid self-reference; publication and an exact-R2 registered smoke
are required before relaunch.

The reason is to use the remaining weekly allowance now. The owner will add
pay-as-you-go credit if the weekly allowance runs out. This addendum changes
only the admission-window start: the design, cards, arms, runs, per-run envelope,
quota rule, decision rule and grader are unchanged. In particular, the current
positive-weekly-allowance admission rule still applies. A second addendum may
follow for pay-as-you-go admission; it will be registered before it affects any
block.

## Stop evidence

The retained private pilot root is
`/Volumes/Mrigesh SSD/eidos-v1/pilot-4-86dfe57/`.
`control/watchdog-events.jsonl` records watchdog exit at
2026-11-13T04:19:30.529693+00:00. `control/parent-state.json` records parent
SIGTERM at 2026-11-13T04:19:31.148218+00:00 and phase `stopped_resumable`.
The parent launch's `cleanup.json` records clean exit at
2026-11-13T04:19:57.179380+00:00, no lab and no owned workers, with caffeinate
released and the parent lock released on exit.

All 137 retained `status.log` observations from
2026-11-12T23:48:49.900668+00:00 to 2026-11-13T04:19:57.171323+00:00 show zero
quota observations, active/done blocks and resolved assignments; the only
phases are `waiting_window` and `stopped_resumable`. `STATUS.json` agrees.
`control/admissions.json` is empty; parent batches, attempts and quota history
are empty; lab is null; neither `runs/` nor `control/parent-jobs/` exists.
Both control locks are free and neither live parent nor watchdog remains.
Thus there have been zero Pilot 4 admissions, quota reads, lab starts, model
calls or hosted dispatches. Earlier registered smoke evidence is synthetic,
with no real workers, lab services or model calls.

The independently recorded private evidence inventory is
`registration/ADDENDUM1_STOP_EVIDENCE.json`, SHA-256
`401d72903980fc2aa29dae91fc5e0e55b2f5fd81e6c2b0c66568907a3fc2039f`.
All original control bytes are retained in `control/archive-pre-addendum-1/`
for a clean, zero-history R2 restart. The old R launch is refused once R2 is
active. No previously admitted block needs a transition or disposition.
