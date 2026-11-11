# 0030 Record bounded model runtime outcomes without proposing a wait

Accepted prospectively after the settled Pilot 3 N03-r2 failure. The historical decision receipts show three token-envelope refusals interleaved with two accepted decisions, then four deadline placeholders across the next two turns. A source-backed replay confirmed that the reviewed Round 4 core still generated an empty wait when the token envelope refused a follow-up. The lab could refuse those fabricated waits, contributing to the recorded refused-proposals stop.

## Decision

The Pydantic AI adapter reports terminal deadline, request-budget, reasoning-runaway and structured-output bounds as typed runtime outcomes. It records the billed usage and response evidence already collected. The bound yields no model proposal. The existing runtime-outcome record and coordinator handle the transition to the authorized bounded recovery. An earlier accepted decision remains executed and recorded. A model-authored wait still follows ordinary decision validation.

A provider error during the reserved tool-free request retains its provider classification and possible usage. The change preserves the token envelope, per-decision request limits, recovery limits and all historical pilot records. The legacy wire fixture remains readable.

## Evidence and verification

The historical replay retains nine decision slots, 27 verified responses and the two recorded effects. The three token-envelope cases use the final input estimates recorded by the historical runtime because the replay's tool read results contain receipts in place of the original payloads. The updated runtime replay covers all slots independently. The coordinator executes the first historical effect, records the first typed request-budget bound and then enters bounded recovery. Later historical effects stay in the fixture as source evidence beyond the corrected path's divergence.

The qualification tests use fixture models to cover all supported output modes, billed usage, bounded repair and re-ask limits, genuine waits and the reserved request's provider failure.
