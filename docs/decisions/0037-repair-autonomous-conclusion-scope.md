# 0037: Repair malformed scope within autonomous finalization

## Context

Autonomous finalization permits one coordinator decision slot. The Pydantic AI
runtime can request a bounded correction of malformed output in that slot. An
output that satisfies the base decision shape can still fail an operator's
optional conclusion-scope contract at durable acceptance. With the single slot
already used, that refusal cannot reach the investigator through the ordinary
next-slot feedback path.

## Decision

Snapshot the optional, validated operator contract into the turn specification.
The decide activity passes it separately from model context and runtime
settings. On autonomous conclude-only turns, the Pydantic AI runtime checks a
supplied structured scope on `conclude` or `stop` with the same domain validator
used for durable acceptance and completion. Omitted scope retains its existing
semantics. The durable checks remain authoritative for all runtimes.

A contract error enters the existing bounded output repair. The correction is
decision-only, including when the error arrives before the read allowance is
used. In native mode it uses the existing tool-free path or the terminal output
validator. A previously bounded one-request runaway continuation receives no
additional retry. Record the original reply and preserve the contract error in
the repair history or usage. The runtime leaves ordinary turns on their existing
durable refusal and next-slot feedback path.

The original request, token, wall, read and coordinator-slot ceilings apply.
An inadmissible or unsuccessful correction follows the existing typed bounded
outcome path. The runtime neither edits the scope nor constructs a conclusion.
The ledger still checks admissible effects, evidence and claim scope before
acceptance and completion.

## Verification

Generic text, native-tool and real coordinator/ledger tests cover the authored
correction, a continued refusal, omitted scope and non-admission. A separate
private development replay retains the historical first three responses and
eight read results, then uses an explicitly synthetic fourth response. This
exercises correction and durable acceptance without making a claim about how
the historical model would have answered.
