# 0013: A credential broker gives components scoped, budgeted LLM calls

Status: accepted (2026-11-09, release 1.1.0).

**Context.** The design: "Keep provider keys in the operator's secret store and issue scoped broker access to
components. A component that creates synthetic data can have its own bounded LLM calls without owning the lab's
credentials or unlimited budget" and "A run obtains a scoped, metered handle". Until 1.1.0 only the investigator
worker held provider keys (ADR 0012 keeps it that way), and a component could declare `llm` use in its manifest
but had no way to call a model.

## Threat model

- **Assets:** the lab's provider keys; the investigation's model budget; the integrity of usage records.
- **Adversary:** component code, which may be buggy or hostile (candidate code under a sealed profile). It can read
  everything in its own invocation and environment and can send arbitrary requests to anything it can reach.
- **Trusted:** the lab's command service, the launcher that writes invocations, and the investigator worker that
  holds the keys and serves the broker. Providers are trusted to report usage, not to be available.
- **Goals:** (1) a component never sees a provider key: not in its environment, invocation, files or responses;
  (2) a component can spend at most what its grant allows, and at most what the investigation's ledger still has,
  with the refusal happening **before** any provider call; (3) a grant is useless outside the invocation it was
  issued for (other job, other attempt, fenced epoch, finished job, after expiry); (4) every call leaves a receipt,
  and spend that cannot be confirmed (timeout, broker crash) is recorded as uncertain, never dropped.
- **Out of scope:** a provider that under-reports usage; prompt contents (the component chooses them; they are
  the component's scientific inputs, and responses are recorded by digest); the operator.

## Design

1. **Grant at admission.** When the bound release's manifest declares `llm` (`role`, `prompt_revision`,
   `max_calls`, `max_tokens`, optional `models`, `max_usd`) **and** the project policy enables the broker
   (`policy.llm_broker`: models allowlist, limits, prices, TTL), the resolver writes an `llm` section into the run
   lock: the model allowlist and limits are the minimum of manifest, policy and run request. Under a sealed
   profile the lock gets no `llm` section unless the run or the evidence plan sets `allow_llm_broker: true` **and** the
   admitting principal may steer (a researcher, not the investigator) or the project policy sets
   `llm_broker.allow_sealed`
   (recorded in the lock). A `max_usd` limit without prices for every allowed model is refused, as for the
   investigator.
2. **Grant per attempt.** The launcher generates a random token, sends only its SHA-256 with `issue_llm_grant`
   (fenced by attempt and epoch), and writes the token into the invocation (`Invocation.llm_grant`). The ledger
   stores the grant record: token hash, invocation identity (project, job, attempt, epoch, component release),
   models, limits, expiry. A retried attempt gets a new grant; the old one dies with its attempt.
3. **The broker** is a loopback endpoint served by the investigator worker, the only process holding provider keys:
   HTTP over a Unix domain socket (`$EIDOS_HOME/run/llm-broker.sock`). For each request it:
   (a) authenticates the token by hash and checks model, request count, token and USD limits, expiry and that the
   grant's attempt is still the job's current, non-terminal attempt (`broker_reserve_call`); (b) reserves the
   request's maximum (one token per input byte, a conservative upper bound, plus `max_tokens`, and its USD at the
   grant's prices; a scope with a `model_usd` limit refuses a model without a price, `BUDGET_UNPRICED`) against the job's budget
   scope, the investigation's ledger, which refuses when the remaining budget cannot cover it; (c) only then calls
   the provider with the lab's key; (d) settles actual usage (`broker_settle_call`), releasing the rest, and
   records the call as an `llm_call` receipt (model, provider request id, usage, USD, response digest). A request
   that would exceed the grant or the ledger is refused before any provider call.
4. **Uncertain usage.** A provider timeout or transport error after the request may have been accepted settles the
   call as uncertain: the reserved maximum is charged as possible usage with the request id, as the investigator
   does. If the broker dies between reserve and settle, the reservation stays open; when the attempt finishes
   (`finish_attempt`) or on `reconcile_llm_grant`, every open call is settled the same way (uncertain, possible
   spend = reserved) and the grant is closed.
5. **Network.** A component with a grant reaches only the broker socket: under `linux:bwrap-sealed` the socket is
   bind-mounted into the sandbox and the network namespace stays unshared; under Seatbelt only that socket path is
   allowed for outbound connections (no write access to it, so sandboxed code cannot replace it). The broker serves
   at most 32 connections at a time with a 30-second idle timeout. Without a grant nothing is mounted or allowed.
6. **Worker SDK.** `eidos_worker.llm.LlmClient` (standard library only, Python 3.9+): `llm.complete(messages,
   max_tokens, model=None)` returns text and usage, raises `LlmRefused` with the broker's code. A component gets it
   as the injected keyword `llm` or as `context.llm`; without a grant both are `None`.

## Wire format

- `Invocation.llm_grant` (optional; `eidos-wire/1`, minor addition; older workers ignore it):
  `{"grant_id", "token", "socket", "models": [..], "max_requests", "max_tokens", "max_usd"|null, "expires_at"}`.
- Broker request: `POST /v1/llm/complete`, header `Authorization: Bearer <token>`, body
  `{"messages": [{"role": "system"|"user"|"assistant", "content": str}], "max_tokens": int, "model": str|null,
  "request_key": str|null}`. `request_key` (at most 200 characters) identifies a request: a repeated key is refused with `409 REQUEST_REPLAYED`
  and the earlier call id and usage (the broker stores no response bodies), never re-sent or re-charged.
- Broker response `200`: `{"call_id", "model", "text", "usage": {"input_tokens", "output_tokens"}, "usd"}`.
  Refusals: `4xx` with `{"error": {"code", "message"}}`; codes `GRANT_INVALID`, `GRANT_EXPIRED`,
  `WRONG_INVOCATION`, `MODEL_NOT_ALLOWED`, `GRANT_EXHAUSTED`, `BUDGET_EXHAUSTED`, `BUDGET_UNPRICED`, `REQUEST_REPLAYED`, `PROVIDER_ERROR`,
  `OUTCOME_UNKNOWN`.

## Alternatives

- *Give the component a provider key with a spending cap at the provider:* rejected; keys would leave the
  investigator worker and provider-side caps are coarse, delayed and not per invocation.
- *Serve the broker from the API server:* rejected; it would have to hold provider keys too.
- *TCP loopback port:* rejected for sealed runs, which have no network namespace access at all; a Unix socket can
  be granted precisely.

## Reversal conditions

Replace the Unix socket with a per-attempt socket if several labs share one host user, or move the broker into its
own process once keys are fetched per call from the secret store rather than held by the investigator worker.
