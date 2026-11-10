# 0016: Reasoning-model support: output and time limits, reasoning tokens, read requests in the text modes

Status: accepted (2026-11-10, release 1.1.1). Refines ADR 0014 (the text output modes).

**Context.** The development pilot uses **MiniMax-M3.1-Flash**, a reasoning model, through its OpenAI-compatible
endpoint (`https://api.minimax.io/v1`). `scripts/qualify/live_provider_check.py` against it (1.1.0, receipts in the
pilot gate) found three harness problems, not model-quality problems:

1. **Output cap.** A reasoning model's reasoning tokens count against `max_tokens`. At the check's fixed 4096-token
   cap, 3 of 5 decision scenarios ended in `Model token limit (4096) exceeded before any response was generated`
   (json 5/8, tool 4/8). At 16384 (a patched local copy of the script) one request ran past the 120 s timeout.
2. **Reasoning tokens.** The provider reports `completion_tokens_details.reasoning_tokens`. Pydantic AI keeps it in
   `usage.details`; the runtime's usage receipt dropped it.
3. **Read tools in JSON mode.** With `response_format` (constrained decoding) the model never made a native tool
   call. In `read-tools-available` it put the read tool `inspect_evidence` into the decision's effect calls, which
   the lab refused ("unknown or non-effect tool"). ADR 0014 already recorded the same behaviour for qwen2.5.

**Decision.**

1. **Limits.** `live_provider_check.py --max-output-tokens N` (default 4096, unchanged) is recorded in the receipt's
   `caps`, next to `request_timeout_seconds`. Recommended investigator settings for reasoning models
   (`investigator.settings`): `max_output_tokens: 16384`, `request_timeout: 300`, `output_mode: "json"` for
   OpenAI-compatible endpoints, and `turn_deadline_seconds` (the `decide` activity's timeout, default 600) of at
   least the time the decision may take; see `docs/release/SUPPORTED.md`.
   - **Reservation check.** `decision_usd_envelope` (activities.py) prices at most
     `max_requests_per_decision × max_output_tokens` of the token envelope at the output rate. Reasoning tokens are
     inside the provider's `max_tokens`/`completion_tokens` (MiniMax stopped at exactly 4096 and 16384 output
     tokens), so this is the right cap; with 8 × 16384 = 131072 above the default 120000 envelope, every reserved
     token is priced at the output rate. The text-mode loop never makes more than `max_requests_per_decision`
     requests, reads and re-ask included, so the bound holds there too. One limit of the bound is unchanged and
     recorded: the token envelope (`max_tokens_per_decision`) is enforced after each response, so the last request
     of a decision can overshoot it by that request's own tokens; settlement always charges the actual usage.
   - **Deadline.** The `decide` activity is cancelled at the turn deadline and retried by Temporal. The text-mode
     loop now refuses to *start* a follow-up request that could outlive the deadline (elapsed + `request_timeout` >
     `deadline_seconds`) and ends with an explicit no-effect decision, `bounded_outcome: decision-deadline`.
2. **Reasoning tokens.** `usage_from_messages` records `reasoning_tokens` per request and in total, and
   `reasoning_accounting`: `included-in-output` (the OpenAI-compatible contract: `completion_tokens` includes them, so
   `output_tokens` already charges them), `added-to-output`, or `not-reported` (the total is then `null`, never a
   silent 0). A response reporting more reasoning than output tokens proves separate counting: the reasoning tokens
   are then added to that request's charged output (and USD). `investigator.settings.reasoning_tokens_billing:
   "separate"` declares a provider that always counts them separately. The ledger's usage detail carries
   `reasoning_tokens`; the component LLM broker records them the same way (`llm_broker.openai_usage`).
3. **Read requests in the text modes.** In `repair` and `json` modes the runtime owns the loop, one model request
   at a time. A reply is one of:
   - a **decision** (validated exactly as before: extraction, normalisation, the typed `Decision` model, then the
     lab's own validator at acceptance);
   - a **read request** `{"rationale": "...", "read": [{"tool": "<read tool>", "args": {...}}]}`: the runtime runs
     the allowed read tools (at most 4 per request; arguments checked against each tool's signature; a bad read is
     answered with an error, not executed), sends the bounded results back, and asks again;
   - **malformed**: at most one schema-guided re-ask, as in ADR 0014.

   Every request (read rounds and the re-ask) counts against `max_requests_per_decision`, the decision's token
   envelope and the usage receipt. A read request made in the last allowed request is not executed: the decision
   ends with `bounded_outcome: request-budget-exhausted`. Reads never become effects: a decision naming a read tool
   in `calls` is refused by the runtime (it counts as a malformed reply, so one re-ask that tells the model how to
   read) and, as before, by the lab's validator. In `json` mode the `response_format` schema admits `read` (with an
   enum of the available read tools) only when read tools are available. Each decision's
   `usage.structured_output` adds `read_requests` and `reads` (request, tool, arguments, executed, available).
   Native tool calls are no longer offered in the text modes, and ADR 0014's request held back for the re-ask is
   replaced by the rule above (the re-ask uses any request still left).

   Also: a leading `<think>...</think>` block in a reply is removed before extraction (named repair `think-block`),
   so braces in inline reasoning are not taken for the decision.

**Why this design.** The alternative the ticket offered, native tool calling for reads followed by a JSON-mode final
request, depends on the endpoint honouring tool calls without constrained decoding and then honouring
`response_format` on the history; it costs an extra request on every decision, and local OpenAI-compatible servers
handle tool calls unevenly (ADR 0014). The read request is plain JSON in the same reply schema, so it works with
constrained decoding, with the repair path, and on endpoints with no tool calling at all; and the loop, its bounds and
its accounting stay in Eidos rather than in the library. `tool` mode is unchanged.

**Measurement (2026-11-10, one run per mode, `--max-output-tokens 16384 --request-timeout 300`).**
`docs/release/evidence/g4-111-minimax-m31flash-{json,tool}.json`. Scenarios and pass criteria are unchanged.

| Mode | Before (1.1.0: 4096 / 120 s; 16384 / 120 s) | After (1.1.1, 16384 / 300 s) | Requests | Output tokens (of which reasoning) |
|---|---|---|---|---|
| `json` | 5/8; 6/8 | **8/8** | 11 | 34127 (25690) |
| `tool` | 4/8; 4/8 | 6/8 | 15 | 82150 (51141) |

- `read-tools-available` in `json` mode: three read requests (9 reads: `inspect_evidence`, `get_job`,
  `search_components`, `describe_component`), then an executable decision; 4 requests, 145 s.
  `request-limit-with-tool-loop` (budget of one request): the model asked to read in its only request; nothing was
  executed and the decision ended `request-budget-exhausted`, as designed.
- `conformance-large-catalog` passed in 62 s; in 1.1.0 at 16384 tokens it hit the 120 s timeout.
- `tool` mode still fails twice: one decision ran past 16384 output tokens (mostly reasoning), and one named a tool
  that does not exist (`catalog_query`). Those are model behaviour under the provider tool-call output, not this
  change; `json` is the recommended mode for this model.
- Each mode ran once (five decision scenarios), so a one-scenario difference is noise; the measurement does not
  separate the effect of the larger timeout from that of the read loop.

**Reversal.** If an endpoint's constrained decoding and native tool calls combine reliably, offer native reads in
`json` mode for that provider; the read-request shape stays for the others.
