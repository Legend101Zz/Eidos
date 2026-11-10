# 0017: Recorded normalization of re-encoded MCP arguments; stop and deadline abandon in-flight model requests

Status: accepted (2026-11-11, post-pilot improvement; the running development pilot does not use it). Extends
ADR 0016 (decision deadline) and the MCP adapter (`src/eidos/mcp`).

Two robustness gaps found while preparing the development pilot (`study/eidos_arms/SMOKE.md` on `task/dev-pilot-1`).
The study worked around both outside Eidos: `mcp_http.py` (a side MCP transport) and `provider_bridge.py` (a
per-investigation provider process killed at the deadline).

## I2. MCP tool arguments re-encoded by the client or provider

**Context.** With MiniMax-M3.1-Flash driving Claude Code, a `run_component` call reached the MCP adapter as

```json
{"config":{"argv":{"item":["python","project.py","train","--inputs","{work}","--out","{out}","--seed","{seed}"]},"seed":"11","timeout_seconds":"180"}}
```

and the lab correctly refused it (`RESOLUTION_ERROR`, 422: `config.argv: expected array, got dict`, `config.seed:
expected integer, got str`, `config.timeout_seconds: expected number, got str`). Every mangled value sits inside
`config`, which the MCP input schema declares only as `object`: a component's configuration is described by that
release's `config_schema`, not by the tool. The shapes are consistent with a tool-call encoding that turns nested
arrays into repeated `item` elements and every untyped scalar into a string, converted back to JSON without a schema.
Values the tool schema types (`seed`, `wall_seconds`) were not mangled in the evidence. Agents spent turns on the
refusals, and the study shipped a side transport.

**Options.**

- **(a) Input schemas or tool descriptions prevent it.** The tool vocabulary is stable and component-independent
  (`tools.py`: never one tool per component), so the MCP schema cannot carry each release's `config_schema` without
  a per-install `oneOf` of every configuration, which grows `tools/list` and changes whenever a component is
  published. A description cannot stop a transport from re-encoding what the model wrote. Kept only as a cheap
  complement: the `config` description now names `describe_component`'s `config_schema` and asks for exact JSON
  types. Nothing relies on it.
- **(b) Narrow, transparent normalization at the MCP boundary.** Chosen; see below.
- **(c) An alternative `arguments_json` string parameter.** Lossless in principle (a string passes through), but it
  is a second way to make every call, with precedence and conflict rules. The model has to know to use it, and after
  a refusal it is no more likely to choose it than to fix the types. JSON escaped inside a string is itself a common
  model error, and approval UIs would show an opaque string. It is what `mcp_http.py` does outside Eidos. Rejected.

**Decision (b).** `src/eidos/mcp/normalize.py`, called only by the MCP adapter's `call_tool`
(`src/eidos/mcp/gateway.py`). It undoes exactly two shapes, and only where a schema the adapter holds says what the
value must be:

| Rule | Shape | Where | Result |
|---|---|---|---|
| `item-wrapper` | an object whose only key is `item` and whose value is an array | the schema's `type` is `array` | that array (its items are then normalized against `items`) |
| `numeric-string` | a string in canonical JSON number syntax | `type` is `integer` (integer literal only: `"11"`, `"-3"`) or `number` (any finite JSON number literal: `"0.5"`, `"1e-3"`) | the number |

The schemas are the tool's own MCP input schema (top-level arguments, e.g. `seed: "7"`), and, for the `config`
argument of `plan_trial`, `run_component` and `plan_branch`, the release's `config_schema`. The adapter reads it with
`describe_component` using the caller's own token, and only when `config` contains a candidate shape. If the caller
cannot read the release, or it has no schema, nothing in `config` is normalized and the lab reports the real error.

Everything else is left for the lab to refuse:
- a wrapper around a scalar (`{"item": "python"}`) or a wrapper with other keys;
- numeric strings that are not canonical: `" 11"`, `"011"`, `"+1"`, `"1_000"`, `"0x10"`, non-ASCII digits, `"NaN"`,
  `"1e400"`, and `"1.0"` or `"1e2"` where an integer is required;
- `"true"`/`"false"` strings (not one of the observed shapes);
- values whose schema is absent, a list of types, or another type;
- the schema-less objects `payload`, `inputs`, `overrides`, `limits`, and so on.

**Recording.** Each change is listed: `{"path", "rule", "expected", "original", "normalized"}` (an `item-wrapper`
records `original` as `{"item": "<array of N>"}`; the normalized array is the payload itself). A command sends the
list as `normalized_arguments` in the HTTP command body (`CommandRequest.normalized_arguments`, at most 64 entries).
`CommandService.execute` stores it in the **command receipt** as `normalized_arguments`, so it is in the ledger's
command row and in every replay of that receipt. A query result carries the same key. With no change, nothing is
added. The idempotency hash covers the payload actually executed, not the list, so a retry that sends the corrected
types with the same `command_key` returns the original receipt.

**Validation strictness in the lab is unchanged.** The normalization exists only in `src/eidos/mcp`. The lab's
validators (`validate_config`, the resolver, decision validation, `validate_args` in the adapter itself) are not
touched. They validate the normalized arguments exactly as they validate anyone's, so a normalized call that is
still wrong is refused as before. The HTTP API, CLI, SDK and investigator paths send what they send. The lab records
`normalized_arguments` as the adapter's statement of what it changed; it is provenance, not authority, and the lab
does not act on it.

**Risk accepted.** A string in an integer field becomes the number it spells instead of a refusal. No valid call
can carry a string there, so the only change is from a refusal to the evidently intended value, and the change is
recorded.

**Tests.** `tests/unit/test_mcp_normalize.py` (both rules; ambiguous shapes untouched; the lab validator still refuses
the original), `tests/mcp/` (adapter: what is sent to the lab, receipts, queries, unreadable schemas),
`tests/integration/test_mcp_normalized_receipts.py` (the receipt and its replay in PostgreSQL; the real handler still
refuses the original config).

## I3. Stop and deadline abandon the in-flight model request

**Context.** In the smoke run arm4-02, after `stop_investigation` at the investigation's deadline, the provider
request already in flight continued, and its usage (18,769 tokens) reached the ledger 19.5 s after the deadline
(`evidence/pre-bridge-late-usage.json`). `decide` is a synchronous activity: neither a Temporal timeout nor
cancellation stops its thread, and `stop_investigation` only changes the ledger. The late decision itself was
refused (the stop changed the investigation's revision, so it was stale), but only because of that revision check.

**Decision.**

1. **One abort signal per decision** (`runtime.DecisionAbort`, passed as `DecisionRequest.abort`). The decide
   activity sets it:
   - when the investigation is no longer active (stopped, paused; a watcher thread reads the investigation record
     every `abort_poll_seconds`, default 1 s);
   - when Temporal cancels the activity (`activity.is_cancelled()`);
   - at the **decision deadline**: the activity's start plus the turn deadline (`deadline_seconds`, the activity's
     start-to-close timeout), less a margin of 10% of it, at most 15 s. The margin leaves time to record the usage
     and the outcome before Temporal gives up on the attempt.
2. **The runtime abandons the request.** Every provider request already passes the token-envelope wrapper
   (ADR 0016). That wrapper now refuses to start a request once the signal is set. While a request is in flight it
   checks the signal every 0.1 s (`ABORT_POLL_SECONDS`) and, once set, cancels the request task. The HTTP client
   closes the connection, so the provider stops streaming to us. This covers every output mode (`tool`, `json`,
   `repair`) and the tool mode's final no-tools request.
3. **Outcome.**
   - On stop, pause or cancellation the runtime raises `DecisionAborted` and nothing is proposed. The activity
     records the usage and returns `{"accepted": false, "reason": "investigation-stopped"}` (or `-paused`).
     `TurnWorkflow` ends the turn `cancelled`, and the coordinator then finds the investigation stopped. Only new
     reason values are mapped, which existing histories never contain, so replay is unaffected.
   - At the deadline the result is the explicit no-effect decision `bounded_outcome: decision-deadline`, the same
     outcome ADR 0016's text-mode guard produces. If nothing is outstanding, the lab refuses that wait like any
     other wait for nothing (ADR 0007).
4. **Usage is uncertain, which is the existing semantics.** The responses received are counted exactly (the token
   envelope keeps every response of the decision). If a request was abandoned in flight, what the provider bills for
   it is unknown. The usage is then marked `uncertain` (budget entries of kind `possible-usage` and the budget's
   `uncertain` counter, as for a timed-out request), and it records `aborted: {reason, in_flight, abandoned_request:
   {input_estimate, output_cap}}` in the `model.usage` event and the ledger's usage detail. The abandoned request's
   worst case is recorded, not charged: it is not a claim that the request was free, nor that it was billed.
5. **Reservations.** The decision's reservation is settled under its own reference against the observed usage, and
   the rest is released, so nothing stays reserved for a stopped decision (the existing settlement path; the turn's
   completion also releases anything a dead attempt left).
6. **No decision from a cancelled request is ever applied.** There are three layers:
   - the runtime never proposes anything from an aborted decision;
   - the decide activity discards a decision that completed after the signal was set (stop, pause, cancellation) and
     records only its usage;
   - the lab's `accept_decision` refuses (records it unapplied, with a `decision.unapplied` event) a decision for an
     investigation that is not active, a decision from a **superseded attempt** (a later attempt of the same slot has
     started: the earlier one was cancelled or timed out and retried), and a decision from an attempt that began
     more than the turn deadline ago (`decision-deadline-passed`).
   `reserve_model_budget` records when each attempt began (the `decision_attempt` record, lab clock), and the decide
   activity reports its attempt to `accept_decision`. A caller that does not report an attempt gets only the status
   check.

**Limits.**
- Closing the connection cannot settle the provider's bill: the usage stays uncertain, as the study recorded for its
  bridge.
- The watcher costs one ledger read per second per running decision.
- Component LLM calls through the credential broker (ADR 0013) are not covered here; they are bounded by their grant
  and request timeout.
- A pause aborts the decision as well. Its decision could no longer be accepted anyway, because the pause changes the
  investigation's revision.

**Test.** `tests/integration/test_decision_abort_slow_provider.py` runs the real decide activity and pydantic-ai runtime against
a slow fake OpenAI-compatible provider: a local HTTP server that holds each request open and records whether the
client hung up. It checks:
- `stop_investigation` mid-request: `decide` returns within 5 s, the provider saw the connection closed, never
  answered, usage is uncertain with `aborted`, the 120k reservation is settled (reserve, possible-usage and release
  under its reference; nothing reserved), and no decision exists;
- the decision deadline (4 s turn deadline): `decide` returns before 4 s, the connection is closed, usage is
  uncertain, nothing from the request is applied;
- a response that arrives in time is still accepted (control);
- a response that completes after the stop is discarded, and its usage is certain;
- the lab refusals: superseded attempt, attempt past its deadline, stopped investigation.

With the abort removed (the activity passes no signal), the stop and deadline tests fail. Unit tests:
`tests/unit/test_decision_abort.py`.

**Reversal.** If Temporal's Python SDK delivers activity cancellation into a running synchronous activity reliably,
the watcher's cancellation check could rely on it. The status and deadline triggers, and the lab-side refusals, would
stay.

## Addendum: review R2 (concurrency, exactness, replay)

An independent review of the first implementation found five gaps. Three were concurrency gaps in code paths that
predate this ADR; this ADR's guarantees depended on them.

1. **Stop and decision acceptance were not serialized.** `accept_decision` locked the turn row but read the
   investigation without a lock. A stop that committed after that read, but before the decision committed, left a
   decision accepted on a stopped investigation. The same window existed before this ADR: there, the stop's revision
   bump (`stale-revision`) was the only stop check at acceptance, and it was read without a lock too. **Fix:**
   `accept_decision` locks the turn row, then the investigation row, before any check. That is the same order as
   `complete_turn`. `stop`, `pause` and `resume` lock only the investigation row, so no cycle is possible. Either the
   stop commits first and the decision is refused (`investigation-stopped`), or the stop waits until the acceptance
   has committed.
2. **A cancellation after the provider response was missed.** The watcher only ran while `runtime.decide` ran, not
   while usage was recorded, the response uploaded and the decision accepted. Before this ADR the decide activity
   never observed cancellation at all. **Fix:**
   - The watcher now runs until `accept_decision` returns.
   - Before the upload, and again just before acceptance, the activity checks `activity.is_cancelled()` directly. The
     watcher stops at its first reason, which may be the deadline.
   - A cancelled activity raises `DECISION_ABORTED`, and a stop or pause returns its no-effect reason.

   Some window remains, because Temporal delivers cancellation to a synchronous activity only through heartbeats. The
   durable boundary covers it: the cancelled `TurnWorkflow` closes the turn (`complete_turn`, `cancelled`), and
   `accept_decision` refuses a closed turn under the same turn row lock. Suppose the acceptance commits first. The
   cancelled workflow never executes the decision. `execute_decision` also refuses to run the calls of a decision
   whose turn is no longer open.
3. **A reservation could be released twice.** `record_model_usage` read a reservation's open amount without a lock.
   A concurrent `complete_turn`, a retry's `reserve_model_budget`, or a second settlement could each read the same
   open amount, and each subtracted it. The second subtraction freed budget held by another reservation. This
   predates this ADR. **Fix:** every read-then-settle of a turn's model reservations holds that turn's row lock:
   - `reserve_model_budget` and `record_model_usage` now take the lock;
   - `complete_turn` and `accept_decision` already held it.

   `record_model_usage` refuses a `reserved` settlement whose `reservation_ref` belongs to another turn, because that
   turn's lock would not cover it.
4. **Numeric normalization could change the value.** For a `number` field, `"1e-400"` became `0.0` and
   `"9007199254740993.0"` became `9007199254740992.0`. **Fix:** a non-integer literal is converted only if the
   double denotes exactly the supplied decimal value, as it is serialized through the command path (its shortest
   round-trip decimal, `repr`): `Decimal(repr(float(s))) == Decimal(s)`. `"0.1"` and `"1e-3"` are still converted.
   Underflow, precision loss and literals with more significant digits than a double holds are passed through unchanged, and
   the lab refuses them as before. Integer literals still become exact integers.
5. **Replayed normalization was not recorded.** A replay returned the stored receipt of the call that committed. That
   receipt carried that call's `normalized_arguments`, or none. **Fix:** the returned receipt's `normalized_arguments` always
   describes the current invocation. On a replay, the committing call's record is returned separately as
   `admission_normalized_arguments`. The stored receipt is not changed, because a replay writes nothing. Each
   invocation with normalized arguments is also logged (`command arguments normalized`, with `replayed`).

**Tests.**
- `tests/integration/test_turn_concurrency.py` covers findings 1 and 3. Each test holds one command's transaction
  open on PostgreSQL and runs the other concurrently. The second command must wait, then see the first command's
  effect. Without the investigation lock, the stop-first test fails. Without the turn lock in `record_model_usage`
  and `reserve_model_budget`, the four overlap tests fail, and the other holder's budget is freed.
- `tests/unit/test_decide_cancel_through_accept.py` covers finding 2.
- `tests/unit/test_mcp_normalize.py`, `tests/mcp/` and `tests/integration/test_mcp_normalized_receipts.py` cover
  findings 4 and 5.

## Addendum: review R3 (cancellation through acceptance, uncertain usage)

1. **A cancelled attempt's acceptance could still be executed.** Temporal can cancel or time out a decide attempt
   after its last cancellation check, while its `accept_decision` request is in progress. The lab cannot see that, so
   the acceptance may commit. The retry then found the accepted decision and recovered it, and the workflow executed
   its calls. **Fix:** the attempt is invalidated durably at the acceptance boundary, in two parts.
   - Every decide attempt first calls the new `begin_decision` command. Under the turn row lock it bumps an epoch in
     the slot's `decision_attempt` record. `reserve_model_budget` and `accept_decision` carry the epoch and run under
     the same lock. A request from an earlier attempt that arrives later is refused as `superseded-attempt`.
   - An acceptance can commit before the retry begins. So `begin_decision` revokes a decision that was accepted by an
     earlier attempt of the **same activity execution** (same workflow run and activity id). Temporal retries only an
     attempt it has failed, so that attempt's result never reached the workflow, and the decision was never executed.
     Decisions are append-only (`accepted` is written once, one accepted decision per slot), so the revocation is a
     `decision_revocation` record plus a `decision.revoked` event. A revoked decision is never recovered and never
     executed: `get_turn` marks it `revoked`, `execute_decision` skips it, and it is left out of `turn_so_far`. The
     attempt returns `attempt-revoked` without a model request, and the TurnWorkflow moves to its next slot.
   - What still holds: a decision accepted by **another run** of the turn (a turn resumed after its workflow was
     terminated) is recovered as before, because that run may have executed it. A decision with recorded receipts
     is never revoked.
   - The cost: if a worker dies after an acceptance committed, the retry no longer recovers that decision. It spends
     the slot instead. Such a decision cannot be told apart from one accepted after Temporal gave up on its attempt.
   - The per-attempt command keys now include the epoch, so a resumed run's attempt 1 never replays an earlier
     run's attempt 1.
2. **An abandoned request was charged as zero.** If no response had completed, the aborted decision's reported
   usage was zero, and the settlement released the whole reservation. **Fix:** the runtime reports the abandoned
   request's admitted worst case as `in_flight_estimate`: its input estimate plus the output cap it was sent with,
   priced when a price is configured. The lab settles the known usage of completed responses as `usage`, and the
   estimate as uncertain `possible-usage`. With no per-request estimate, the rest of the reservation is charged
   instead. Known responses are no longer marked uncertain just because a later request was abandoned.
3. **Closing a turn released an in-flight reservation as zero.** `complete_turn`, or a retry's
   `reserve_model_budget`, released a still-open reservation without charging anything. If the cancelled activity
   then died, no usage was ever recorded. **Fix:** both now settle each open model reservation as provisional
   `possible-usage` of the whole open amount. The token envelope keeps a decision inside its reservation, so this is
   the conservative bound. `complete_turn` reports the amount as `uncertain_model_reservations`.
   - A later settlement of the same `reservation_ref` first reverses the outstanding provisional amount with a
     `correction` entry (used and uncertain are decremented), then books what it reports. A late settlement therefore
     never double-charges.
   - Reconciliation only touches open reservations, and the reversal only touches outstanding provisional amounts,
     so both are idempotent. Both run under the turn row lock.
   - The trade-off: until a late settlement arrives, a dead attempt costs its whole decision envelope against the
     budget. That envelope is the most it can have used.

**Tests.**
- Finding 1: `tests/integration/test_cancel_through_acceptance.py`, run against PostgreSQL:
  - The reviewer's sequence through the decide activity: the acceptance commits, then the cancellation is raised in
    the activity thread. The retry gets `attempt-revoked` and sends only `begin_decision`. The revoked decision is
    never executed (no job, no receipts), and the next slot decides afresh.
  - The reverse interleaving through the activity.
  - Both commit orders with held concurrent transactions.
  - A stale-epoch reservation is refused, a decision from another run is recovered, an executed decision is never
    revoked, and a control case.
  - Removing any one of the following makes at least one test fail: the revocation, the epoch check, the
    `execute_decision` guard, or the activity's handling of `revoked`.
  - `tests/integration/test_workflows.py::test_revoked_slot_decision_moves_the_turn_to_its_next_slot` covers the
    workflow side.
- Findings 2 and 3: `tests/integration/test_uncertain_model_usage.py` checks exact ledger amounts.
  `tests/integration/test_decision_abort_slow_provider.py` now asserts the abandoned request's uncertain amount.
  `tests/integration/test_turn_recovery.py` was updated for provisional reconciliation.
