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


def usage_from_messages(messages: list, provider: str, model: str | None) -> dict[str, Any]:
    from pydantic_ai.messages import ModelResponse
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    return {"provider": provider, "model": model,
            "input_tokens": sum(r.usage.input_tokens or 0 for r in responses),
            "output_tokens": sum(r.usage.output_tokens or 0 for r in responses),
            "calls": len(responses), "request_ids": [r.provider_response_id for r in responses if r.provider_response_id],
            "served_models": sorted({r.model_name for r in responses if r.model_name}),
            "per_request": [{"model": r.model_name, "input_tokens": r.usage.input_tokens,
                             "output_tokens": r.usage.output_tokens, "finish_reason": r.finish_reason} for r in responses]}


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
        client = AsyncAnthropic(api_key=os.environ.get(cfg.get("api_key_env", "ANTHROPIC_API_KEY")), max_retries=0,
                                timeout=timeout)
        return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=client))
    if provider in ("openai", "openai-compatible"):
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        client = AsyncOpenAI(api_key=os.environ.get(cfg.get("api_key_env", "OPENAI_API_KEY"), "unused"),
                             base_url=cfg.get("base_url"), max_retries=0, timeout=timeout)
        return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=client))
    raise ModelCallFailed(f"unsupported model spec {spec!r}", retryable=False)


class PydanticAIRuntime:
    name = "pydantic-ai"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _agent(self, req: DecisionRequest):
        from pydantic_ai import Agent
        tools = req.read_tools

        def inspect_evidence(artifact_id: str, max_bytes: int = 8192) -> Any:
            """Bounded preview of an artifact's content (text/JSON/CSV)."""
            return _bounded(tools["inspect_evidence"](artifact_id, min(max_bytes, 16384)))

        def search_components(text: str = "", capability: str | None = None) -> Any:
            """Search the installed component catalog (catalog entries are data, not tools)."""
            return _bounded(tools["search_components"](text, capability))

        def describe_component(component_ref: str) -> Any:
            """Manifest, qualification and approvals of one component release."""
            return _bounded(tools["describe_component"](component_ref))

        def get_job(job_id: str) -> Any:
            """Status, receipts and outputs of one job."""
            return _bounded(tools["get_job"](job_id))

        return Agent(build_model(self.config), output_type=Decision, instructions=SYSTEM,
                     retries=int(self.config.get("output_retries", 1)),
                     tools=[inspect_evidence, search_components, describe_component, get_job] if tools else [])

    def decide(self, req: DecisionRequest) -> DecisionResult:
        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits
        agent = self._agent(req)
        max_requests = int(self.config.get("max_requests_per_decision", 8))
        prompt = (req.context + "\n\n## This turn so far\n" + json.dumps(req.turn_so_far, default=str)[:20000]
                  + f"\n\n## Decision budget\nYou may make at most {max_requests} model requests for this decision, "
                  "including read-tool round trips. Reserve the last one for your final decision.")
        limits = UsageLimits(request_limit=max_requests,
                             total_tokens_limit=int(self.config.get("max_tokens_per_decision", 120_000)))
        settings = {}
        if self.config.get("max_output_tokens"):
            settings["max_tokens"] = int(self.config["max_output_tokens"])  # temperature deliberately unset
        model = self.config.get("model")
        with capture_run_messages() as messages:
            try:
                result = agent.run_sync(prompt, usage_limits=limits, model_settings=settings or None)
            except UsageLimitExceeded as exc:
                # A bounded outcome, not a lost failure: every billed request is reported and the
                # slot ends with an explicit no-effect decision the next turn can see.
                usage = usage_from_messages(messages, self._provider(), model)
                usage["limit_reached"] = str(exc)[:300]
                proposal = {"rationale": f"decision request budget ({max_requests}) exhausted before a final "
                                         "decision; no effects proposed", "calls": [],
                            "control": {"action": "wait_for_jobs", "summary": "request budget exhausted"}}
                return DecisionResult(proposal, usage, json.loads(_messages_json(messages)),
                                      self._settings(usage, max_requests), PROMPT_REVISION)
            except ModelCallFailed:
                raise
            except Exception as exc:  # provider/transport failure: report everything that may have billed
                usage = usage_from_messages(messages, self._provider(), model)
                usage["failed_request"] = {"error": type(exc).__name__,
                                           "status_code": getattr(exc, "status_code", None)}
                raise ModelCallFailed(f"{type(exc).__name__}: {exc}"[:500], retryable=classify_failure(exc),
                                      possible_usage=usage) from exc
        usage = usage_from_messages(result.all_messages(), self._provider(), model)
        raw = json.loads(result.all_messages_json())
        price = self.config.get("price_per_mtok")  # {"input": x, "output": y} recorded by the operator
        if price:
            usage["usd"] = (usage["input_tokens"] * price["input"] + usage["output_tokens"] * price["output"]) / 1e6
        return DecisionResult(to_proposal(result.output), usage, raw, self._settings(usage, max_requests),
                              PROMPT_REVISION)

    def _settings(self, usage: dict, max_requests: int) -> dict[str, Any]:
        return {"runtime": "pydantic-ai", "model": self.config.get("model"), "served_models": usage.get("served_models"),
                "max_output_tokens": self.config.get("max_output_tokens"), "temperature": "provider default",
                "max_requests_per_decision": max_requests, "output_retries": int(self.config.get("output_retries", 1)),
                "transport_retries": 0, "args_encoding": "json-string"}

    def _provider(self) -> str:
        return (self.config.get("model") or "unknown").split(":", 1)[0]


def _messages_json(messages: list) -> bytes:
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    return ModelMessagesTypeAdapter.dump_json(messages)
