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

## Addendum: review F5 (turn and slot authorization)

The third review of 7516e2c found three ways a decision step from an execution that had lost its authority could
still act. All three come down to one gap: the authority to decide a slot was tracked per slot (the epoch), but not
per turn, and the execution step was not tied to it.

1. **Late acceptance after termination (C1).** An old `accept_decision` committed after its workflow was terminated,
   leaving the turn open. A new workflow resumed the turn. `begin_decision` recovered the decision because it came
   from a different execution, and the decision could then be executed although it had never been delivered.
2. **Reservation refs collided across executions (C2).** A resumed turn starts again at attempt 1, so two executions
   both reserved `model:<turn>:<slot>:1`. Reconciliation and late-settlement corrections could then erase the other
   execution's uncertain charge.
3. **A delayed older `begin_decision` superseded a newer attempt (C3).** Attempt 1, delayed until after attempt 2
   began, bumped the epoch unconditionally.

### Model: one monotonic, durable authorization per turn slot

Two records, both read and written only under the **turn row lock**, which every decision step takes:

- `turn_authorization` (one per turn): `{run, generation, previous_run}`. `run` is the TurnWorkflow run that owns the
  turn. Its activities identify themselves by `activity.info().workflow_run_id`, and the decide activity's
  `execution` is `<run>/<activity id>`.
- `decision_attempt` (one per turn slot, since F2c): `{execution, attempt, epoch}`.

A third record marks the boundary between deciding and executing:

- `decision_handoff` (one per accepted decision): written by `hand_off_decision`, the first step of
  `execute_decision`, by the run that owns the turn.

**Guaranteed states and transitions**

| step | allowed when (under the turn lock) | effect |
|---|---|---|
| `begin_turn(run)`, new turn | investigation active | owner = `run`, generation 1 |
| `begin_turn(run)`, open turn, `run` ≠ owner (resume) | investigation active | owner = `run`, generation + 1; every accepted decision of an earlier run that was neither handed off nor executed is revoked (`decision.revoked`, reason `superseded-execution`); event `turn.resumed` |
| `begin_decision(execution, attempt)` | `run(execution)` = owner, and not an earlier attempt of the slot's current execution | epoch + 1; a decision accepted by an earlier attempt of the same execution, or by another run, that was not handed off is revoked; a handed-off or executed decision is recovered |
| `begin_decision`, refused | otherwise | nothing changes; `{"superseded": "superseded-execution" \| "stale-attempt"}` |
| `reserve_model_budget(execution, epoch)` | owner and current epoch | reservation under `model:<turn>:<slot>:<attempt>:e<epoch>`; other open reservations of the slot are charged as provisional uncertain usage under their own refs |
| `accept_decision(execution, epoch)` | owner, current epoch, turn open, investigation active | accepted; otherwise stored unapplied with the reason |
| `hand_off_decision(execution)` | owner, decision accepted and not revoked, turn open | the decision may be executed; idempotent |
| `complete_turn(execution)` | owner (or no run identity) | the turn closes and settles; a non-owner gets `superseded` and the turn stays open |

The run's authority never moves back: a resume always moves it to the newer run, and the epoch only moves forward,
for the owner's newest attempt. A decision is executed only if its handoff committed while its run owned the turn.
So every interleaving of a late step with a resume is ordered by the turn row lock:

- **Before the resume.** If the step was the acceptance, the resume revokes the decision unless it was handed off.
  If it was the handoff, the new run recovers the decision. It may have been executed partially, and its calls carry
  their recorded command keys.
- **After the resume.** The step is refused.

`begin_turn` now locks an open turn's row before the investigation row, the F2b lock order.

The idempotency keys of `begin_turn`, `complete_turn` and the handoff include the run (the handoff's also the
activity attempt, so a retry is re-checked), so a refused step's receipt is
never replayed to the owner. Callers that do not identify themselves (no `run_id` or `execution`: tools and older
tests) are not checked against the owner. They are checked as before, by epoch and attempt.

**C2.** The reservation ref and the returned `reservation_ref` carry the slot epoch, which is unique per
`begin_decision` across executions. The settlement paths (`record_model_usage`, reconciliation, `complete_turn`) match
exact refs, so one execution's late settlement replaces only its own provisional charge. A caller without an epoch
keeps the attempt-only ref.

**Tests.**
- `tests/integration/test_f5_turn_authorization.py`: termination during acceptance followed by a new execution, in
  both commit orders; handoff before the resume; refused completion and reservation; two executions both at
  attempt 1 with closure and a late settlement from one; the delayed older `begin_decision`.
- `tests/integration/test_cancel_through_acceptance.py`: recovery now needs a handoff, otherwise the decision is
  revoked.

**Known limitations**

- A decision handed off by the old run and recovered by the new run can be executed by both at once, if the old
  run's `execute_decision` activity was already past its handoff when the resume committed and is still running
  after its workflow was terminated. A retry of that activity is refused, since the handoff check comes first.
  Every call carries the same recorded command key, so each effect is admitted once. Fencing the running old
  activity itself would need a Temporal-level redesign, which is not done here.
- A resumed turn loses the slot of a revoked decision. The slot keeps its revoked decision (F2c: one accepted
  decision per slot), so the new run's decide returns `attempt-revoked` and the turn moves on to its next slot. A
  turn with a single model call then ends without a decision, and the coordinator starts the next turn.
- `record_decision_receipts` and `record_model_usage` are not checked against the owner. They record what already
  happened (audit, late settlement) and must be accepted from any run.
- Not run against the Temporal dev server in this session (no Temporal CLI on this machine). The workflow tests
  (`tests/integration/test_workflows.py`, `test_failed_turn_*`) were skipped, and the run identities in tests come
  from `ActivityEnvironment`.

## Addendum: review R8 (F5c: ordered turn tickets, cancellation at every effect)

The fourth review of 117741a found two gaps in the F5 model above.

1. **A delayed `begin_turn` could give the turn back to a terminated run (R8 #1).** `authorize_turn_run` treated any
   run other than the owner as newer. If run A's `begin_turn` was delayed, A was terminated and run B began or
   resumed the turn first, A's late `begin_turn` made A the owner again, and B's steps were refused as superseded.
2. **A handed-off decision could execute after a stop or turn closure (R8 #2).** `hand_off_decision` did not check the
   investigation, and it returned an existing handoff before checking the turn. Some effects (`file_claim`,
   `record_note`, `publish_component`, ...) do not check the investigation themselves, so a stop that committed after
   the handoff did not stop the decision's remaining effects.

### Turn tickets: an ordered generation for turn acquisition

The coordinator (`InvestigationWorkflow`, the only workflow that starts turn workflows, one at a time) gives every
`TurnWorkflow` it starts a **ticket**: its workflow clock in microseconds when it starts the child (patch
`turn-ticket-v1`). The ticket is durable (it is in the child's input, in the history; a replay gives the same value)
and ordered: the coordinator starts a turn workflow only after the previous one ended (completed, failed, timed out
or terminated), so its workflow clock has moved on. Within one coordinator the ticket is also never below the
previous ticket + 1 (`last_ticket`, carried through Continue-As-New), so two starts that read the same clock value
still get ordered tickets. The same holds across Continue-As-New (the new run's clock starts
after the old run's), a reset of the coordinator (the reset continues at a later time) and a coordinator the
reconciler starts again (its clock is later; terminating a coordinator also terminates its turn workflow, the
default parent-close policy).

`begin_turn` carries `{run_id, ticket}`. Under the investigation row lock (which every `begin_turn` takes, after the
open turn's row) the lab keeps the highest ticket that has begun or resumed a turn of the investigation
(`turn_ticket` record: `{ticket, run}`) and fences turn acquisition by it:

| `begin_turn(run, ticket)` | result |
|---|---|
| `ticket` > held ticket (or none held) | held ticket = `ticket`; the turn is begun or resumed as before (owner = `run`) |
| `ticket` = held ticket, same run | idempotent (a retried activity) |
| `ticket` = held ticket, another run | refused, `ticket-held-by-another-run` (only a hand reset of a turn workflow reuses its input) |
| `ticket` < held ticket | refused, `stale-ticket`: no turn begun or resumed, owner unchanged, no turns budget reserved |
| no ticket, a ticketed run already began | refused, `unticketed-run` |

A refused `begin_turn` returns `{"started": false, "reason": "superseded-run", "superseded": ...}`. Its run was
terminated, so nobody waits for it. Since ownership can only move to the run with the highest ticket, every other
step's owner check (`begin_decision`, `reserve_model_budget`, `accept_decision`, `hand_off_decision`, the effect
admission, `complete_turn`) inherits the order. Both delivery orders give the same final owner: if A's `begin_turn`
commits first and B's second, B resumes the turn and A's undelivered decisions are revoked (as in F5). If B's commits
first, A's is refused.

### Cancellation at the handoff and at every effect's admission

`hand_off_decision` now locks the turn row and then the investigation row (the order of `accept_decision`,
`complete_turn` and stop/pause/resume) and refuses, in this order: a run that does not own the turn
(`superseded-execution`), a revoked decision, a closed turn (`turn-<status>`) and an investigation that is not active
(`investigation-<status>`). Only then does it return an existing handoff or record a new one. A stop or closure that
commits first therefore always wins, also against a retry of an earlier, granted handoff.

Every effect of a decision is admitted against the decision. `execute_decision` sends each effect command with an
**admission** `{decision_id, execution}` (a command envelope field, `X-Eidos-Admission` for the artifact upload of
`author_source`/`author_component`). It is not part of the payload, so the idempotency hash and the recorded command
keys are unchanged. In the effect's own transaction, before the handler runs, the command service calls
`admit_decision_effect`, which takes the same two locks and refuses the effect with `EFFECT_NOT_ADMITTED` (nothing is
written; the key's claim rolls back) unless the decision was handed off, is not revoked, its turn is open, the
investigation is active and the execution's run still owns the turn. The locks are held until the effect commits,
so a stop either commits first (every later effect is refused) or waits until the effect has committed (and the stop
then sees, and applies its running-job policy to, any job the effect admitted). A replay of an effect that already
committed returns its receipt as before.

When an effect is refused, `execute_decision` stops: the effects before it stand, the remaining ones are not sent.
For a stop, pause, closed turn or revoked decision it records the receipts (the refused call carries
`EFFECT_NOT_ADMITTED`, under their own key per refused call) and returns `{"stopped": "<reason>"}`. A retry
that completes the decision after a resume records its full receipts under the decision's usual key. For `superseded-execution` (another run now owns the
turn) it records nothing and returns `skipped`: the new owner continues the handed-off decision with the same
command keys.

This also closes most of the first F5 limitation (a handed-off decision executed by the old and the new run at once):
once the resume commits, every further effect of the old run's still-running activity is refused at admission.

**Tests.** `tests/integration/test_f5c_ticket_and_handoff.py`: both `begin_turn` delivery orders, a delayed old
`begin_turn` after the newer run completed its turn, idempotence, unticketed and same-ticket runs; stop and pause
before the handoff, stop and closure after it (the existing handoff is not returned), the effect admission
(`file_claim`) refused after a stop with nothing written, replay of a committed effect, and a stop that commits
between two effects of one decision through `execute_decision`.

**Known limitations**

- **Histories started before `turn-ticket-v1`.** Their turn workflows carry no ticket. Among such runs the F5 rule
  remains: the last `begin_turn` to commit owns the turn. Once a ticketed run has begun a turn, an unticketed one is
  refused. This affects only coordinators that were already running when the lab was upgraded, and only until their
  current turn workflow ends.
- **The order rests on the Temporal server clock.** A ticket is the workflow time at which the coordinator started the
  child. Across a coordinator restarted by the reconciler, the order holds unless the server clock moved backwards by
  more than the time between the two starts. Two runs with the same ticket (a turn workflow reset by hand, which
  Eidos never does) are not ordered. The second is refused, which is conservative but can stall that turn until the
  coordinator starts the next one.
- **An effect admitted just before a stop runs to completion.** Admission is the boundary: a job the effect admitted
  is then subject to the stop's running-job policy (`drain` keeps it).
- **Effects sent without an admission.** Operators and tools that call effect commands directly are not decisions and
  are not checked here; the existing per-command rules apply (for example, run and trial admission refuse a
  non-active investigation).
