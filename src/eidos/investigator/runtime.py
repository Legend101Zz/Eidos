"""The ``InvestigatorRuntime`` port and the deterministic fixture runtime.

A runtime turns a packed context into a *proposed* decision. It has no authority: the command
service validates and accepts the decision before any effect. Provider transport retries are
disabled in every adapter (one retry owner: Temporal); model-output repair is a separate,
recorded call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "purpose": {"type": "string"},
        "hypotheses": {"type": "array", "items": {"type": "object"}},
        "calls": {"type": "array", "items": {"type": "object", "properties": {
            "tool": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool", "args"]}},
        "control": {"type": "object", "properties": {"action": {"type": "string"}, "summary": {"type": "string"}}},
    },
    "required": ["rationale", "calls"],
}


@dataclass
class DecisionRequest:
    turn_id: str
    slot: int
    context: str
    turn_so_far: list[dict[str, Any]]
    limits: dict[str, Any]
    settings: dict[str, Any] = field(default_factory=dict)
    read_tools: dict[str, Callable[..., Any]] = field(default_factory=dict)
    view: dict[str, Any] | None = None  # structured authorized view (same horizon as context)


@dataclass
class DecisionResult:
    proposal: dict[str, Any]
    usage: dict[str, Any]
    raw_response: Any
    model_settings: dict[str, Any]
    prompt_revision: str


class ModelCallFailed(Exception):
    """A provider call failed. ``possible_usage`` is recorded as uncertain spend."""

    def __init__(self, message: str, *, possible_usage: dict[str, Any] | None = None, retryable: bool = True):
        super().__init__(message)
        self.possible_usage = possible_usage or {}
        self.retryable = retryable


class InvestigatorRuntime(Protocol):
    name: str

    def decide(self, request: DecisionRequest) -> DecisionResult: ...


Policy = Callable[[DecisionRequest], dict[str, Any]]


class FixtureRuntime:
    """Deterministic scripted policies for development and tests. Every result is labelled
    ``fixture`` so it can never be mistaken for real LLM behaviour."""

    name = "fixture"

    def __init__(self, policy: Policy, *, policy_name: str):
        self.policy = policy
        self.policy_name = policy_name

    def decide(self, request: DecisionRequest) -> DecisionResult:
        proposal = self.policy(request)
        text = json.dumps(proposal, sort_keys=True)
        usage = {"provider": "fixture", "model": f"fixture:{self.policy_name}", "input_tokens": len(request.context) // 4,
                 "output_tokens": len(text) // 4, "usd": 0.0, "calls": 1, "request_ids": [], "fixture": True}
        return DecisionResult(proposal, usage, {"fixture_policy": self.policy_name, "proposal": proposal},
                              {"runtime": "fixture", "policy": self.policy_name}, f"fixture:{self.policy_name}")


FIXTURE_POLICIES: dict[str, Policy] = {}


def fixture_policy(name: str):
    def deco(fn: Policy) -> Policy:
        FIXTURE_POLICIES[name] = fn
        return fn
    return deco


@fixture_policy("conclude-immediately")
def _conclude(req: DecisionRequest) -> dict[str, Any]:
    return {"rationale": "fixture: nothing to do", "calls": [],
            "control": {"action": "conclude", "summary": "No exploration performed (fixture)."}}


@fixture_policy("note-then-wait")
def _note(req: DecisionRequest) -> dict[str, Any]:
    if req.turn_so_far:
        return {"rationale": "wait", "calls": [], "control": {"action": "wait_for_jobs"}}
    return {"rationale": "record a hypothesis", "calls": [{"tool": "record_note", "args": {
        "kind": "hypothesis", "text": "fixture hypothesis"}}], "control": {}}


def build_runtime(config: dict[str, Any]) -> InvestigatorRuntime:
    kind = config.get("runtime", "fixture")
    if kind == "fixture":
        name = config.get("policy", "conclude-immediately")
        if name not in FIXTURE_POLICIES:
            import importlib
            for mod in ("eidos.investigator.task_policies", "eidos.investigator.fixture_policies"):
                try:
                    importlib.import_module(mod)
                except ModuleNotFoundError:
                    pass
        return FixtureRuntime(FIXTURE_POLICIES[name], policy_name=name)
    if kind == "pydantic-ai":
        from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
        return PydanticAIRuntime(config)
    raise ValueError(f"unknown investigator runtime {kind!r}")
