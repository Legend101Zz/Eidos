# ADR 0026: Deadline admission and runtime bounded outcomes

Status: accepted (prospective; no pilot records changed)

D3 found six valid N01-r2 read responses stranded with 221–249 seconds left because follow-up admission required the full configured 300-second request timeout. The runtime fabricated empty waits, correctly refused by the no-outstanding-work validator. Runtime inability was misclassified as model proposal behavior.

Every provider request, including native tool follow-ups, now uses the smaller of its configured timeout and the actual decision/turn time left minus a two-second durable-write margin. The activity retains its existing watchdog margin, cancellation watcher, token reservation, per-decision request cap and Temporal turn boundary. HTTP timeout and a total request timer both enforce this budget. Context reports remaining time and the usable request budget. A late slot uses the current turn boundary rather than restarting the original allowance.

Insufficient time or abandonment at that boundary returns `DecisionResult.runtime_outcome = deadline-exhausted`, with no proposal. Usage, uncertainty and admitted in-flight estimates are settled as before. A separate `runtime_outcome` record and `decision.runtime_bounded` event retain the raw response reference. Runtime bounds never become invalid model decisions or exempt empty waits from validation. Other malformed proposal refusals remain model-origin errors.

At the true exploration boundary, the coordinator immediately commits a finalization transition rather than sleeping through reproduction/export hold-back. Before the boundary, an exhausted decision gets the single bounded recovery of ADR 0027. Operator policy surfaces input without looping. All time and tokens remain inside the original envelope.

Validation: fake-provider N01-r2 fixture reproduces all six terminal read-request slots, preserving the D3 receipt shape, timeout and 249/233/221-second windows. Each read can now be followed by a decision. Margin exhaustion yields no wait; cancellation and uncertain usage retain their existing tests. This changes prospective system behavior and supplies no evidence of counterfactual pilot success.
