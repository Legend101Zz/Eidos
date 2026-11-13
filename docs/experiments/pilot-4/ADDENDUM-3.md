# Pilot 4 Addendum 3 — exhausted weekly window under attested PAYG

Prepared prospectively on 13 November 2026 as a direct child of R3
`902efdce9c893e88449b2be0fdaf51e8101b6ade`. This draft must be registered,
published, and qualified at the exact resulting R4 before it affects any block.
Local preparation and offline tests confer no admission authority.

The owner's direction on 13 November 2026 is that pay-as-you-go applies
automatically to the same API key and the remaining block should run using those
credits. This addendum corrects the weekly exhausted-status interpretation.
It authorizes no change to the interval wait or either incident cap.

The v10.3 policy rejects weekly status 2 at
`pilot4-review-repairs/pilot4_payg.py:54–56`, before it considers attestation.
Recorded MiniMax observations show interval 53%, status 1, and weekly 0%,
status 2. They also show interval 0%, status 2 with weekly 4%, status 1;
that interval exhaustion continues to wait. The narrow projected admission
check at preparation found 23 R2 admissions, batches 1–12, and no R3 admissions.
No outcomes, candidates, grades or live run files were examined.

In v10.4, a weekly remaining percentage of exactly zero with integer status 2
(EXHAUSTED) may select `payg-attested` only with the existing valid, independently
hashed owner attestation and an active integer-status-1 interval whose percentage
is strictly positive. Weekly status 1 with zero retains the same PAYG path.
Both windows must be unexpired, all percentages finite and in [0,100], and the
observation valid, fresh (at most 60 seconds), and no later than the launch clock.
HTTP/provider errors, malformed observations, other non-1 statuses, expired
windows and invalid/absent/revoked/stale attestations refuse. Status 2 with a
positive weekly percentage refuses. Interval zero or interval status other than
1 waits. Integer status 1 with both percentages positive selects `token-plan`
without depending on an attestation. Stage, dispatch, parent admission and
capture use the same pinned policy, including decision-time and dispatch-time
attestation validity and revocation checks.

The Addendum 2 consecutive cap of two quota/billing provider incidents stays
unchanged. Under PAYG, two consecutive unrecognised provider incidents also
trigger the same pause, including mixed quota/billing and unknown incidents.
Known 2062 remains an interval incident with cooldown, exempt from the credit
cap. A NEW owner attestation and the exact orchestrator acknowledgement are
still required to clear the cap; elapsed time cannot clear it.

Capture verifies the complete R2/FREEZE.v2, R3/FREEZE.v3 and R4/FREEZE.v4 chain,
including each predecessor activation and executable closure. Each attempt
retains its actual admission freeze, helper hashes, charter commit and basis.
R2 batches 1–12 remain R2. R3 currently has no admissions; an empty R3 generation
is retained in the chain. Each switch has a distinct immutable receipt and
hashed stopped-state snapshot. The R2→R3 artifacts remain
`control/freeze-switchover.json` and `control/parent-state.v2-final.json`.
The future R3→R4 artifacts are `control/freeze-switchover.v4.json` and
`control/parent-state.v3-final.json`. The latter uses schema
`eidos-pilot4-freeze-switchover/2` with predecessor/successor freeze hashes and
last-predecessor/first-successor batch fields. It verifies all earlier admission
generations, the earlier takeover hash, switch-time order and settled attempts.
R4 captures require both receipts, including an all-R2 census and a chain with
no R3 admissions. Mixed R2/R3/R4 captures and missing-receipt refusals are
qualified synthetically. Takeover requires the exact zero-active stopped
snapshot and adopts successful blocks without admitting them again.

The future orchestrator procedure stops the identified v3 watchdog, then
requests the restart-safe v3 parent drain, waits for both owners and every
owned job/worker to settle, and exclusively writes the new snapshot/receipt
while holding both ownership locks. It advances the clean registration checkout
from exact R3 to exact R4 only after this barrier. Exact remote R4 and isolated
R4 smoke are prerequisites. The v4 parent rechecks those bindings before launch.
The prepared script preserves failure evidence and performs no automatic rollback.

All other registered charter and addendum rules remain in force: roster,
randomization, two arms, envelopes, admission window, model identity, grading,
replacement, decision rule, reporting limits and the existing attestation bytes.
No provider balance is inferred from an owner attestation. This addendum is
registered before it affects any block; push, smoke and switchover remain future
orchestrator operations.

## Prospective executable binding

| Artifact | SHA-256 |
| --- | --- |
| v10.4 executable manifest | `67f04383ab927e46daebcf385ac7996621965173964f925d52f9e50a187df737` |
| v10.4 guard | `143290259a07c349d068d02649ca5631b238b6aa7817ec46cb1dc821a955b349` |
| v10.4 PAYG policy | `a257fa4171dd97a7555c024ade13322c4f65337deefb4574c7dddff89eb60464` |
| v10.4 freeze-chain verifier | `1aed3f6ad5f444619bd10ff79a16d138e81e1a9d490a288feffc318d3af2b8bc` |
| R3 predecessor freeze | `854cb57fcde85588c394be7aad61d4ca73d11f765650481d6b605e2f1c05e2e9` |
| R3 predecessor activation | `7146f0ce70baaab34e42690c221e3b74a4e5df3bf7e92ad6b3eb7d19f78445e8` |
