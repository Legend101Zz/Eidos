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
