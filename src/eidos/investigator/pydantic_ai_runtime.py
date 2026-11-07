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

from pydantic import BaseModel, Field

from eidos.investigator.runtime import DecisionRequest, DecisionResult, ModelCallFailed

PROMPT_REVISION = "investigator-prompt/1"
SYSTEM = (
    "You are the internal investigator of a model-research lab. Diagnose failures, propose the most "
    "informative next experiment, author components when the catalog lacks a needed method, and stop "
    "with an honest scoped conclusion. Every hypothesis needs a prediction and a falsifier. You may call "
    "read-only tools to inspect evidence; effects happen only through the calls in your final decision, "
    "which the lab validates before execution. You cannot change success rules."
)


class Call(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Control(BaseModel):
    action: Literal["wait_for_jobs", "request_input", "conclude", "stop"] | None = None
    summary: str = ""
    scope: dict[str, Any] | None = None


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
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits
        agent = self._agent(req)
        prompt = req.context + "\n\n## This turn so far\n" + json.dumps(req.turn_so_far, default=str)[:20000]
        limits = UsageLimits(request_limit=int(self.config.get("max_requests_per_decision", 6)),
                             total_tokens_limit=int(self.config.get("max_tokens_per_decision", 120_000)))
        try:
            result = agent.run_sync(prompt, usage_limits=limits)
        except UsageLimitExceeded as exc:
            raise ModelCallFailed(f"usage limit: {exc}", retryable=False,
                                  possible_usage={"provider": self._provider(), "model": self.config.get("model")}) from exc
        except ModelCallFailed:
            raise
        except Exception as exc:  # provider/transport failure: spend may have occurred
            request_id = getattr(getattr(exc, "response", None), "headers", {}).get("request-id") if hasattr(exc, "response") else None
            raise ModelCallFailed(f"{type(exc).__name__}: {exc}"[:500], retryable=True,
                                  possible_usage={"provider": self._provider(), "model": self.config.get("model"),
                                                  "request_ids": [request_id] if request_id else []}) from exc
        u = result.usage()
        usage = {"provider": self._provider(), "model": self.config.get("model"), "input_tokens": u.input_tokens or 0,
                 "output_tokens": u.output_tokens or 0, "calls": u.requests, "request_ids": []}
        price = self.config.get("price_per_mtok")  # {"input": x, "output": y} recorded by the operator
        if price:
            usage["usd"] = (usage["input_tokens"] * price["input"] + usage["output_tokens"] * price["output"]) / 1e6
        raw = json.loads(result.all_messages_json())
        settings = {"runtime": "pydantic-ai", "model": self.config.get("model"),
                    "resolved_model": getattr(agent.model, "model_name", None) if hasattr(agent, "model") else None,
                    "output_retries": int(self.config.get("output_retries", 1)), "transport_retries": 0}
        return DecisionResult(result.output.model_dump(exclude_none=True), usage, raw, settings, PROMPT_REVISION)

    def _provider(self) -> str:
        return (self.config.get("model") or "unknown").split(":", 1)[0]
