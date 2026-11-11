"""The ``InvestigatorRuntime`` port and the deterministic fixture runtime.

A runtime turns a packed context into a *proposed* decision. It has no authority: the command
service validates and accepts the decision before any effect. Provider transport retries are
disabled in every adapter (one retry owner: Temporal); model-output repair is a separate,
recorded call.
"""

from __future__ import annotations

import json
import re
import threading
import time
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
        "control": {"type": "object", "properties": {"action": {"type": "string"}, "summary": {"type": "string"},
                                                     "scope": {"type": "object"}, "claim": {"type": "object"}}},
        "resource_policy": {"type": "object", "properties": {"max_concurrent_jobs": {"type": "integer", "minimum": 1}}},
    },
    "required": ["rationale", "calls"],
}


# Every model request of a decision carries an output cap: the configured ``max_output_tokens``, else this default
# (the live check's default). The runtime sends it and admits a request only if its input estimate plus this cap fits
# what is left of the decision's token envelope; the decide activity prices the same cap (ADR 0016).
DEFAULT_MAX_OUTPUT_TOKENS = 4096


def output_cap(settings: dict[str, Any]) -> int:
    """The output cap of each model request (``max_output_tokens`` or the default)."""
    return int(settings.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)


class DecisionAbort:
    """Why the decision in progress must end now, shared by the decide activity and the runtime (ADR 0017).

    The activity sets it (``trigger``) when the investigation stops or pauses, or when Temporal cancels the activity; the
    runtime checks it (``poll``) before it starts every provider request and every ``ABORT_POLL_SECONDS`` while one is
    in flight, and abandons the request (its connection is closed) once it is set. ``deadline`` (a ``clock`` reading)
    sets it to ``decision-deadline`` when it passes. Thread-safe: the activity's watcher thread triggers it while the
    runtime's event loop polls it."""

    def __init__(self, deadline: float | None = None, *, clock: Callable[[], float] = time.monotonic):
        self.deadline, self.clock = deadline, clock
        self.reason: str | None = None
        self._lock = threading.Lock()

    def trigger(self, reason: str) -> None:
        with self._lock:
            if self.reason is None:  # the first reason wins
                self.reason = reason

    def poll(self) -> str | None:
        if self.reason is None and self.deadline is not None and self.clock() >= self.deadline:
            self.trigger("decision-deadline")
        return self.reason


ABORT_POLL_SECONDS = 0.1  # how often an in-flight request checks its DecisionAbort


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
    abort: DecisionAbort | None = None  # set by the decide activity: stop, pause, cancellation, deadline


@dataclass
class DecisionResult:
    proposal: dict[str, Any]
    usage: dict[str, Any]
    raw_response: Any
    model_settings: dict[str, Any]
    prompt_revision: str


class ModelCallFailed(Exception):
    """A provider call failed. ``possible_usage`` is recorded as uncertain spend. ``failure`` (ADR 0023) classifies it:
    ``{"kind": "transient"|"quota-exhausted"|"permanent", "status": int|None, "retry_after_seconds": float|None}``."""

    def __init__(self, message: str, *, possible_usage: dict[str, Any] | None = None, retryable: bool = True,
                 failure: dict[str, Any] | None = None):
        super().__init__(message)
        self.possible_usage = possible_usage or {}
        self.failure = failure or {"kind": "transient" if retryable else "permanent", "status": None,
                                   "retry_after_seconds": None}
        self.retryable = retryable and self.failure.get("kind") != "quota-exhausted"


# -- provider failures (ADR 0023, recommendation 6) -------------------------------------------------------------------
# Quota exhaustion is not overload: retrying cannot succeed until the quota returns, so it stops at once and is recorded
# as provider-unavailable. MiniMax reports it as HTTP 429 with error 2056 ("usage limit exceeded"); other providers say
# "insufficient_quota", "exceeded your current quota" or "credit balance is too low".
QUOTA_EXHAUSTED = re.compile(r"\b2056\b|usage limit (?:exceeded|reached)|insufficient[_ ]quota|quota (?:has been |is )?(?:exceeded|exhausted)"
                             r"|exceeded your current quota|credit balance is too low|out of credits|billing hard limit",
                             re.IGNORECASE)
MAX_RETRY_AFTER_SECONDS = 600.0  # a Retry-After beyond this is not waited for within a turn


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in out and len(out) < 8:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _headers(exc: BaseException) -> Any:
    for e in _chain(exc):
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None) or getattr(e, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            return headers
    return None


def retry_after_seconds(exc: BaseException, *, now: float | None = None) -> float | None:
    """The provider's Retry-After for a failed request (``retry-after-ms``, or ``retry-after`` in seconds or as an HTTP
    date), from the exception or the SDK error it wraps; None when absent or unreadable."""
    headers = _headers(exc)
    if headers is None:
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None and float(ms) == float(ms):
            return min(max(0.0, float(ms) / 1000.0), MAX_RETRY_AFTER_SECONDS)
        value = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - an unusual header container: no hint
        return None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        return min(max(0.0, seconds), MAX_RETRY_AFTER_SECONDS) if seconds == seconds else None  # NaN: no hint
    from email.utils import parsedate_to_datetime
    try:
        when = parsedate_to_datetime(str(value)).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return min(max(0.0, when - (time.time() if now is None else now)), MAX_RETRY_AFTER_SECONDS)


def _status(exc: BaseException) -> int | None:
    for e in _chain(exc):
        for v in (getattr(e, "status_code", None), getattr(getattr(e, "response", None), "status_code", None)):
            if isinstance(v, int):
                return v
    return None


def provider_failure(exc: BaseException, retryable: bool) -> dict[str, Any]:
    """Classify a failed provider request: quota exhaustion (never retried), transient (retried, honouring the
    provider's Retry-After) or permanent. ``retryable`` is the transport classification of the error."""
    chain = _chain(exc)
    text = " ".join(f"{e} {getattr(e, 'body', '') or ''} {getattr(e, 'message', '') or ''}" for e in chain)
    status = _status(exc)
    # the runtime's own request/token limits (pydantic-ai UsageLimitExceeded) are never a provider's quota
    local_limit = any(type(e).__name__ == "UsageLimitExceeded" for e in chain)
    quota = not local_limit and bool(QUOTA_EXHAUSTED.search(text))
    kind = "quota-exhausted" if quota else ("transient" if retryable else "permanent")
    return {"kind": kind, "status": status, "retry_after_seconds": retry_after_seconds(exc),
            "error": type(exc).__name__}


class DecisionAborted(ModelCallFailed):
    """The decision ended because its ``DecisionAbort`` was set (``reason``). ``in_flight``: a provider request had been
    sent and was abandoned, so what it billed is unknown (recorded as uncertain usage, never as zero).
    ``abandoned``: that request's admitted worst case (input estimate, output cap), for the record."""

    def __init__(self, reason: str, *, in_flight: bool = False, abandoned: dict[str, Any] | None = None,
                 possible_usage: dict[str, Any] | None = None):
        super().__init__(f"decision aborted: {reason}", possible_usage=possible_usage, retryable=False)
        self.reason, self.in_flight, self.abandoned = reason, in_flight, abandoned


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
        # Nothing was launched, so there are no jobs to wait for: wait for the operator instead.
        return {"rationale": "wait for operator input", "calls": [],
                "control": {"action": "request_input", "question": "fixture: steer to continue"}}
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
