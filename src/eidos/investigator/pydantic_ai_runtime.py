"""Pydantic AI adapter behind the owned ``InvestigatorRuntime`` port.

Authority stays with Eidos: the agent only *proposes* a structured decision; read-only lab
queries are its only tools. One retry owner: provider SDK clients are built with
``max_retries=0`` and Temporal owns retries. Output-validation repair (``output_retries``) is a
bounded semantic action whose extra model requests are counted in usage. If this adapter fails
qualification it can be replaced by direct provider SDK calls behind the same port.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from eidos.domain.assessment import validate_claim_block
from eidos.domain.errors import Invalid
from eidos.domain.turns import FINALIZATION_READ_ROUNDS, validate_conclusion_scope
from eidos.investigator import structured_output as so
from eidos.investigator.playbook import PLAYBOOK
from eidos.investigator.runtime import (
    ABORT_POLL_SECONDS,
    DecisionAbort,
    DecisionAborted,
    DecisionRequest,
    DecisionResult,
    ModelCallFailed,
    output_cap,
    provider_failure,
)

FINALIZATION_DECISION_ONLY = ("No further reads are available. Return only your scoped conclude or stop decision, "
                              "with your own short rationale and no effect calls, using the evidence already shown.")

FINALIZATION_RUNAWAY_NOTE = (
    "The previous reply ran out of reasoning space without a usable answer. No new evidence was gathered from it. "
    "State the limits of the evidence you actually have."
)

FINALIZATION_READ_NOTE = (
    "You may use up to {rounds} read rounds, with at most four reads per round. Submit reads and decisions separately. "
    "Your final response must be a scoped conclude or stop with your own rationale and no effect calls."
)

FINALIZATION_JSON_SHAPE = (
    "Reply with one JSON object only. Use conclude or stop as control.action. Example:\n"
    '{"rationale": "<your rationale>", "calls": [], '
    '"control": {"action": "conclude", "summary": "<scoped result and limitations>", "scope": {}}}'
)

PROMPT_REVISION = "investigator-prompt/4"  # ADR 0040: finalization, claim and effect-purpose guidance
SYSTEM = (
    "You are the internal investigator of a model-research lab. Diagnose failures, propose the most "
    "informative next experiment, author components when the catalog lacks a needed method, and stop "
    "with an honest scoped conclusion. Every hypothesis needs a prediction and a falsifier. You may call "
    "read-only tools to inspect evidence; effects happen only through the calls in your final decision, "
    "which the lab validates before execution. You cannot change success rules.\n\n" + PLAYBOOK
)


# Free-form objects are encoded as JSON *strings*: providers' strict structured-output modes reject
# open dict fields (Anthropic strict mode forces {} for them), which silently emptied tool args in the
# first live qualification run. Strings round-trip exactly and are validated here.
class Call(BaseModel):
    tool: str
    args_json: str = Field(description="JSON object with this tool's arguments, e.g. "
                                       '{"component_ref": "x@1.0.0", "purpose": "reproduction", "inputs": {"train": "art_1"}, "config": {}}')

    @field_validator("args_json")
    @classmethod
    def _object(cls, v: str) -> str:
        try:
            obj = json.loads(v)
        except ValueError as exc:
            raise ValueError(f"args_json is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError("args_json must encode a JSON object")
        return v


class Control(BaseModel):
    action: Literal["wait_for_jobs", "request_input", "conclude", "stop"] | None = None
    summary: str = ""
    scope_json: str | None = Field(default=None, description="optional JSON object describing the claim scope")
    claim_json: str | None = Field(default=None, description="optional JSON object for conclude: {disposition: "
                                   "candidate|confirmed|null|abstain, claim_id?, point_estimate?, uncertainty?, "
                                   "population?, evidence_refs?, obligations?}; the state itself is derived from evidence")


class Hypothesis(BaseModel):
    statement: str
    prediction: str = ""
    falsifier: str = ""


class ResourcePolicy(BaseModel):
    max_concurrent_jobs: int = Field(default=1, ge=1, description="more than 1 only if the project's resource policy allows it")


class Decision(BaseModel):
    rationale: str
    purpose: str = "open-search"
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    control: Control | None = None
    resource_policy: ResourcePolicy | None = None


class FinalizationControl(Control):
    action: Literal["conclude", "stop"]


class FinalizationDecision(Decision):
    calls: list[Call] = Field(default_factory=list, max_length=0)
    control: FinalizationControl


def to_proposal(d: Decision, *, strict_scope: bool = False) -> dict[str, Any]:
    """Decode the typed model output into the lab's decision proposal."""
    out = d.model_dump(exclude_none=True)
    out["calls"] = [{"tool": c.tool, "args": json.loads(c.args_json)} for c in d.calls]
    if d.control is not None:
        ctl: dict[str, Any] = {"action": d.control.action, "summary": d.control.summary}
        if strict_scope and "scope_json" in d.control.model_fields_set:
            raw_scope = d.control.scope_json
            try:
                scope = json.loads(raw_scope) if raw_scope is not None else None
            except ValueError:
                scope = raw_scope
            ctl["scope"] = scope
        elif d.control.scope_json:
            try:
                scope = json.loads(d.control.scope_json)
            except ValueError:
                scope = None
            if isinstance(scope, dict):  # the published contract: a scope is an object, anything else stays as text
                ctl["scope"] = scope
            else:
                ctl["scope_text"] = d.control.scope_json
        if d.control.claim_json is not None:
            try:
                ctl["claim"] = json.loads(d.control.claim_json)
            except ValueError:
                ctl["claim"] = d.control.claim_json
        out["control"] = {k: v for k, v in ctl.items() if v is not None or (strict_scope and k == "scope")}
    return out


def no_effect_proposal(outcome: str) -> dict[str, Any]:
    """Historical wire fixture for the retired fabricated-wait representation."""
    return {"rationale": f"no usable decision ({outcome}); no effects proposed", "calls": [],
            "control": {"action": "wait_for_jobs", "summary": outcome, "bounded_outcome": outcome}}


# How the decision is obtained (ADR 0014): "tool" = the provider's tool-call output (default; what Anthropic
# qualified with); "repair" = a JSON text reply with strict extraction and at most one schema-guided re-ask;
# "json" = "repair" plus constrained decoding where the endpoint supports it (OpenAI-compatible response_format).
OUTPUT_MODES = ("tool", "repair", "json")
LENIENT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["rationale"],
    "properties": {
        "rationale": {"type": "string"}, "purpose": {"type": "string"},
        "hypotheses": {"type": "array", "items": {"type": "object", "required": ["statement"], "properties": {
            "statement": {"type": "string"}, "prediction": {"type": "string"}, "falsifier": {"type": "string"}}}},
        "calls": {"type": "array", "items": {"type": "object", "required": ["tool", "args"], "properties": {
            "tool": {"type": "string"}, "args": {"type": "object"}}}},
        "control": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["wait_for_jobs", "request_input", "conclude", "stop"]},
            "summary": {"type": "string"}, "scope": {"type": "object"}, "claim": {"type": "object"}}},
        "resource_policy": {"type": "object", "properties": {"max_concurrent_jobs": {"type": "integer", "minimum": 1}}},
    },
}

RETRYABLE_STATUS = {408, 409, 425, 429}


def classify_failure(exc: BaseException) -> bool:
    """True when a new attempt may succeed: timeouts, connection errors, 429 and 5xx. Client errors
    (not found, invalid request, authentication) are not retryable."""
    from pydantic_ai import exceptions as pe
    if isinstance(exc, (pe.UsageLimitExceeded, pe.ModelAuthenticationError, pe.ModelNotFoundError,
                        pe.ModelRequestRejectedError, pe.ModelContextWindowExceededError, pe.ContentFilterError)):
        return False
    if isinstance(exc, (pe.ModelTimeoutError, pe.ModelConnectionError, pe.ModelRateLimitError, pe.ModelServerError,
                        pe.ModelOverloadedError, pe.ModelUnavailableError, TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS or status >= 500
    return True


def model_call_failed(exc: BaseException, usage: dict[str, Any]) -> ModelCallFailed:
    """The failure of a provider request (ADR 0023): classified (quota exhaustion, transient with the provider's
    Retry-After, permanent) and recorded with the usage that may have been billed."""
    retryable = classify_failure(exc)
    failure = provider_failure(exc, retryable)
    usage["failed_request"] = {"error": type(exc).__name__, "status_code": getattr(exc, "status_code", None),
                               "kind": failure["kind"], "retry_after_seconds": failure["retry_after_seconds"]}
    return ModelCallFailed(f"{type(exc).__name__}: {exc}"[:500], retryable=retryable, possible_usage=usage,
                           failure=failure)


# ADR 0023: a reply that stays malformed after the schema-guided re-ask is not answered with an empty wait (which the lab
# refuses when nothing is outstanding). The runtime asks again from a fresh state: the original context alone, without
# the malformed replies, with a short note of what was wrong. Bounded (``fresh_state_repairs``, default 1), counted
# against the decision's requests and token envelope, and recorded in ``usage.structured_output.fresh_state``.
DEFAULT_FRESH_STATE_REPAIRS = 1
FRESH_STATE_NOTE = ("## Correction\nAn earlier reply to this context could not be used ({error}). This is a fresh "
                    "request: that reply is not shown. Reply with ONLY your decision in the required shape.")


def _reasoning_tokens(response: Any) -> int | None:
    """Reasoning tokens a provider reported for one response (OpenAI-compatible
    ``completion_tokens_details.reasoning_tokens``, which Pydantic AI keeps in ``usage.details``); None if absent."""
    v = (getattr(response.usage, "details", None) or {}).get("reasoning_tokens")
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def _charged(response: Any, reasoning_billing: str) -> tuple[int, int, int | None, bool]:
    """(input, charged output, reported reasoning, reasoning added to output) of one response; see
    ``usage_from_messages``."""
    r_in, r_out, r_reason = response.usage.input_tokens or 0, response.usage.output_tokens or 0, \
        _reasoning_tokens(response)
    added = r_reason is not None and (reasoning_billing == "separate" or r_reason > r_out)
    return r_in, r_out + (r_reason if added and r_reason else 0), r_reason, added


class EnvelopeRefused(Exception):
    """A model request was not started: its worst case does not fit what is left of the decision's token envelope."""


# Chat-template tokens per message part (role markers, separators, tool-call framing) that are not in the part's own
# bytes: a generous allowance, so the input estimate stays an upper bound.
PART_OVERHEAD_TOKENS = 32


def _nbytes(x: Any) -> int:
    return len(json.dumps(x, default=str, ensure_ascii=False).encode())


class TokenEnvelope:
    """Per-decision admission (ADR 0016): a model request is started only if the tokens already charged plus its worst
    case, ``input estimate + output cap``, fit the decision's token envelope (``max_tokens_per_decision``, which the
    decide activity reserves). Every request of the decision passes here, in every output mode.

    Input estimate. After a response whose input the provider reported: that request's input plus its charged output
    (the text sent back is part of it) plus the bytes of every message part added since, with
    ``PART_OVERHEAD_TOKENS`` per part. Otherwise (the first request, or no reported input): the bytes of the whole
    request: messages, instructions, tool definitions and settings (the response schema). One token per byte is the
    lab's conservative rule (the LLM broker reserves the same way): no tokenizer produces more tokens than bytes in
    practice.

    The output cap bounds every billed output token when reasoning is counted inside the output (``included``, the
    OpenAI-compatible contract). A provider configured as ``separate`` may bill reasoning beyond the cap; that request
    is then charged in full and no further request starts once the envelope is spent."""

    def __init__(self, limit: int, cap: int, reasoning_billing: str = "included"):
        self.limit, self.cap, self.reasoning_billing = limit, cap, reasoning_billing
        self.spent = 0
        self.last: tuple[int, int] | None = None  # (input, charged output) of the last response with reported input
        self.admitted: list[dict[str, int]] = []
        self.responses: list[Any] = []  # every response of the decision, in order: what a stopped decision has billed

    def estimate(self, messages: list, settings: Any, params: Any) -> int:
        from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse
        last = max((i for i, m in enumerate(messages) if isinstance(m, ModelResponse)), default=-1)
        if self.last is not None and last >= 0:
            new = messages[last + 1:]
            parts = sum(len(getattr(m, "parts", ())) for m in new)
            return self.last[0] + self.last[1] + len(ModelMessagesTypeAdapter.dump_json(new)) \
                + PART_OVERHEAD_TOKENS * parts
        tools = [{"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
                 for t in [*getattr(params, "function_tools", []), *getattr(params, "output_tools", [])]]
        parts = sum(len(getattr(m, "parts", ())) for m in messages) + len(tools)
        return len(ModelMessagesTypeAdapter.dump_json(messages)) + _nbytes(tools) + _nbytes(settings or {}) \
            + PART_OVERHEAD_TOKENS * parts

    def admit(self, messages: list, settings: Any, params: Any) -> None:
        est = self.estimate(messages, settings, params)
        if self.spent + est + self.cap > self.limit:
            raise EnvelopeRefused(f"the next request may use {est} input + {self.cap} output tokens; the decision's "
                                  f"token envelope has {max(self.limit - self.spent, 0)} of {self.limit} left")
        self.admitted.append({"input_estimate": est, "output_cap": self.cap, "spent_before": self.spent})

    def record(self, response: Any) -> None:
        r_in, r_out, _, _ = _charged(response, self.reasoning_billing)
        self.responses.append(response)
        self.spent += r_in + r_out
        if is_runaway(response):
            # A runaway reply is not sent back (ADR 0019): the next request's input is the history before it, so the
            # estimate keeps counting from the last response that stays in the history (or from the request bytes).
            return
        self.last = (r_in, r_out) if r_in > 0 else None


async def _abortable(request: Any, abort: DecisionAbort, abandoned: dict[str, Any] | None) -> Any:
    """Await the provider request ``request`` unless ``abort`` is set first: then cancel it (the HTTP client closes the
    connection, so the provider stops streaming to us; what it bills for the abandoned request is unknown) and raise
    ``DecisionAborted``. A response that completed is always returned, even if ``abort`` was set meanwhile: the caller
    decides what an aborted decision may still use (nothing, ADR 0017)."""
    import asyncio
    task = asyncio.ensure_future(request)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=ABORT_POLL_SECONDS)
            reason = None if task.done() else abort.poll()
            if reason:
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - CancelledError or the transport's own error: abandoned either way
                    pass
                raise DecisionAborted(reason, in_flight=True, abandoned=abandoned)
    except asyncio.CancelledError:
        task.cancel()
        raise
    return task.result()


def in_flight_estimate(abandoned: dict[str, Any], price: dict[str, float] | None) -> dict[str, Any]:
    """A conservative bill for a request abandoned in flight (F2c): the worst case it was admitted with, its input
    estimate plus the output cap it was sent with, priced like reported usage. The provider may bill any part of it."""
    inp, out = int(abandoned.get("input_estimate") or 0), int(abandoned.get("output_cap") or 0)
    est: dict[str, Any] = {"input_tokens": inp, "output_tokens": out, "basis": "admitted-worst-case"}
    if price:
        est["usd"] = (inp * price["input"] + out * price["output"]) / 1e6
    return est


DURABLE_WRITE_MARGIN_SECONDS = 2.0


def _guarded(model: Any, envelope: TokenEnvelope, abort: DecisionAbort | None = None,
             request_timeout: float = 120.0) -> Any:
    """``model`` with ``envelope`` checked before, and charged after, every request; with ``abort``, no request starts
    once it is set and an in-flight request is abandoned when it is set (ADR 0017)."""
    from pydantic_ai.models.wrapper import WrapperModel

    class EnvelopeModel(WrapperModel):
        async def request(self, messages, model_settings, model_request_parameters):
            reason = abort.poll() if abort is not None else None
            if reason:
                raise DecisionAborted(reason)
            # ADR 0026: admit against the actual boundary, not a full configured timeout.
            settings = dict(model_settings or {})
            usable = request_timeout
            if abort is not None and abort.deadline is not None:
                usable = min(usable, abort.deadline - abort.clock() - DURABLE_WRITE_MARGIN_SECONDS)
                if usable <= 0:
                    raise DecisionAborted("decision-deadline")
            settings["timeout"] = usable
            model_settings = settings
            envelope.admit(messages, model_settings, model_request_parameters)
            call = super().request(messages, model_settings, model_request_parameters)
            import asyncio
            pending = _abortable(call, abort, envelope.admitted[-1]) if abort is not None else call
            sent_at = time.monotonic()
            try:
                response = await asyncio.wait_for(pending, timeout=usable)
            except Exception as exc:
                import httpx
                timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException)) or type(exc).__name__ == "APITimeoutError"
                if timed_out and usable < request_timeout and time.monotonic() - sent_at >= usable - 0.01:
                    raise DecisionAborted("decision-deadline", in_flight=True,
                                          abandoned=envelope.admitted[-1]) from None
                raise
            envelope.record(response)
            return response
    return EnvelopeModel(model)


def usage_from_messages(messages: list, provider: str, model: str | None,
                        price: dict[str, float] | None = None, reasoning_billing: str = "included") -> dict[str, Any]:
    """Usage of every model response in ``messages``; the basis of usage receipts and budget settlement.

    Reasoning tokens are recorded per request and in total whenever the provider reports them, never dropped.
    OpenAI-compatible APIs count them inside ``completion_tokens`` (``reasoning_billing="included"``, the default),
    so ``output_tokens`` already charges them. A response reporting more reasoning tokens than output tokens proves
    the provider counts them separately: they are then added to that request's charged output, and recorded as
    added. ``reasoning_billing="separate"`` declares a provider that always reports them separately."""
    from pydantic_ai.messages import ModelResponse
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    per_request, inp, out, reasoning, reported, added = [], 0, 0, 0, False, False
    for r in responses:
        r_in, r_out, r_reason, r_added = _charged(r, reasoning_billing)
        entry: dict[str, Any] = {"model": r.model_name, "input_tokens": r.usage.input_tokens,
                                 "output_tokens": r.usage.output_tokens, "finish_reason": r.finish_reason}
        if r_reason is not None:
            reported = True
            reasoning += r_reason
            entry["reasoning_tokens"] = r_reason
            if r_added:
                added = True
                entry["reasoning_added_to_output"] = True
        inp, out = inp + r_in, out + r_out
        per_request.append(entry)
    usage: dict[str, Any] = {
        "provider": provider, "model": model, "input_tokens": inp, "output_tokens": out, "calls": len(responses),
        "request_ids": [r.provider_response_id for r in responses if r.provider_response_id],
        "served_models": sorted({r.model_name for r in responses if r.model_name}), "per_request": per_request,
        "reasoning_tokens": reasoning if reported else None,
        "reasoning_accounting": ("added-to-output" if added else "included-in-output") if reported else "not-reported"}
    if price:
        usage["usd"] = (inp * price["input"] + out * price["output"]) / 1e6
    return usage


def _read(tools: dict[str, Any], name: str, *a: Any) -> Any:
    """Run one read tool: a read failure is information for the model, never a crash of the decision."""
    try:
        out = tools[name](*a)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:300],
                "note": "No further evidence is available for this request; decide with what you have."}
    if isinstance(out, dict) and out.get("available") is False:
        out = {**out, "note": "No further evidence is available for this request; do not re-query it."}
    return _bounded(out)


def _bounded(x: Any, limit: int = 8000) -> Any:
    s = json.dumps(x, default=str)
    return x if len(s) <= limit else {"truncated": True, "preview": s[:limit]}


# Runaway reasoning (ADR 0019): a reasoning model may spend a request's whole output cap on reasoning and return no
# answer (``finish_reason=length``, only reasoning or blank text; MiniMax-M3.1-Flash in pilot D12-r3, 16384 of 16384
# output tokens). That is not a provider failure: retrying the whole decision repeats it from scratch. The runtime
# re-asks once within the decision, concisely and with a smaller scope; a second runaway ends the decision in the
# bounded no-effect outcome ``reasoning-runaway``. Every runaway request is counted and recorded.
RUNAWAY_NOTE = ("Your previous reply to this message used its whole output limit ({cap} tokens) on reasoning and "
                "contained no answer. Think briefly. Do not request further reads: reply now with ONLY the JSON "
                "decision object, with a short rationale. If you cannot decide yet, reply with a decision whose "
                "control.action is request_input (or wait_for_jobs if jobs are outstanding) and a one-line summary.")
RUNAWAY_TOOL_NOTE = ("Your previous reply used its whole output limit ({cap} tokens) on reasoning and contained no "
                     "answer. No further reads are available. Think briefly and return your decision now, with a "
                     "short rationale; if you cannot decide yet, use control.action request_input.")
REASONING_EFFORTS = ("minimal", "low", "medium", "high")


def is_runaway(response: Any) -> bool:
    """A response that hit the output cap (``finish_reason == "length"``) before giving any answer: no part other than
    reasoning (``ThinkingPart``) or blank text. A truncated answer (some text) is not a runaway: it is a malformed
    reply, handled by the schema-guided re-ask."""
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
    if not isinstance(response, ModelResponse) or response.finish_reason != "length":
        return False
    return all(isinstance(p, ThinkingPart) or (isinstance(p, TextPart) and not (p.content or "").strip())
               for p in response.parts)


def runaway_record(response: Any, request: int, reask: bool, reasoning_billing: str = "included") -> dict[str, Any]:
    """What is recorded for one runaway request (in ``usage.structured_output.runaway``)."""
    _, charged, reasoning, _ = _charged(response, reasoning_billing)
    return {"request": request, "reask": reask, "finish_reason": response.finish_reason,
            "output_tokens": response.usage.output_tokens, "reasoning_tokens": reasoning, "charged_output": charged,
            "request_id": response.provider_response_id}


def build_model(cfg: dict[str, Any]):
    """Construct a provider model with transport retries disabled."""
    spec = cfg.get("model") or ""
    timeout = float(cfg.get("request_timeout", 120))
    if cfg.get("_model_object") is not None:
        return cfg["_model_object"]
    if spec == "test":
        from pydantic_ai.models.test import TestModel
        return TestModel()
    provider, _, name = spec.partition(":")
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        anthropic_client = AsyncAnthropic(api_key=os.environ.get(cfg.get("api_key_env", "ANTHROPIC_API_KEY")),
                                          max_retries=0, timeout=timeout)
        return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=anthropic_client))
    if provider in ("openai", "openai-compatible"):
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        openai_client = AsyncOpenAI(api_key=os.environ.get(cfg.get("api_key_env", "OPENAI_API_KEY"), "unused"),
                                    base_url=cfg.get("base_url"), max_retries=0, timeout=timeout)
        return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=openai_client))
    raise ModelCallFailed(f"unsupported model spec {spec!r}", retryable=False)


class PydanticAIRuntime:
    name = "pydantic-ai"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.envelope: TokenEnvelope | None = None  # set per decision; every request of the decision passes it
        self.abort: DecisionAbort | None = None  # the decision's abort signal (stop, pause, cancellation, deadline)
        # The decision's structured-output report so far (repairs, re-asks, reads, runaways); kept on an aborted
        # decision too (F5, A3), so a runaway that arrived before a stop or deadline is never discarded.
        self._report: dict[str, Any] | None = None
        self._native_error_response: Any = None

    def _model(self) -> Any:
        model = build_model(self.config)
        return _guarded(model, self.envelope, self.abort, float(self.config.get("request_timeout", 120))) \
            if self.envelope is not None else model

    def _time_note(self) -> str:
        """Dynamic instructions, recomputed for every provider request, including native tool rounds."""
        left = None if self.abort is None or self.abort.deadline is None else max(
            0.0, self.abort.deadline - self.abort.clock())
        usable = float(self.config.get("request_timeout", 120))
        if left is not None:
            usable = max(0.0, min(usable, left - DURABLE_WRITE_MARGIN_SECONDS))
        return f"Runtime time left: {left} seconds; usable request budget: {usable:.3f} seconds."

    def _agent(self, req: DecisionRequest, *, with_tools: bool = True, output_type: Any = None,
               instructions: str | None = None, terminal: bool = False, output_retries: int | None = None):
        from pydantic_ai import Agent, ModelRetry, RunContext, Tool
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        tools = req.read_tools

        def call(ctx, name: str, *a: Any) -> Any:
            if req.conclude_only:
                response = next((m for m in reversed(ctx.messages) if isinstance(m, ModelResponse)), None)
                if response:
                    calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
                    if any(part.tool_name not in tools for part in calls):
                        return {"available": False, "error": "read batch refused: submit reads or a conclusion separately"}
                    if len(calls) > so.MAX_READS_PER_REQUEST:
                        return {"available": False, "error": "read batch refused: at most four reads per round"}
            return _read(tools, name, *a)

        def inspect_evidence(ctx, artifact_id: str, max_bytes: int = 8192) -> Any:
            """Bounded preview of an artifact's content (text/JSON/CSV)."""
            return call(ctx, "inspect_evidence", artifact_id, min(max_bytes, 16384))

        def search_components(ctx, text: str = "", capability: str | None = None) -> Any:
            """Search the installed component catalog (catalog entries are data, not tools)."""
            return call(ctx, "search_components", text, capability)

        def describe_component(ctx, component_ref: str) -> Any:
            """Manifest, qualification and approvals of one component release."""
            return call(ctx, "describe_component", component_ref)

        def get_job(ctx, job_id: str) -> Any:
            """Status, receipts and outputs of one job."""
            return call(ctx, "get_job", job_id)

        def current_instructions() -> str:
            return (instructions or SYSTEM) + "\n\n" + self._time_note()

        if output_retries is None:
            output_retries = 0 if req.conclude_only and with_tools else int(self.config.get("output_retries", 1))
        agent = Agent(self._model(), output_type=output_type or (FinalizationDecision if req.conclude_only else Decision),
                      instructions=current_instructions, retries=output_retries,
                      tools=[Tool(t, takes_ctx=True)
                             for t in (inspect_evidence, search_components, describe_component, get_job)
                             if t.__name__ in tools] if (tools and with_tools) else [])
        if terminal:
            @agent.output_validator
            def conclude(ctx: RunContext[object], output: Decision) -> Decision:
                proposal = to_proposal(output, strict_scope=req.conclusion_scope_schema is not None)
                try:
                    _validate_native_finalization(req, proposal, ctx.messages)
                except Invalid as exc:
                    report: dict[str, Any] = self._report if self._report is not None else {"mode": "tool", "errors": []}
                    report.setdefault("errors", []).append(so.validation_message(exc))
                    report["last_error_code"] = exc.details.get("cause")
                    self._report = report
                    self._native_error_response = next(
                        (message for message in reversed(ctx.messages) if isinstance(message, ModelResponse)), None)
                    raise ModelRetry(so.validation_message(exc)) from exc
                return output
        return agent

    def _terminal_requests(self, remaining: int) -> int:
        return min(remaining, 1 + int(int(self.config.get("output_retries", 1)) > 0))

    def decide(self, req: DecisionRequest) -> DecisionResult:
        mode = self.config.get("output_mode", "tool")
        if mode not in OUTPUT_MODES:
            raise ModelCallFailed(f"unknown output_mode {mode!r} (one of {OUTPUT_MODES})", retryable=False)
        self.envelope = TokenEnvelope(int(self.config.get("max_tokens_per_decision", 120_000)), output_cap(self.config),
                                      self._billing())
        self.abort = req.abort
        seconds = float(req.limits.get("deadline_seconds") or 0)
        if self.abort is None and seconds > 0:
            self.abort = DecisionAbort(time.monotonic() + seconds)
        elif self.abort is not None and seconds > 0:
            bound = self.abort.clock() + seconds
            self.abort.deadline = min(self.abort.deadline, bound) if self.abort.deadline is not None else bound
        self._report = None
        self._native_error_response = None
        try:
            if mode != "tool":
                return self._decide_text(req, mode)
            return self._decide_tool(req)
        except DecisionAborted as exc:
            return self._aborted(exc)

    def _aborted(self, exc: DecisionAborted) -> DecisionResult:
        """A decision whose abort signal was set (ADR 0017). Nothing from it is ever proposed: the usage is that of
        every response received (uncertain when a request was abandoned in flight: its bill is unknown, and the
        abandoned request's admitted worst case is recorded with it). At the decision deadline the result is the
        runtime outcome ``deadline-exhausted`` with no proposal; on stop, pause or cancellation ``DecisionAborted`` is raised
        with that usage, and the decide activity records it without proposing anything."""
        assert self.envelope is not None
        max_requests = int(self.config.get("max_requests_per_decision", 8))
        responses = list(self.envelope.responses)
        usage = self._usage(responses)
        if self._report is not None:  # F5 (A3): e.g. the runaway whose re-ask the stop or deadline prevented
            usage["structured_output"] = {**self._report, **({"repairs": sorted(set(self._report["repairs"]))}
                                                             if "repairs" in self._report else {})}
        usage["aborted"] = {"reason": exc.reason, "in_flight": exc.in_flight,
                            **({"abandoned_request": exc.abandoned} if exc.abandoned else {})}
        if exc.in_flight:
            usage["uncertain"] = True
            if exc.abandoned:  # F2c: settled as uncertain usage at this conservative estimate, never as zero
                usage["in_flight_estimate"] = in_flight_estimate(exc.abandoned, self.config.get("price_per_mtok"))
        if exc.reason != "decision-deadline":
            raise DecisionAborted(exc.reason, in_flight=exc.in_flight, abandoned=exc.abandoned,
                                  possible_usage=usage) from exc
        usage["limit_reached"] = ("decision deadline: the in-flight request was abandoned" if exc.in_flight
                                  else "decision deadline: no further request was started")
        usage["runtime_outcome"] = "deadline-exhausted"
        return DecisionResult({}, usage, json.loads(_messages_json(responses)),
                              self._settings(usage, max_requests), PROMPT_REVISION, "deadline-exhausted")

    # -- text output with bounded repair (ADR 0014) and an Eidos-owned read loop (ADR 0016) -----------------------
    def _json_mode_settings(self, reads: list[str] | tuple[str, ...] = (), *, conclude_only: bool = False) -> dict[str, Any] | None:
        """Constrained decoding where the endpoint supports it: OpenAI-compatible ``response_format``
        (Ollama maps it onto its ``format``). Anthropic has no JSON mode: the repair path alone applies.
        With read tools available the schema also admits the read-request shape (``read``)."""
        if self._provider() not in ("openai", "openai-compatible"):
            return None
        schema = LENIENT_DECISION_SCHEMA
        if conclude_only:
            props = schema["properties"]
            control = props["control"]
            schema = {**schema, "required": ["rationale", "control"], "properties": {
                **props, "calls": {**props["calls"], "maxItems": 0},
                "control": {**control, "required": ["action"], "properties": {
                    **control["properties"], "action": {"type": "string", "enum": ["conclude", "stop"]},
                }},
            }}
        if reads:
            schema = {**schema, "properties": {**schema["properties"], "read": {
                "type": "array", "maxItems": so.MAX_READS_PER_REQUEST,
                "items": {"type": "object", "required": ["tool", "args"], "properties": {
                    "tool": {"type": "string", "enum": list(reads)}, "args": {"type": "object"}}}}}}
        if conclude_only:
            if reads:
                schema = {**schema, "required": ["rationale"], "oneOf": [
                    {"required": ["read"], "not": {"anyOf": [{"required": ["calls"]}, {"required": ["control"]}]}},
                    {"required": ["control"], "not": {"required": ["read"]}},
                ]}
            else:
                schema = {**schema, "not": {"required": ["read"]}}
        return {"response_format": {"type": "json_schema", "json_schema": {
            "name": "eidos_decision", "strict": False, "schema": schema}}}

    def _runaway_effort(self) -> str | None:
        """The reduced reasoning request sent with a runaway re-ask: ``runaway_reasoning_effort`` (opt-in; one of
        ``REASONING_EFFORTS``), as the OpenAI-compatible ``reasoning_effort`` body field. Only OpenAI and
        OpenAI-compatible endpoints get it, and only when configured: an endpoint that rejects the field would turn
        the re-ask into a failed request."""
        effort = self.config.get("runaway_reasoning_effort")
        if not effort or self._provider() not in ("openai", "openai-compatible"):
            return None
        if effort not in REASONING_EFFORTS:
            raise ModelCallFailed(f"runaway_reasoning_effort must be one of {REASONING_EFFORTS}", retryable=False)
        return str(effort)

    def _runaway_settings(self, settings: dict[str, Any], mode: str, *, conclude_only: bool = False) -> dict[str, Any]:
        """Settings of a runaway re-ask: the same output cap (a smaller one would only cut the answer shorter), the
        decision-only response schema in JSON mode (no read request), and the reduced reasoning request if configured."""
        out = dict(settings)
        body = dict(self._json_mode_settings(conclude_only=conclude_only) or {}) if mode == "json" else {}
        effort = self._runaway_effort()
        if effort:
            body["reasoning_effort"] = effort
        if body:
            out["extra_body"] = body
        else:
            out.pop("extra_body", None)
        return out

    def _decide_text(self, req: DecisionRequest, mode: str) -> DecisionResult:
        """One model request at a time, each counted against ``max_requests_per_decision`` and the decision's
        token envelope. A reply is a decision, a read request (executed here, results sent back) or malformed
        (at most one schema-guided re-ask). Native tool calls are not offered in these modes: constrained decoding
        and native tool calls conflict on OpenAI-compatible endpoints (G4 1.1.1: MiniMax-M3.1-Flash in JSON mode
        never called a tool and put the read tool into its effect calls instead)."""
        from pydantic import ValidationError
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
        from pydantic_ai.messages import ModelResponse
        from pydantic_ai.usage import UsageLimits

        self._runaway_effort()  # a misconfigured effort fails before any request
        max_requests = int(self.config.get("max_requests_per_decision", 8))
        max_tokens = int(self.config.get("max_tokens_per_decision", 120_000))
        model = self.config.get("model")
        reads_allowed = [t for t in so.READ_TOOLS if t in (req.read_tools or {})]
        read_round_limit = min(FINALIZATION_READ_ROUNDS, max_requests - 1) if req.conclude_only else None
        terminal = read_round_limit is not None and (read_round_limit <= 0 or not reads_allowed)
        if read_round_limit is not None:
            report_limit = {"finalization_read_round_limit": max(0, read_round_limit) if reads_allowed else 0,
                            "read_rounds_forfeited": False}
        else:
            report_limit = {}
        settings: dict[str, Any] = {"max_tokens": output_cap(self.config)}  # the cap the envelope admits with
        extra = self._json_mode_settings(reads_allowed if not terminal else (), conclude_only=req.conclude_only) \
            if mode == "json" else None
        if extra:
            settings["extra_body"] = extra
        report: dict[str, Any] = {"mode": mode, "json_mode": bool(extra), "repairs": [], "reasks": 0,
                                  "errors": [], "valid": False, "read_requests": 0, "reads": [], "runaway": [],
                                  "runaway_reasks": 0, "fresh_state_repairs": 0, "fresh_state": [], **report_limit}
        fresh_left = int(self.config.get("fresh_state_repairs", DEFAULT_FRESH_STATE_REPAIRS))
        self._report = report
        shape = FINALIZATION_JSON_SHAPE if req.conclude_only else so.JSON_SHAPE
        if reads_allowed and not terminal:
            shape += "\n\n" + so.read_shape(reads_allowed)
        instructions = SYSTEM + "\n\n" + shape
        agent = self._agent(req, with_tools=False, output_type=str, instructions=instructions)
        message = (req.context + "\n\n## This turn so far\n" + json.dumps(req.turn_so_far, default=str)[:20000]
                   + f"\n\n## Decision budget\nYou may make at most {max_requests} model requests for this decision, "
                   "including read requests and one possible correction of a malformed reply.\n\n" + shape)
        if read_round_limit and not terminal:
            message += "\n\n" + FINALIZATION_READ_NOTE.format(rounds=read_round_limit)
        fresh_message = message  # ADR 0023: the original request, for a fresh-state repair
        terminal_agent = None
        terminal_settings: dict[str, Any] = {"max_tokens": output_cap(self.config)}
        if mode == "json" and (terminal_schema := self._json_mode_settings(conclude_only=True)):
            terminal_settings["extra_body"] = terminal_schema

        def parse(text: str, decision_only: bool = False) -> tuple[list[Any] | None, dict[str, Any] | None]:
            obj, repairs = so.extract_json(text)
            reads, obj, read_repairs = so.split_read_request(obj, [] if decision_only else reads_allowed)
            report["repairs"] += repairs + read_repairs
            if reads is not None:
                return reads, None
            obj, shape_repairs = so.normalize(obj, strict_scope=req.conclusion_scope_schema is not None)
            report["repairs"] += shape_repairs
            proposal = to_proposal(Decision.model_validate(obj), strict_scope=req.conclusion_scope_schema is not None)
            misuse = so.read_tools_in_calls(proposal["calls"], reads_allowed)
            if misuse:  # never accepted as effects: refused here (one re-ask) and by the lab's validator
                raise so.StructuredOutputError(misuse)
            if req.conclude_only:
                _validate_finalization(req, proposal)
            return None, proposal

        def runaway(response: Any, was_reask: bool) -> DecisionResult | None:
            """Record a runaway reply; the bounded outcome if no re-ask is left, else None (re-ask once)."""
            report["runaway"].append(runaway_record(response, len(billed), was_reask, self._billing()))
            if req.conclude_only:
                report["evidence_limited"] = True
                if not terminal and report["read_requests"] < report["finalization_read_round_limit"]:
                    report["read_rounds_forfeited"] = True
            if terminal:
                return self._bounded(archived + history, model, max_requests,
                                     "reasoning runaway on the final decision-only request",
                                     "reasoning-runaway", report, billed)
            if was_reask:  # the concise re-ask ran away too: a bounded outcome, not a retried failure
                return self._bounded(archived + history, model, max_requests, f"reasoning runaway: output cap "
                                     f"{output_cap(self.config)} reached twice without an answer",
                                     "reasoning-runaway", report, billed)
            if len(billed) >= max_requests:
                return self._bounded(archived + history, model, max_requests, f"reasoning runaway in the last of "
                                     f"{max_requests} requests", "reasoning-runaway", report, billed)
            return None

        history: list = []
        # A fresh prompt intentionally starts without previous replies. Preserve the
        # request/response pairs separately for the audit, including when that request cannot fit.
        archived: list = []
        # Every response of this decision, in order: what it billed. ``history`` (what the model sees next) leaves out
        # a runaway reply, which carries no answer; its tokens and its request still count.
        billed: list = []
        reask_pending = False  # counted only once the re-ask request is actually made
        fresh_pending: dict[str, Any] | None = None  # likewise for a fresh-state repair (ADR 0023)
        runaway_pending = False  # likewise for the runaway re-ask
        while True:
            used = len(billed)
            # The envelope (``TokenEnvelope``) admits the request only if its worst case fits what is left; the
            # usage limit is the same envelope checked after the response.
            left = max(max_tokens - sum(self._tokens(billed)), 1)
            reask_now, reask_pending = reask_pending, False
            if reask_now:
                report["reasks"] += 1
            runaway_now, runaway_pending = runaway_pending, False
            if runaway_now:
                report["runaway_reasks"] += 1
            fresh_now, fresh_pending = fresh_pending, None
            if fresh_now is not None:
                report["fresh_state_repairs"] += 1
                report["fresh_state"].append(fresh_now)
            active_agent = agent
            if terminal:
                if terminal_agent is None:
                    terminal_agent = self._agent(req, with_tools=False, output_type=str,
                                                 instructions=SYSTEM + "\n\n" + FINALIZATION_JSON_SHAPE)
                active_agent = terminal_agent
            active_settings = terminal_settings if terminal else settings
            with capture_run_messages() as captured:
                try:
                    run = active_agent.run_sync(
                        message + ("\n\n" + FINALIZATION_DECISION_ONLY if terminal else "")
                        + ("\n\n" + (FINALIZATION_RUNAWAY_NOTE if terminal else RUNAWAY_NOTE.format(cap=output_cap(self.config)))
                           if runaway_now else ""),
                        message_history=history or None,
                        usage_limits=UsageLimits(request_limit=1, total_tokens_limit=left),
                        model_settings=(self._runaway_settings(active_settings, mode, conclude_only=terminal)
                                        if runaway_now else active_settings) or None)
                except EnvelopeRefused as exc:  # not started: nothing billed, a re-ask that was not sent is not counted
                    report["reasks"] -= int(reask_now)
                    report["runaway_reasks"] -= int(runaway_now)
                    if fresh_now is not None:  # a fresh-state repair that was not sent is not counted
                        report["fresh_state_repairs"] -= 1
                        report["fresh_state"].pop()
                    outcome = "reasoning-runaway" if runaway_now else (
                        "structured-output-invalid" if report["errors"] and not report["valid"] and (not terminal or reask_now)
                        else "request-budget-exhausted")
                    return self._bounded(archived + history, model, max_requests, f"decision token envelope: {exc}", outcome,
                                         report, billed)
                except UsageLimitExceeded as exc:
                    billed += [m for m in captured if isinstance(m, ModelResponse) and m not in billed]
                    history += [m for m in captured if m not in history]
                    return self._bounded(archived + history, model, max_requests, str(exc), "request-budget-exhausted", report,
                                         billed)
                except ModelCallFailed:
                    raise
                except Exception as exc:  # provider/transport failure: report everything that may have billed
                    new = [m for m in captured if isinstance(m, ModelResponse) and m not in history]
                    billed += new
                    if isinstance(exc, UnexpectedModelBehavior) and new and is_runaway(new[-1]):
                        ended = runaway(new[-1], runaway_now)
                        if ended is not None:
                            return ended
                        runaway_pending = True  # same history, same message, concise and decision-only
                        terminal = req.conclude_only
                        continue
                    usage = self._usage(billed)
                    report["repairs"] = sorted(set(report["repairs"]))
                    usage["structured_output"] = report
                    raise model_call_failed(exc, usage) from exc
            new_responses = [m for m in run.new_messages() if isinstance(m, ModelResponse)]
            billed += new_responses
            if new_responses and is_runaway(new_responses[-1]):
                # Whitespace-only text at the cap: Pydantic AI returns it as text, but it is the same runaway.
                ended = runaway(new_responses[-1], runaway_now)
                if ended is not None:
                    return ended
                runaway_pending = True
                terminal = req.conclude_only
                continue
            history = list(run.all_messages())
            used = len(billed)
            try:
                reads, proposal = parse(run.output, decision_only=runaway_now or terminal)
            except (Invalid, ValidationError, ValueError) as exc:
                msg = so.validation_message(exc)
                report["errors"].append(msg)
                if req.conclude_only:
                    # One decision-only correction even when the read phase has not ended.
                    if not terminal and report["read_requests"] < report["finalization_read_round_limit"]:
                        report.update(read_rounds_forfeited=True, evidence_limited=True)
                    terminal = True
                if isinstance(exc, Invalid):
                    report["last_error_code"] = exc.details.get("cause")
                elif req.conclude_only and run.output.strip():
                    report["last_error_code"] = "finalization-output-shape"
                else:
                    report.pop("last_error_code", None)
                if terminal:
                    if report["reasks"] < 1 and used < max_requests:
                        reask_pending = True
                        message = "Your reply could not be used: " + msg
                        if report.get("read_rounds_forfeited"):
                            message += "\nState the limits of the evidence you actually have."
                        continue
                    return self._bounded(archived + history, model, max_requests, msg,
                                         "structured-output-invalid", report, billed)
                if (report["reasks"] >= 1 or report["fresh_state_repairs"]) and fresh_left > 0 and used < max_requests:
                    # ADR 0023: still malformed after the re-ask: ask again from a fresh state (bounded, recorded)
                    fresh_left -= 1
                    fresh_pending = {"request": used + 1, "after_error": msg[:300]}  # counted once it is sent
                    archived.extend(history)
                    history = []
                    message = fresh_message + "\n\n" + FRESH_STATE_NOTE.format(error=msg[:300])
                    continue
                if report["reasks"] >= 1 or report["fresh_state_repairs"] or used >= max_requests:
                    # at most one schema-guided re-ask and the bounded fresh-state repairs
                    return self._bounded(archived + history, model, max_requests, msg, "structured-output-invalid", report,
                                         billed)
                reask_pending = True
                message = ("Your reply could not be used: " + msg + "\nReply with ONLY the corrected JSON object in "
                           "the required shape, nothing else.")
                continue
            if proposal is not None:
                report["valid"] = True
                break
            report["read_requests"] += 1
            assert reads is not None
            if used >= max_requests:  # no request left to decide with the results: nothing is executed
                report["reads"] += [{"request": used, "tool": r.get("tool") if isinstance(r, dict) else None,
                                     "executed": False} for r in reads]
                return self._bounded(archived + history, model, max_requests, f"read request in the last of {max_requests} "
                                     "requests", "request-budget-exhausted", report, billed)
            results = []
            for i, entry in enumerate(reads):
                rec: dict[str, Any] = {"request": used, "tool": entry.get("tool") if isinstance(entry, dict) else None}
                if i >= so.MAX_READS_PER_REQUEST:
                    out: Any = {"error": f"not executed: at most {so.MAX_READS_PER_REQUEST} reads per request"}
                    rec["executed"] = False
                else:
                    try:
                        tool, args = so.check_read(entry, reads_allowed)
                    except ValueError as exc:
                        out, rec["executed"] = {"error": str(exc)[:300]}, False
                    else:
                        out = _read(req.read_tools, tool, *args.values())
                        rec.update({"args": args, "executed": True,
                                    "available": not (isinstance(out, dict) and out.get("available") is False)})
                report["reads"].append(rec)
                results.append({"tool": rec["tool"], "args": rec.get("args"), "result": out})
            remaining = max_requests - used
            if read_round_limit is not None:
                rounds_left = read_round_limit - report["read_requests"]
                terminal = rounds_left <= 0 or remaining <= 1
            note = ("This is your last request for this decision: reply with your decision now; a read request will "
                    "not be executed." if remaining == 1 else
                    f"You have {remaining} model requests left for this decision, including your decision.")
            if read_round_limit is not None and not terminal:
                note += f" You have {rounds_left} finalization read round(s) left."
            message = "## Read results\n" + json.dumps(results, default=str) + ("\n\n" + note if not terminal else "")
        report["repairs"] = sorted(set(report["repairs"]))
        usage = self._usage(billed)
        report.pop("last_error_code", None)
        usage["structured_output"] = report  # repairs, re-asks, reads and runaways are never silent
        assert proposal is not None
        return DecisionResult(proposal, usage, json.loads(_messages_json(archived + history)), self._settings(usage, max_requests),
                              PROMPT_REVISION)

    def _usage(self, messages: list) -> dict[str, Any]:
        usage = usage_from_messages(messages, self._provider(), self.config.get("model"),
                                    self.config.get("price_per_mtok"), reasoning_billing=self._billing())
        if self._report is not None and self._report.get("mode") == "tool":
            usage["structured_output"] = dict(self._report)
        return usage

    def _billing(self) -> str:
        return str(self.config.get("reasoning_tokens_billing", "included"))

    def _tokens(self, messages: list) -> tuple[int, int]:
        u = self._usage(messages)
        return int(u["input_tokens"]), int(u["output_tokens"])

    def _bounded(self, messages: list, model: Any, max_requests: int, reason: str, outcome: str,
                 report: dict, billed: list | None = None) -> DecisionResult:
        """No usable decision within the bounds: report the outcome and every billed request
        (``billed``: every response of the decision, when ``messages`` leaves some out)."""
        usage = self._usage(messages if billed is None else billed)
        usage["limit_reached"] = str(reason)[:300]
        report["repairs"] = sorted(set(report["repairs"]))
        usage["structured_output"] = report
        return self._runtime_bound(messages, usage, max_requests, outcome)

    def _runtime_bound(self, messages: list, usage: dict, max_requests: int, outcome: str) -> DecisionResult:
        usage["runtime_outcome"] = outcome
        return DecisionResult({}, usage, json.loads(_messages_json(messages)),
                              self._settings(usage, max_requests), PROMPT_REVISION, outcome)

    def _decide_tool(self, req: DecisionRequest) -> DecisionResult:
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits
        self._runaway_effort()  # a misconfigured effort fails before any request (nothing billed yet)
        max_requests = int(self.config.get("max_requests_per_decision", 8))
        tool_budget = max(1, max_requests - 1)
        if req.conclude_only:
            tool_budget = min(FINALIZATION_READ_ROUNDS, max_requests - 1) if req.read_tools else 0
        terminal = req.conclude_only and tool_budget <= 0
        if req.conclude_only:
            self._report = {"mode": "tool", "finalization_read_round_limit": tool_budget,
                            "read_rounds_forfeited": False}
        terminal_requests = self._terminal_requests(max_requests) if terminal else None
        agent = self._agent(req, with_tools=not terminal, terminal=terminal,
                            output_retries=terminal_requests - 1 if terminal_requests is not None else None)
        prompt = (req.context + "\n\n## This turn so far\n" + json.dumps(req.turn_so_far, default=str)[:20000]
                  + f"\n\n## Decision budget\nYou may make at most {max_requests} model requests for this decision, "
                  "including read-tool round trips. Reserve the last one for your final decision.")
        settings = {"max_tokens": output_cap(self.config)}  # the cap the envelope admits with; temperature unset
        if terminal:
            prompt += "\n\n" + FINALIZATION_DECISION_ONLY
        elif req.conclude_only:
            prompt += "\n\n" + FINALIZATION_READ_NOTE.format(rounds=tool_budget)
        limits = UsageLimits(request_limit=terminal_requests if terminal else (tool_budget if req.read_tools else max_requests),
                             total_tokens_limit=int(self.config.get("max_tokens_per_decision", 120_000)))
        limit_error: Exception
        with capture_run_messages() as messages:
            try:
                result = agent.run_sync(prompt, usage_limits=limits, model_settings=settings or None)
            except EnvelopeRefused as exc:  # the next request was not started: its worst case does not fit
                usage = self._usage(messages)
                usage["limit_reached"] = f"decision token envelope: {exc}"[:300]
                outcome = "structured-output-invalid" if terminal and (self._report or {}).get("errors") else "request-budget-exhausted"
                return self._runtime_bound(messages, usage, max_requests, outcome)
            except UsageLimitExceeded as exc:
                limit_error = exc
            except ModelCallFailed:
                raise
            except Exception as exc:  # provider/transport failure: report everything that may have billed
                from pydantic_ai.exceptions import UnexpectedModelBehavior
                from pydantic_ai.messages import ModelResponse
                responses = [m for m in messages if isinstance(m, ModelResponse)]
                if isinstance(exc, UnexpectedModelBehavior) and responses and terminal:
                    if not is_runaway(responses[-1]):
                        self._record_native_output_shape(exc, responses[-1])
                    usage = self._usage(messages)
                    return self._runtime_bound(messages, usage, max_requests,
                        "reasoning-runaway" if is_runaway(responses[-1]) else "structured-output-invalid")
                if isinstance(exc, UnexpectedModelBehavior) and responses and is_runaway(responses[-1]):
                    return self._tool_runaway(req, list(messages), responses[-1], max_requests, settings)
                if isinstance(exc, UnexpectedModelBehavior) and responses and req.conclude_only:
                    # Preserve admitted read evidence for the only remaining decision request.
                    limit_error = exc
                elif isinstance(exc, UnexpectedModelBehavior) and responses \
                        and int(self.config.get("fresh_state_repairs", DEFAULT_FRESH_STATE_REPAIRS)) > 0:
                    # ADR 0023: the output stayed invalid through the output retries: one fresh-state request
                    return self._tool_fresh_state(req, prompt, list(messages), exc, max_requests, settings)
                else:
                    raise model_call_failed(exc, self._usage(messages)) from exc
            else:
                usage = self._usage(result.all_messages())
                proposal = to_proposal(result.output, strict_scope=req.conclusion_scope_schema is not None)
                if terminal and not _conclusion(proposal):
                    return self._runtime_bound(list(result.all_messages()), usage, max_requests, "structured-output-invalid")
                try:
                    if req.conclude_only:
                        _validate_native_finalization(req, proposal, result.all_messages())
                except Invalid as exc:
                    # The early conclusion already used its initial attempt. Correct it without read tools.
                    limit_error = exc
                else:
                    usage.get("structured_output", {}).pop("last_error_code", None)
                    raw = json.loads(result.all_messages_json())
                    return DecisionResult(proposal, usage, raw, self._settings(usage, max_requests),
                                          PROMPT_REVISION)

        # The reserved request starts outside the prior UsageLimitExceeded
        # handler, so its provider failure has no local-limit exception context.
        return self._tool_after_limit(req, list(messages), limit_error, max_requests, tool_budget, settings)

    def _record_native_output_shape(self, error: Exception, response: Any) -> None:
        if self._native_error_response is response:
            return  # the output validator already attributed this exact response
        report: dict[str, Any] = self._report if self._report is not None else {"mode": "tool"}
        underlying = error.__cause__
        detail = so.validation_message(underlying if isinstance(underlying, Exception) else error)
        report.setdefault("errors", []).append(detail)
        if _has_authored_output(response):
            report["last_error_code"] = "finalization-output-shape"
        else:
            report.pop("last_error_code", None)
        self._report = report

    def _tool_after_limit(self, req: DecisionRequest, messages: list, error: Exception,
                          max_requests: int, tool_budget: int, settings: dict[str, Any]) -> DecisionResult:
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
        from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart
        from pydantic_ai.usage import UsageLimits

        reason = str(error)
        underlying = error.__cause__ if isinstance(error, UnexpectedModelBehavior) else None
        detail = so.validation_message(underlying if isinstance(underlying, Exception) else error)
        output_refused = req.conclude_only and isinstance(error, (Invalid, UnexpectedModelBehavior))
        outcome = "structured-output-invalid" if output_refused else "request-budget-exhausted"
        if output_refused:
            report: dict[str, Any] = self._report if self._report is not None else {"mode": "tool"}
            report.setdefault("errors", []).append(detail)
            if isinstance(error, Invalid):
                report["last_error_code"] = error.details.get("cause")
            else:
                response = next((message for message in reversed(messages) if isinstance(message, ModelResponse)), None)
                if _has_authored_output(response):
                    report["last_error_code"] = "finalization-output-shape"
            if _native_read_rounds(messages, req.read_tools) < tool_budget:
                report.update(read_rounds_forfeited=True, evidence_limited=True)
            self._report = report
        if req.read_tools and max_requests > 1 and tool_budget > 0:
            # Continue without tools over the evidence already gathered.
            first = list(messages)
            billed = int(self._usage(first)["calls"])
            remaining = max_requests - billed
            terminal_requests = self._terminal_requests(remaining) if req.conclude_only else 1
            last_reply = max((i for i, message in enumerate(first) if isinstance(message, ModelResponse)), default=0)
            previous_repair = any(isinstance(part, RetryPromptPart) for message in first[:last_reply]
                                  if isinstance(message, ModelRequest) for part in message.parts)
            if req.conclude_only and previous_repair:
                terminal_requests = 0 if output_refused else min(terminal_requests, 1)
            elif output_refused:
                terminal_requests = min(terminal_requests, int(int(self.config.get("output_retries", 1)) > 0))
            if terminal_requests <= 0:
                usage = self._usage(first)
                usage["limit_reached"] = reason[:300]
                return self._runtime_bound(first, usage, max_requests, outcome)
            final_agent = self._agent(req, with_tools=False, terminal=req.conclude_only,
                                      output_retries=terminal_requests - 1 if req.conclude_only else None)
            prompt = FINALIZATION_DECISION_ONLY if req.conclude_only else (
                "Your read-tool budget for this decision is used up. No further evidence is available. "
                "Return your decision now from what you have gathered."
            )
            if output_refused:
                prompt += "\n\nYour reply could not be used: " + detail
                if (self._report or {}).get("read_rounds_forfeited"):
                    prompt += "\nState the limits of the evidence you actually have."
            with capture_run_messages() as final_messages:
                try:
                    result = final_agent.run_sync(
                        prompt,
                        message_history=_close_tool_calls(first),
                        # Finalization can use its existing one correction if the original bounds permit.
                        usage_limits=UsageLimits(request_limit=terminal_requests),
                        model_settings=settings or None)
                    usage = self._usage(first + [m for m in result.new_messages()])
                    if not output_refused:
                        usage["tool_budget_exhausted"] = True
                    complete = first + list(result.new_messages())
                    proposal = to_proposal(result.output, strict_scope=req.conclusion_scope_schema is not None)
                    if req.conclude_only and not _conclusion(proposal):
                        return self._runtime_bound(complete, usage, max_requests, "structured-output-invalid")
                    usage.get("structured_output", {}).pop("last_error_code", None)
                    raw = json.loads(_messages_json(complete))
                    return DecisionResult(proposal, usage, raw,
                                          self._settings(usage, max_requests), PROMPT_REVISION)
                except (DecisionAborted, ModelCallFailed):
                    raise
                except Exception as final_exc:
                    new = [m for m in final_messages if m not in messages]
                    messages.extend(new)
                    last = [m for m in new if isinstance(m, ModelResponse)]
                    if isinstance(final_exc, UnexpectedModelBehavior) and last and is_runaway(last[-1]):
                        # F5 (A4): the reserved tool-free request ran away (reasoning only, output cap hit).
                        # It was the last request of the decision, so there is no re-ask: recorded, and the
                        # bounded outcome is reasoning-runaway, not request-budget-exhausted.
                        return self._tool_budget_runaway(messages, last[-1], max_requests)
                    if isinstance(final_exc, EnvelopeRefused):
                        reason = f"decision token envelope: {final_exc}"
                        if req.conclude_only and (self._report or {}).get("errors"):
                            outcome = "structured-output-invalid"
                    elif isinstance(final_exc, UnexpectedModelBehavior) and req.conclude_only and last:
                        self._record_native_output_shape(final_exc, last[-1])
                        outcome = "structured-output-invalid"
                    elif not isinstance(final_exc, (UnexpectedModelBehavior, UsageLimitExceeded)):
                        raise model_call_failed(final_exc, self._usage(messages)) from final_exc
        usage = self._usage(messages)
        usage["limit_reached"] = reason[:300]
        return self._runtime_bound(messages, usage, max_requests, outcome)

    def _tool_budget_runaway(self, messages: list, runaway: Any, max_requests: int) -> DecisionResult:
        """Tool mode: the reserved tool-free request after the read-tool budget ran away (F5, A4). No request is left
        for a re-ask: the runaway is recorded and the decision ends in the bounded no-effect ``reasoning-runaway``."""
        from pydantic_ai.messages import ModelResponse
        billed = [m for m in messages if isinstance(m, ModelResponse)]
        report: dict[str, Any] = {"mode": "tool", "runaway": [runaway_record(runaway, len(billed), False,
                                                                             self._billing())], "runaway_reasks": 0}
        self._report = report
        usage = self._usage(messages)
        usage["tool_budget_exhausted"] = True
        usage["limit_reached"] = (f"reasoning runaway in the reserved final request (output cap "
                                  f"{output_cap(self.config)}) after the read-tool budget; no request left")
        usage["structured_output"] = report
        history = _close_tool_calls([m for m in messages if m is not runaway])
        return self._runtime_bound(history, usage, max_requests, "reasoning-runaway")

    def _tool_runaway(self, req: DecisionRequest, messages: list, runaway: Any, max_requests: int,
                      settings: dict[str, Any]) -> DecisionResult:
        """Tool mode: the last response ran away (``is_runaway``). One concise request WITHOUT read tools over the
        history before it (reduced reasoning request if configured), if the request budget has one left; a second
        runaway, or no request left, ends in the bounded no-effect outcome ``reasoning-runaway`` (ADR 0019)."""
        from pydantic_ai import capture_run_messages
        from pydantic_ai.messages import ModelResponse
        from pydantic_ai.usage import UsageLimits
        billed = [m for m in messages if isinstance(m, ModelResponse)]
        kept = _close_tool_calls([m for m in messages if m is not runaway])
        report: dict[str, Any] = {"mode": "tool", "runaway": [runaway_record(runaway, len(billed), False,
                                                                             self._billing())], "runaway_reasks": 0}
        if req.conclude_only:
            limit = (self._report or {}).get("finalization_read_round_limit", 0)
            report.update(evidence_limited=True, finalization_read_round_limit=limit,
                          read_rounds_forfeited=_native_read_rounds(messages, req.read_tools) < limit)
        self._report = report

        def bounded(reason: str, history: list) -> DecisionResult:
            usage = self._usage(billed)
            usage["limit_reached"] = reason[:300]
            usage["structured_output"] = report
            return self._runtime_bound(history, usage, max_requests, "reasoning-runaway")

        if len(billed) >= max_requests:
            return bounded(f"reasoning runaway in the last of {max_requests} requests", kept)
        effort = self._runaway_effort()
        final_settings = {**settings, **({"extra_body": {"reasoning_effort": effort}} if effort else {})}
        report["runaway_reasks"] = 1
        final_agent = self._agent(req, with_tools=False, output_retries=0 if req.conclude_only else None)
        with capture_run_messages() as final_messages:
            try:
                result = final_agent.run_sync(FINALIZATION_DECISION_ONLY + "\n\n" + FINALIZATION_RUNAWAY_NOTE if req.conclude_only else
                                              RUNAWAY_TOOL_NOTE.format(cap=output_cap(self.config)),
                                              message_history=kept, usage_limits=UsageLimits(request_limit=1),
                                              model_settings=final_settings)
            except EnvelopeRefused as exc:  # not started: nothing billed
                report["runaway_reasks"] = 0
                return bounded(f"decision token envelope: {exc}", kept)
            except (DecisionAborted, ModelCallFailed):
                raise
            except Exception as exc:  # noqa: BLE001
                from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
                new = [m for m in final_messages if isinstance(m, ModelResponse) and m not in billed]
                billed.extend(new)
                if isinstance(exc, UnexpectedModelBehavior) and new and is_runaway(new[-1]):
                    report["runaway"].append(runaway_record(new[-1], len(billed), True, self._billing()))
                    return bounded(f"reasoning runaway: output cap {output_cap(self.config)} reached twice without "
                                   "an answer", kept)
                if req.conclude_only and new and isinstance(exc, (UnexpectedModelBehavior, UsageLimitExceeded)):
                    underlying = exc.__cause__
                    detail = so.validation_message(underlying if isinstance(underlying, Exception) else exc)
                    self._record_native_output_shape(exc, new[-1])
                    usage = self._usage(billed)
                    usage["limit_reached"] = detail[:300]
                    complete = messages + [m for m in final_messages if m not in messages]
                    return self._runtime_bound(complete, usage, max_requests, "structured-output-invalid")
                usage = self._usage(billed)
                usage["structured_output"] = report
                raise model_call_failed(exc, usage) from exc
        new_messages = list(result.new_messages())
        billed.extend(m for m in new_messages if isinstance(m, ModelResponse))
        usage = self._usage(billed)
        usage["structured_output"] = report
        complete = (messages if req.conclude_only else kept) + new_messages
        proposal = to_proposal(result.output, strict_scope=req.conclusion_scope_schema is not None)
        if req.conclude_only:
            # The runaway recovery already used its one tool-free request.
            try:
                _validate_native_finalization(req, proposal, new_messages)
            except Invalid as exc:
                usage["limit_reached"] = so.validation_message(exc, limit=300)
                report.setdefault("errors", []).append(so.validation_message(exc))
                report["last_error_code"] = exc.details.get("cause")
                return self._runtime_bound(complete, usage, max_requests, "structured-output-invalid")
        usage.get("structured_output", {}).pop("last_error_code", None)
        return DecisionResult(proposal, usage, json.loads(_messages_json(complete)),
                              self._settings(usage, max_requests), PROMPT_REVISION)

    def _tool_fresh_state(self, req: DecisionRequest, prompt: str, messages: list, error: BaseException,
                          max_requests: int, settings: dict[str, Any]) -> DecisionResult:
        """Tool mode (ADR 0023): the decision output stayed invalid through the output retries. One fresh-state run:
        the original prompt and a note of the error, no earlier replies, no read tools, within the requests left. A
        reply still invalid ends in the bounded no-effect ``structured-output-invalid``; every request is billed."""
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
        from pydantic_ai.messages import ModelResponse
        from pydantic_ai.usage import UsageLimits
        billed = [m for m in messages if isinstance(m, ModelResponse)]
        msg = str(error)[:300]
        report: dict[str, Any] = {"mode": "tool", "errors": [msg], "fresh_state_repairs": 0, "fresh_state": [],
                                  "valid": False}
        self._report = report

        def bounded(reason: str, history: list) -> DecisionResult:
            usage = self._usage(billed)
            usage["limit_reached"] = reason[:300]
            usage["structured_output"] = report
            return self._runtime_bound(history, usage, max_requests, "structured-output-invalid")

        left = max_requests - len(billed)
        if left < 1:
            return bounded(f"structured output invalid in the last of {max_requests} requests: {msg}", messages)
        report["fresh_state_repairs"] = 1
        report["fresh_state"].append({"request": len(billed) + 1, "after_error": msg})
        agent = self._agent(req, with_tools=False)
        with capture_run_messages() as fresh:
            try:
                result = agent.run_sync(prompt + "\n\n" + FRESH_STATE_NOTE.format(error=msg),
                                        usage_limits=UsageLimits(request_limit=min(left, 1 + int(
                                            self.config.get("output_retries", 1)))), model_settings=settings or None)
            except EnvelopeRefused as exc:  # not started: nothing billed
                report["fresh_state_repairs"] = 0
                report["fresh_state"] = []
                return bounded(f"decision token envelope: {exc}", messages)
            except (DecisionAborted, ModelCallFailed):
                raise
            except Exception as exc:  # noqa: BLE001
                billed.extend(m for m in fresh if isinstance(m, ModelResponse) and m not in billed)
                if isinstance(exc, (UnexpectedModelBehavior, UsageLimitExceeded)):  # invalid within the requests left
                    report["errors"].append(str(exc)[:300])
                    return bounded(f"structured output invalid after a fresh-state repair: {str(exc)[:200]}",
                                   messages + list(fresh))
                usage = self._usage(billed)
                usage["structured_output"] = report
                raise model_call_failed(exc, usage) from exc
        new_messages = list(result.new_messages())
        billed.extend(m for m in new_messages if isinstance(m, ModelResponse))
        report["valid"] = True
        usage = self._usage(billed)
        usage["structured_output"] = report
        return DecisionResult(to_proposal(result.output, strict_scope=req.conclusion_scope_schema is not None), usage, json.loads(_messages_json(messages + new_messages)),
                              self._settings(usage, max_requests), PROMPT_REVISION)

    def _settings(self, usage: dict, max_requests: int) -> dict[str, Any]:
        return {"runtime": "pydantic-ai", "model": self.config.get("model"), "served_models": usage.get("served_models"),
                "max_output_tokens": output_cap(self.config), "temperature": "provider default",
                "max_requests_per_decision": max_requests, "output_retries": int(self.config.get("output_retries", 1)),
                "transport_retries": 0, "args_encoding": "json-string",
                "fresh_state_repairs": int(self.config.get("fresh_state_repairs", DEFAULT_FRESH_STATE_REPAIRS)),
                "output_mode": self.config.get("output_mode", "tool"),
                **({"runaway_reasoning_effort": self.config["runaway_reasoning_effort"]}
                   if self.config.get("runaway_reasoning_effort") else {}),
                **({"structured_output": usage["structured_output"]} if usage.get("structured_output") else {})}

    def _provider(self) -> str:
        return (self.config.get("model") or "unknown").split(":", 1)[0]


def _has_authored_output(response: Any) -> bool:
    from pydantic_ai.messages import TextPart, ToolCallPart

    return any(isinstance(part, ToolCallPart) or isinstance(part, TextPart) and bool(part.content.strip())
               for part in getattr(response, "parts", ()))


def _native_read_rounds(messages: list, tools: dict) -> int:
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    return sum(any(isinstance(part, ToolReturnPart) and part.tool_name in tools for part in message.parts)
               for message in messages if isinstance(message, ModelRequest))


def _validate_native_finalization(req: DecisionRequest, proposal: dict[str, Any], messages: list) -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    response = next((message for message in reversed(messages) if isinstance(message, ModelResponse)), None)
    if response and sum(isinstance(part, ToolCallPart) for part in response.parts) > 1:
        raise Invalid("Submit one conclusion without read calls or other tool calls.",
                      details={"cause": "finalization-control"})
    _validate_finalization(req, proposal)


def _validate_finalization(req: DecisionRequest, proposal: dict[str, Any]) -> None:
    if not _conclusion(proposal):
        raise Invalid(FINALIZATION_DECISION_ONLY, details={"cause": "finalization-control"})
    control = proposal.get("control") or {}
    validate_conclusion_scope(control, req.conclusion_scope_schema)
    claim = control.get("claim")
    if claim is not None:
        try:
            validate_claim_block(claim)
        except Invalid as exc:
            raise Invalid(exc.message, details={**exc.details, "cause": "finalization-claim-contract"}) from exc
    if req.finalization_has_eligible_lock is False and isinstance(claim, dict) and claim.get("disposition") in (
            "candidate", "confirmed"):
        raise Invalid("No eligible locked candidate: finalization must honestly abstain",
                      details={"cause": "finalization-no-lock"})


def _conclusion(proposal: dict[str, Any]) -> bool:
    return not proposal["calls"] and (proposal.get("control") or {}).get("action") in ("conclude", "stop")


def _close_tool_calls(messages: list) -> list:
    """History ending in unanswered tool calls cannot be continued; drop a trailing tool-call response."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    msgs = list(messages)
    while msgs and isinstance(msgs[-1], ModelResponse) and any(isinstance(p, ToolCallPart) for p in msgs[-1].parts):
        msgs.pop()
    return msgs


def _messages_json(messages: list) -> bytes:
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    return ModelMessagesTypeAdapter.dump_json(messages)
