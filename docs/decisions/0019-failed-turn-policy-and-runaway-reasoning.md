# 0019: A failed turn never stalls an investigation; bounded recovery from runaway reasoning

Status: accepted (2026-11-11, post-pilot improvement F4/I5; the running development pilot does not use it). Extends
ADR 0007 (refused proposals: bounded retries, then needs-input), ADR 0014 (structured-output repair) and ADR 0016
(reasoning models). The number 0018 is taken by the classification-accuracy ADR on a sibling branch.

## Evidence (pilot block D12-r3, arm 4, investigation `inv_8007e5614869`)

- Turns 1 to 9 ended `waiting`. In turn 10, slot 0 ended in the bounded `structured-output-invalid` outcome, which
  was refused because nothing was outstanding (ADR 0007).
- In slot 1, MiniMax-M3.1-Flash (`json` mode, `max_output_tokens` 16384) answered a request with
  `finish_reason: length`, `reasoning_tokens: 16384` and no content. Pydantic AI raises `UnexpectedModelBehavior`
  ("Model token limit (16384) exceeded before any response was generated"). `classify_failure` fell through to
  *retryable*. So Temporal's MODEL policy ran the whole decision three times, and each attempt did its read rounds
  again. Each attempt ended the same way: 26 759, 17 627 and 19 387 output tokens in 8 requests.
- The decide activity failed, and the turn closed `failed` / `error` at 04:07.
- `InvestigationWorkflow` treated `error` like a wait reason. It polled for ledger news (job, evidence or operator
  events) for up to `max_idle_turn_seconds` (6 h). Nothing was outstanding, so no news could arrive. `needs_input` is
  set only for refused proposals and coordinator limits, so it was never set. The coordinator's status said
  `waiting (error)`, but only through a Temporal query. The investigation stayed `active` with nothing scheduled
  until the study's outer deadline stopped it at 04:15 (counted as a timeout).
- Found while testing this change: the coordinator stress test (`test_workflows.py::test_stress_many_turns...`) had
  every turn failing, and it passed only because a failed turn looked like a wait. Its decide result was
  oversized: `bounded()` kept the 3 MB `reason` field whole, over Temporal's 2 MB payload limit. `bounded()` now
  truncates `reason` and `control`.

## Decision 1: failed-turn policy (no silent stall)

A turn ends `error` when an activity fails after Temporal's retries, or when the `TurnWorkflow` itself fails.
`TurnWorkflow` classifies the failure (`turn_failure`):

```json
{"activity": "decide", "error_type": "MODEL_CALL_FAILED", "retryable": true,
 "retry_state": "MAXIMUM_ATTEMPTS_REACHED", "message": "..."}
```

The failure is **non-retryable** if Temporal stopped retrying because the error was marked non-retryable
(`RetryState.NON_RETRYABLE_FAILURE`, or an `ApplicationError` with `non_retryable`). Examples: an unsupported model
spec, a provider's authentication or not-found error (`classify_failure`), a refused payload. The failure is
**retryable** if the retries ran out or the activity timed out (timeouts, connection errors, 429, 5xx). It is stored
in the turn's result and in the `turn.completed` event. The coordinator (`InvestigationWorkflow`, patch
`failed-turn-policy-v1`) then applies this policy:

| After a failed turn | Action |
|---|---|
| retryable, consecutive failed turns ≤ `MAX_FAILED_TURN_RETRIES` (2) | `record_turn_retry` → event `investigation.turn_retry_scheduled` `{failed_turn_id, attempt, max_attempts, delay_seconds, failure}`; durable backoff `failed_turn_backoff_seconds` (60) × 2^(n−1), at most 900 s (a wake, e.g. stop or new evidence, ends it early); then a normal turn |
| non-retryable | at once: `mark_needs_input` with `code: "turn-failed"` and `details` |
| retryable, but the third consecutive failure | the same `turn-failed` mark, with `automatic_retries: 2` |

A failed `TurnWorkflow` (e.g. its execution timeout) is the retryable failure `turn-workflow-failed` under the same
policy. Before, the coordinator returned `failed` after three such failures.

**Why both, and not one of them.**
- Automatic continuation alone would retry configuration and authentication errors, which never succeed. Each
  retry spends a turn and model tokens.
- Surfacing alone would stop an unattended study on a single 503.
- Two retries with backoff cover a provider blip. The visible mark covers everything else, and an operator or MCP
  client can act on it without guessing.

**Budgets.** A retry turn is an ordinary turn: `begin_turn` reserves the `turns` budget, and each decision reserves
its model-token and USD envelope. Retries are therefore counted. When a budget is spent, `begin_turn` refuses the
turn and the existing `budget-exhausted` path surfaces it.

**Visibility.** The `needs_input` mark is the existing durable investigation label plus the event
`investigation.needs_input`. It is shown by `get_investigation` (CLI, HTTP API, MCP `get_investigation`) and by the
project's `status` (`investigations_needing_input`). It now carries a machine-readable `code`:

- `refused-proposals` (ADR 0007);
- `coordinator-stopped` (a limit);
- `turn-failed`.

It also carries a bounded `details` object (at most 2000 bytes):

```json
{"code": "turn-failed", "reason": "turn failed (non-retryable error): MODEL_CALL_FAILED: ...; steer, resume or stop",
 "details": {"turn_counter": 10, "turn_id": "turn_..._10", "retryable": false, "consecutive_failed_turns": 1,
             "automatic_retries": 0, "max_automatic_retries": 2,
             "failure": {"activity": "decide", "error_type": "MODEL_CALL_FAILED", "retryable": false, "...": "..."}}}
```

The reconciler leaves a marked investigation alone, as before. While it is marked, the coordinator waits only for
operator news (steer, revise the question, resume, stop, ...); a Continue-As-New during that wait keeps waiting
(`needs_operator`). An operator action clears the mark (`investigation.input_received`) and the next turn starts
with the failure count reset.

**Replay.** Histories recorded before this change take the old branches (`workflow.patched`). The recorded
histories in `tests/fixtures/histories` replay.

## Decision 2: runaway reasoning gets one bounded re-ask in the decision

A response is a **runaway** if it has `finish_reason == "length"` and no answer: only reasoning parts, or blank text,
or nothing (`is_runaway`). A truncated answer that has some text is not a runaway: it is a malformed reply, and the
existing schema-guided re-ask handles it. Whitespace-only text at the cap carries no answer either. Pydantic AI
returns it as text rather than raising, and the runtime treats it as the same runaway.

A runaway is not a provider failure, and it is no longer raised as one. Raising it made Temporal repeat the whole
decision, reads included. Instead, within the same decision:

1. **One re-ask.** It uses the same history, without the runaway reply, which has no answer, and the same message,
   plus a concise-answer instruction ("think briefly ... reply now with ONLY the JSON decision ... short rationale;
   if you cannot decide, request_input").
2. **A smaller decision scope.** The re-ask asks for a decision only: in `json` mode the response schema has no
   `read` property, and a read request in reply is refused like a malformed reply. In `tool` mode it is one request
   without read tools over the history, the same pattern as the existing read-budget fallback.
3. **A reduced reasoning request, opt-in.** `runaway_reasoning_effort` (`minimal|low|medium|high`) is sent as the
   OpenAI-compatible `reasoning_effort` body field, on OpenAI and OpenAI-compatible endpoints only. It is off by
   default: whether MiniMax-M3.1-Flash accepts the field has not been measured (no live calls in this change), and an
   endpoint that rejects it would turn the re-ask into a failed request. An invalid value fails the decision before
   any request.
4. **The same output cap.** A smaller cap would only cut the answer shorter. The default cap was not raised.
5. **The outcome.** A second runaway, a runaway on the last request allowed by `max_requests_per_decision`, or an
   envelope refusal of the re-ask ends the decision in the explicit no-effect outcome `reasoning-runaway`
   (`bounded_outcome`, `usage.limit_reached`). Like the other bounded outcomes, it is accepted as a wait when work is
   outstanding. Otherwise it is refused as a wait with nothing to wait for, and the refused-proposal path applies
   (bounded retry turns, then `needs_input` `refused-proposals`).

**Counted and recorded, never silent.**
- Every runaway request is in `billed`, the list of every response of the decision. It counts against
  `max_requests_per_decision` and the decision's token envelope. It is in the usage receipt: `calls`,
  `output_tokens`, and `per_request` with its `finish_reason: length` and `reasoning_tokens`.
- It is also in `usage.structured_output.runaway` (request index, whether it was the re-ask, output, reasoning and
  charged tokens, provider request id) and `runaway_reasks`.
- The decision's `model_settings` record `runaway_reasoning_effort` when it is set.
- Before, the text-mode loop counted requests and the remaining envelope from the model-visible history.
- The envelope estimates the re-ask's input from the history before the runaway. The runaway reply is not sent back,
  so charging its output again would refuse re-asks that fit.
- An invalid `runaway_reasoning_effort` is refused before the first request in every mode. Then no runaway request
  is billed and left out of the usage.

## Review fixes (F5, review of f78ab41)

1. **The needs-input wait has its own cursor.** `mark_needs_input` returns `cursor`, the sequence number of its
   `investigation.needs_input` event (for an existing mark: the current event horizon, stored back in the mark).
   The coordinator passes it to `check_news` (`after`), which counts only operator events after
   `max(last turn horizon, cursor)`. The last turn's horizon alone is not enough: while turn workflows fail before
   creating a turn it does not move, so an operator event behind it ended every wait and reset the failure counter.
   An operator action taken before the mark is not taken as the answer to it; the mark is visible, so the operator
   acts after it. The cursor survives a rollover (`operator_cursor` in the carried arguments).
   F5c (review R8 #3): the same holds for the refused-proposal needs-input wait (`refused-proposals`), which had
   discarded the cursor: its operator-only `check_news` calls pass the mark's cursor, and the cursor is carried
   through a Continue-As-New during that wait to the wait that resumes it (`needs-input-survives-rollover-v1`).
   Every needs-input wait now uses its mark's cursor. Test: `tests/integration/test_f5c_refused_wait_cursor.py`
   (an operator event between the last turn's horizon and the mark, with and without a rollover).
2. **An abandoned turn is closed, not resumed, by a retry.** `record_turn_retry` and `mark_needs_input` with code
   `turn-failed` first close the investigation's last turn if it is still open (stop reason `error`,
   `failure.abandoned`), with the same settlement as `complete_turn`: open model reservations charged as uncertain
   usage, the turns reservation settled. The retry is then a new turn that reserves the turns budget. Lock order is
   the turn row, then the investigation row (as in `complete_turn`).
3. **Runaway records survive an abort.** The decision's structured-output report is kept on the runtime and attached
   to the usage of an aborted decision (stop: `DecisionAborted.possible_usage`; deadline: the `decision-deadline`
   result). Known nit: when the abort comes before the re-ask is sent, the report still counts `runaway_reasks: 1`.
4. **Runaway in the reserved tool-free request** after the read-tool budget is recorded and ends in
   `reasoning-runaway`; no request is left for a re-ask.

## Not done

- No live measurement. Whether the re-ask converges for MiniMax-M3.1-Flash, and whether its endpoint accepts
  `reasoning_effort`, are open. Both are cheap to measure with `live_provider_check.py` in a later run.
- The backoff and retry count are code constants (the base is a workflow argument). They are not investigator
  settings.
- `classify_failure` still treats an unrecognised provider failure as retryable, for example a malformed reply
  that Pydantic AI rejects. A deterministic failure of this kind takes the slow path: 3 activity attempts per turn
  and 2 retry turns, so 9 requests, before `needs_input`. That is bounded, but not immediate.
- A `TurnWorkflow` that hits its execution timeout never runs its `complete_turn` (this predates the change). The
  turn record stays open until the next `begin_turn` resumes it, and the retry event has `failed_turn_id: null`.
- A `needs_input` mark set after an operator action that happened during the failed turn stays until the next
  operator action. The refused-proposal path already behaves this way.
