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
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from eidos.investigator.runtime import DecisionRequest, DecisionResult, ModelCallFailed

PROMPT_REVISION = "investigator-prompt/1"
SYSTEM = (
    "You are the internal investigator of a model-research lab. Diagnose failures, propose the most "
    "informative next experiment, author components when the catalog lacks a needed method, and stop "
    "with an honest scoped conclusion. Every hypothesis needs a prediction and a falsifier. You may call "
    "read-only tools to inspect evidence; effects happen only through the calls in your final decision, "
    "which the lab validates before execution. You cannot change success rules."
)


# Free-form objects are encoded as JSON *strings*: providers' strict structured-output modes reject
# open dict fields (Anthropic strict mode forces {} for them), which silently emptied tool args in the
# first live qualification run. Strings round-trip exactly and are validated here.
class Call(BaseModel):
    tool: str
    args_json: str = Field(description="JSON object with this tool's arguments, e.g. "
                                       '{"component_ref": "x@1.0.0", "inputs": {"train": "art_1"}, "config": {}}')

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


class Hypothesis(BaseModel):
    statement: str
    prediction: str = ""
    falsifier: str = ""


class Decision(BaseModel):
    rationale: str
    purpose: str = "open-search"
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    control: Control | None = None


def to_proposal(d: Decision) -> dict[str, Any]:
    """Decode the typed model output into the lab's decision proposal."""
    out = d.model_dump(exclude_none=True)
    out["calls"] = [{"tool": c.tool, "args": json.loads(c.args_json)} for c in d.calls]
    if d.control is not None:
        ctl = {"action": d.control.action, "summary": d.control.summary}
        if d.control.scope_json:
            try:
                ctl["scope"] = json.loads(d.control.scope_json)
            except ValueError:
                ctl["scope_text"] = d.control.scope_json
        out["control"] = {k: v for k, v in ctl.items() if v is not None}
    return out


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
            "summary": {"type": "string"}, "scope": {"type": "object"}}},
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


def usage_from_messages(messages: list, provider: str, model: str | None,
                        price: dict[str, float] | None = None) -> dict[str, Any]:
    from pydantic_ai.messages import ModelResponse
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    return {"provider": provider, "model": model,
            "input_tokens": sum(r.usage.input_tokens or 0 for r in responses),
            "output_tokens": sum(r.usage.output_tokens or 0 for r in responses),
            "calls": len(responses), "request_ids": [r.provider_response_id for r in responses if r.provider_response_id],
            "served_models": sorted({r.model_name for r in responses if r.model_name}),
            "per_request": [{"model": r.model_name, "input_tokens": r.usage.input_tokens,
                             "output_tokens": r.usage.output_tokens, "finish_reason": r.finish_reason} for r in responses],
            **({"usd": (sum(r.usage.input_tokens or 0 for r in responses) * price["input"]
                        + sum(r.usage.output_tokens or 0 for r in responses) * price["output"]) / 1e6} if price else {})}


def _bounded(x: Any, limit: int = 8000) -> Any:
    s = json.dumps(x, default=str)
    return x if len(s) <= limit else {"truncated": True, "preview": s[:limit]}


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

    def _agent(self, req: DecisionRequest, *, with_tools: bool = True, output_type: Any = None,
               instructions: str | None = None):
        from pydantic_ai import Agent
        tools = req.read_tools

        def call(name: str, *a: Any) -> Any:
            try:
                out = tools[name](*a)
            except Exception as exc:  # a read failure is information, never a crash of the decision
                return {"available": False, "error": str(exc)[:300],
                        "note": "No further evidence is available for this request; decide with what you have."}
            if isinstance(out, dict) and out.get("available") is False:
                out = {**out, "note": "No further evidence is available for this request; do not re-query it."}
            return _bounded(out)

        def inspect_evidence(artifact_id: str, max_bytes: int = 8192) -> Any:
            """Bounded preview of an artifact's content (text/JSON/CSV)."""
            return call("inspect_evidence", artifact_id, min(max_bytes, 16384))

        def search_components(text: str = "", capability: str | None = None) -> Any:
            """Search the installed component catalog (catalog entries are data, not tools)."""
            return call("search_components", text, capability)

        def describe_component(component_ref: str) -> Any:
            """Manifest, qualification and approvals of one component release."""
            return call("describe_component", component_ref)

        def get_job(job_id: str) -> Any:
            """Status, receipts and outputs of one job."""
            return call("get_job", job_id)

        return Agent(build_model(self.config), output_type=output_type or Decision, instructions=instructions or SYSTEM,
                     retries=int(self.config.get("output_retries", 1)),
                     tools=[inspect_evidence, search_components, describe_component, get_job]
                     if (tools and with_tools) else [])

    def decide(self, req: DecisionRequest) -> DecisionResult:
        mode = self.config.get("output_mode", "tool")
        if mode not in OUTPUT_MODES:
            raise ModelCallFailed(f"unknown output_mode {mode!r} (one of {OUTPUT_MODES})", retryable=False)
        if mode != "tool":
            return self._decide_text(req, mode)
        return self._decide_tool(req)

    # -- text output with bounded repair (ADR 0014) --------------------------------------------------------
    def _json_mode_settings(self) -> dict[str, Any] | None:
        """Constrained decoding where the endpoint supports it: OpenAI-compatible ``response_format``
        (Ollama maps it onto its ``format``). Anthropic has no JSON mode: the repair path alone applies."""
        if self._provider() in ("openai", "openai-compatible"):
            return {"response_format": {"type": "json_schema", "json_schema": {
                "name": "eidos_decision", "strict": False, "schema": LENIENT_DECISION_SCHEMA}}}
        return None

    def _decide_text(self, req: DecisionRequest, mode: str) -> DecisionResult:
        from pydantic import ValidationError
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits

        from eidos.investigator import structured_output as so
        max_requests = int(self.config.get("max_requests_per_decision", 8))
        model = self.config.get("model")
        settings: dict[str, Any] = {}
        if self.config.get("max_output_tokens"):
            settings["max_tokens"] = int(self.config["max_output_tokens"])
        extra = self._json_mode_settings() if mode == "json" else None
        if extra:
            settings["extra_body"] = extra
        report: dict[str, Any] = {"mode": mode, "json_mode": bool(extra), "repairs": [], "reasks": 0,
                                  "errors": [], "valid": False}
        instructions = SYSTEM + "\n\n" + so.JSON_SHAPE
        tools_agent = self._agent(req, output_type=str, instructions=instructions)
        prompt = (req.context + "\n\n## This turn so far\n" + json.dumps(req.turn_so_far, default=str)[:20000]
                  + f"\n\n## Decision budget\nYou may make at most {max_requests} model requests for this decision, "
                  "including read-tool round trips and one possible correction of a malformed reply.\n\n" + so.JSON_SHAPE)
        # One request is held back for the schema-guided re-ask when the budget allows it.
        reask_budget = 1 if max_requests > 1 else 0
        limits = UsageLimits(request_limit=max(1, max_requests - reask_budget),
                             total_tokens_limit=int(self.config.get("max_tokens_per_decision", 120_000)))

        def parse(text: str) -> dict[str, Any]:
            obj, repairs = so.extract_json(text)
            obj, shape = so.normalize(obj)
            report["repairs"] += repairs + shape
            return to_proposal(Decision.model_validate(obj))

        all_messages: list = []
        with capture_run_messages() as messages:
            try:
                result = tools_agent.run_sync(prompt, usage_limits=limits, model_settings=settings or None)
                all_messages = list(result.all_messages())
                text = result.output
            except UsageLimitExceeded as exc:
                all_messages = list(messages)
                return self._bounded(all_messages, model, max_requests, str(exc), "request-budget-exhausted", report)
            except ModelCallFailed:
                raise
            except Exception as exc:  # provider/transport failure: report everything that may have billed
                usage = usage_from_messages(list(messages), self._provider(), model, self.config.get("price_per_mtok"))
                usage["failed_request"] = {"error": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
                usage["structured_output"] = report
                raise ModelCallFailed(f"{type(exc).__name__}: {exc}"[:500], retryable=classify_failure(exc),
                                      possible_usage=usage) from exc
        try:
            proposal = parse(text)
            report["valid"] = True
        except (so.StructuredOutputError, ValidationError, ValueError) as exc:
            msg = so.validation_message(exc)
            report["errors"].append(msg)
            if not reask_budget:
                return self._bounded(all_messages, model, max_requests, msg, "structured-output-invalid", report)
            report["reasks"] += 1
            fix_agent = self._agent(req, with_tools=False, output_type=str, instructions=instructions)
            with capture_run_messages() as fix_messages:
                try:
                    fixed = fix_agent.run_sync(
                        "Your reply could not be used as a decision: " + msg + "\nReply with ONLY the corrected JSON "
                        "object in the required shape, nothing else.", message_history=_close_tool_calls(all_messages),
                        usage_limits=UsageLimits(request_limit=1), model_settings=settings or None)
                    all_messages += list(fixed.new_messages())
                    proposal = parse(fixed.output)
                    report["valid"] = True
                except (so.StructuredOutputError, ValidationError, ValueError) as exc2:
                    report["errors"].append(so.validation_message(exc2))
                    all_messages += [m for m in fix_messages if m not in all_messages]
                    return self._bounded(all_messages, model, max_requests, report["errors"][-1],
                                         "structured-output-invalid", report)
                except UsageLimitExceeded as exc2:
                    all_messages += [m for m in fix_messages if m not in all_messages]
                    return self._bounded(all_messages, model, max_requests, str(exc2), "request-budget-exhausted",
                                         report)
                except Exception as exc2:  # noqa: BLE001
                    usage = usage_from_messages(all_messages + list(fix_messages), self._provider(), model,
                                                self.config.get("price_per_mtok"))
                    usage["failed_request"] = {"error": type(exc2).__name__}
                    usage["structured_output"] = report
                    raise ModelCallFailed(f"{type(exc2).__name__}: {exc2}"[:500], retryable=classify_failure(exc2),
                                          possible_usage=usage) from exc2
        report["repairs"] = sorted(set(report["repairs"]))
        usage = usage_from_messages(all_messages, self._provider(), model, self.config.get("price_per_mtok"))
        usage["structured_output"] = report  # repairs and re-asks are never silent: usage, receipts, settings
        raw = json.loads(_messages_json(all_messages))
        return DecisionResult(proposal, usage, raw, self._settings(usage, max_requests), PROMPT_REVISION)

    def _bounded(self, messages: list, model: Any, max_requests: int, reason: str, outcome: str,
                 report: dict) -> DecisionResult:
        """No usable decision within the bounds: an explicit no-effect decision; every billed request reported."""
        usage = usage_from_messages(messages, self._provider(), model, self.config.get("price_per_mtok"))
        usage["limit_reached"] = str(reason)[:300]
        report["repairs"] = sorted(set(report["repairs"]))
        usage["structured_output"] = report
        proposal = {"rationale": f"no usable decision ({outcome}); no effects proposed", "calls": [],
                    "control": {"action": "wait_for_jobs", "summary": outcome, "bounded_outcome": outcome}}
        return DecisionResult(proposal, usage, json.loads(_messages_json(messages)), self._settings(usage, max_requests),
                              PROMPT_REVISION)

    def _decide_tool(self, req: DecisionRequest) -> DecisionResult:
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits
        agent = self._agent(req)
        max_requests = int(self.config.get("max_requests_per_decision", 8))
        prompt = (req.context + "\n\n## This turn so far\n" + json.dumps(req.turn_so_far, default=str)[:20000]
                  + f"\n\n## Decision budget\nYou may make at most {max_requests} model requests for this decision, "
                  "including read-tool round trips. Reserve the last one for your final decision.")
        settings = {}
        if self.config.get("max_output_tokens"):
            settings["max_tokens"] = int(self.config["max_output_tokens"])  # temperature deliberately unset
        model = self.config.get("model")
        tool_budget = max(1, max_requests - 1)  # the last request is reserved for a tool-free decision
        limits = UsageLimits(request_limit=tool_budget if req.read_tools else max_requests,
                             total_tokens_limit=int(self.config.get("max_tokens_per_decision", 120_000)))
        with capture_run_messages() as messages:
            try:
                result = agent.run_sync(prompt, usage_limits=limits, model_settings=settings or None)
            except UsageLimitExceeded as exc:
                if req.read_tools and max_requests > 1:
                    # Read-tool loop hit its share: one final request WITHOUT tools over the same history,
                    # so the model must decide from the evidence it has already gathered.
                    first = list(messages)
                    final_agent = self._agent(req, with_tools=False)
                    with capture_run_messages() as final_messages:
                        try:
                            result = final_agent.run_sync(
                                "Your read-tool budget for this decision is used up. No further evidence is "
                                "available. Return your decision now from what you have gathered.",
                                message_history=_close_tool_calls(first),
                                # exactly the one reserved request: the per-decision cap is a hard cap
                                usage_limits=UsageLimits(request_limit=max(1, max_requests - tool_budget)),
                                model_settings=settings or None)
                            usage = usage_from_messages(first + [m for m in result.new_messages()], self._provider(), model, self.config.get("price_per_mtok"))
                            usage["tool_budget_exhausted"] = True
                            raw = json.loads(_messages_json(first + list(result.new_messages())))
                            return DecisionResult(to_proposal(result.output), usage, raw,
                                                  self._settings(usage, max_requests), PROMPT_REVISION)
                        except Exception:  # noqa: BLE001 - fall through to the bounded outcome below
                            messages.extend(m for m in final_messages if m not in messages)
                # A bounded outcome, not a lost failure: every billed request is reported and the
                # slot ends with an explicit no-effect decision the next turn can see.
                usage = usage_from_messages(messages, self._provider(), model, self.config.get("price_per_mtok"))
                usage["limit_reached"] = str(exc)[:300]
                proposal = {"rationale": f"decision request budget ({max_requests}) exhausted before a final "
                                         "decision; no effects proposed", "calls": [],
                            "control": {"action": "wait_for_jobs", "summary": "request budget exhausted",
                                        "bounded_outcome": "request-budget-exhausted"}}
                return DecisionResult(proposal, usage, json.loads(_messages_json(messages)),
                                      self._settings(usage, max_requests), PROMPT_REVISION)
            except ModelCallFailed:
                raise
            except Exception as exc:  # provider/transport failure: report everything that may have billed
                usage = usage_from_messages(messages, self._provider(), model, self.config.get("price_per_mtok"))
                usage["failed_request"] = {"error": type(exc).__name__,
                                           "status_code": getattr(exc, "status_code", None)}
                raise ModelCallFailed(f"{type(exc).__name__}: {exc}"[:500], retryable=classify_failure(exc),
                                      possible_usage=usage) from exc
        usage = usage_from_messages(result.all_messages(), self._provider(), model, self.config.get("price_per_mtok"))
        raw = json.loads(result.all_messages_json())
        return DecisionResult(to_proposal(result.output), usage, raw, self._settings(usage, max_requests),
                              PROMPT_REVISION)

    def _settings(self, usage: dict, max_requests: int) -> dict[str, Any]:
        return {"runtime": "pydantic-ai", "model": self.config.get("model"), "served_models": usage.get("served_models"),
                "max_output_tokens": self.config.get("max_output_tokens"), "temperature": "provider default",
                "max_requests_per_decision": max_requests, "output_retries": int(self.config.get("output_retries", 1)),
                "transport_retries": 0, "args_encoding": "json-string",
                "output_mode": self.config.get("output_mode", "tool"),
                **({"structured_output": usage["structured_output"]} if usage.get("structured_output") else {})}

    def _provider(self) -> str:
        return (self.config.get("model") or "unknown").split(":", 1)[0]


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
