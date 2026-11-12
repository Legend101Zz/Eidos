# ADR 0042 — Split the conclude-only reserve

Status: Accepted for the prospective runtime.

D02 on build a7c5d19 had a registered 1800 s wall. Its conclude-only turn began with 179.4 s left.
The activity's margin and the runtime's two-second durable-write margin left about 162 s for the
decision. Two read rounds used about 10.7 s. The final request, with 49,147 estimated input tokens
and a 16,384 output cap, then waited about 151 s without returning. The runtime abandoned it at
the decision deadline. No investigator conclusion existed, so the controller abstained and the
run was recorded as a crash. The other four reviewed runtime deadline flags were ordinary phase
transitions followed by successful conclusions.

The latency review covers 2,117 completed real requests. Among 28 answered conclude-only final
requests, 57% returned within 60 s, 82% within 90 s, 89% within 120 s and 96% within 150 s; two
additional final requests never returned. Consecutive large requests had Spearman correlation
0.03. These observations motivate reserving enough time for a second draw, but do not establish
a counterfactual result for D02.

The default conclude-only reserve becomes 15% of registered wall, clamped to 90–600 s. An explicit
conclude_threshold_seconds still wins. Ordinary reproduction/export holdback stays at 15%,
clamped to 120–900 s. An 1800 s investigation therefore reserves 270 s for conclude-only work
instead of 180 s; exploration ends about 90 s earlier.

Within a conclude-only decision, let W be the time from decide start to the activity's abort
deadline. A read round can start only before clamp(0.12 * W, 10, 30) seconds have elapsed,
and the existing two-round/four-reads limit still applies. Crossing the slot forfeits remaining
rounds. A round already started can finish and its results remain available.

The fallback reserve F is clamp(0.30 * W, 45, 120) seconds. At admission of the first
decision-only request, split only if left − F − 2 ≥ 60. Its timeout is then
min(request_timeout, left − F − 2). Otherwise the existing deadline clip applies. Dynamic
instructions describe this request's usable timeout. With a D02-like new decision window of
about 253 s and 10.7 s of reads, the first terminal request gets about 164.4 s and the fallback
keeps about 75.9 s.

If that request reaches its own timeout, while the decision remains active, send one more
decision-only request over the same history with FINALIZATION_FALLBACK_NOTE: ask for the
shortest valid conclude or stop decision, brief reasoning and already shown evidence. Keep
the output cap and decision-only response schema. The optional runaway_reasoning_effort
configuration is reused; no effort setting is added otherwise. The fallback's timeout is the remaining
decision window minus two seconds, including when that exceeds the configured request timeout. Provider errors before the request's own timeout keep their
existing failure taxonomy. Stop, pause, cancellation and decision-deadline aborts never start
a fallback.

Runaway recovery and timeout fallback share one extra terminal request. Existing bounded
malformed-output correction remains available within the original request, token and time
bounds. The request admission guard counts an abandoned attempt, charges its admitted worst
case to the token envelope, and refuses a fallback that cannot fit. Returned-response token
totals and usage.calls keep their existing meanings. Abandoned requests remain separate,
uncertain in_flight_estimate usage, including after a successful fallback; two abandoned
requests are aggregated without losing either charge. The ledger settles known responses
separately from possible usage. Structured-output receipts and budget entries retain the
window, read slot, fallback reserve, terminal timeout, armed flag, timeout flag, fallback use
and outcome. The new model-facing note advances PROMPT_REVISION to investigator-prompt/5.

Only a valid investigator-authored reply can produce a conclusion. A malformed fallback uses
the existing correction rules; exhaustion returns the existing bounded outcome. If both
requests hang, the result remains deadline-exhausted, with no proposal and the existing
controller abstention/finalization failure.

A smaller output cap was rejected: valid conclusions used median 8,056 tokens, p95 10,345 and
maximum 12,830, about 77% reasoning. A 4,096 cap would truncate 84% of observed conclusions,
and 6,144 would truncate 72%. A prompt asking for brevity does not prove a smaller cap safe.
Splitting only the former roughly 162 s decision window was also rejected: it does not reduce
failure probability under the historical latency distribution. The reserve must grow first.

The cost is less exploration time and potentially another admitted terminal request, within
the same registered wall and token budget. Tests use synthetic providers and accelerated
clocks, including the real coordinator activities and PostgreSQL ledger; they establish
control flow and accounting, not improved live success rates. Independence is an assumption
supported by historical correlation, and the conclusion sample is small and censored.
There is no proof that either prospective attempt would have completed D02 successfully.
