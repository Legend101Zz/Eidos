"""Provider resilience (ADR 0023, recommendation 6): provider failures are classified (quota exhaustion, transient with
the provider's Retry-After, permanent), quota exhaustion is never retried, and a Retry-After becomes the next retry's
delay (with jitter, bounded by the exploration time left before the conclusion's reserve) at every layer: the runtime
(``model_call_failed``), the decide activity (``model_failure_error``) and the coordinator (``turn_failure``,
``turn_retry_delay``).

Fixture models and plain exception classes only: the "provider" is a pair of exceptions chained like pydantic-ai's
``ModelHTTPError`` wrapping an SDK error. Nothing here calls a provider or a network endpoint."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from email.utils import formatdate
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from eidos.investigator import activities
from eidos.investigator.activities import (
    MAX_RETRY_DELAY_SECONDS,
    QUOTA_ERROR_TYPE,
    RETRY_JITTER_FRACTION,
    InvestigatorActivities,
    model_failure_error,
    retry_delay,
)
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime, classify_failure, model_call_failed
from eidos.investigator.runtime import (
    DecisionRequest,
    ModelCallFailed,
    provider_failure,
    retry_after_seconds,
)
from eidos.orchestration.workflows import MAX_FAILED_TURN_BACKOFF_SECONDS, turn_failure, turn_retry_delay

pytestmark = pytest.mark.fixture_llm

NOW = 1_800_000_000.0  # a fixed clock reading for Retry-After dates and the completion boundary

# MiniMax reports an exhausted usage quota as HTTP 429 with error 2056 in its response body.
MINIMAX_2056 = {"base_resp": {"status_code": 2056, "status_msg": "usage limit exceeded (2056)"}}


# -- a fake provider: an SDK error wrapped like pydantic-ai's ModelHTTPError ----------------------------------------------
class FakeResponse:
    def __init__(self, status_code: int | None, headers: dict[str, str] | None):
        self.status_code, self.headers = status_code, headers if headers is not None else {}


class FakeSDKError(Exception):
    """The provider SDK's status error: ``status_code``, ``body`` and the HTTP response with its headers."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code, self.body = status_code, body
        self.response = FakeResponse(status_code, headers)


class FakeModelHTTPError(Exception):
    """pydantic-ai's wrapper: the status and the body, but not the response headers."""

    def __init__(self, status_code: int, model_name: str, body: Any = None):
        super().__init__(f"status_code: {status_code}, model_name: {model_name}, body: {body}")
        self.status_code, self.model_name, self.body = status_code, model_name, body


def provider_error(status: int, body: Any = None, headers: dict[str, str] | None = None, *,
                   message: str = "upstream error") -> FakeModelHTTPError:
    """``FakeModelHTTPError`` raised ``from`` a ``FakeSDKError`` that carries the response headers."""
    try:
        try:
            raise FakeSDKError(message, status_code=status, body=body, headers=headers)
        except FakeSDKError as sdk:
            raise FakeModelHTTPError(status, "fake-model", body) from sdk
    except FakeModelHTTPError as wrapped:
        return wrapped


def quota_error(headers: dict[str, str] | None = None) -> FakeModelHTTPError:
    return provider_error(429, MINIMAX_2056, headers)


def overloaded_error(headers: dict[str, str] | None = None) -> FakeModelHTTPError:
    return provider_error(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
                          headers)


def failure_of(status: int, kind: str = "transient", retry_after: float | None = None) -> dict[str, Any]:
    return {"kind": kind, "status": status, "retry_after_seconds": retry_after}


# -- 1. classification: quota exhaustion is not overload -------------------------------------------------------------------
@pytest.mark.parametrize("body", [
    MINIMAX_2056,
    {"base_resp": {"status_code": 2056}},  # the code alone
    {"base_resp": {"status_msg": "usage limit exceeded for this plan"}},  # the words alone
    "Error 2056: usage limit exceeded",
])
def test_minimax_quota_429_is_quota_exhausted(body):
    exc = provider_error(429, body)
    failure = provider_failure(exc, classify_failure(exc))
    assert failure["kind"] == "quota-exhausted" and failure["status"] == 429


@pytest.mark.parametrize("body", [
    {"error": {"type": "insufficient_quota", "code": "insufficient_quota", "message": "check your plan"}},
    {"error": {"message": "You exceeded your current quota, please check your plan and billing details."}},
    {"error": {"message": "Your credit balance is too low to access the API."}},
])
def test_other_providers_quota_wordings_are_quota_exhausted(body):
    assert provider_failure(provider_error(429, body), True)["kind"] == "quota-exhausted"


def test_quota_text_is_found_in_a_chained_cause_not_only_in_the_outer_error():
    try:
        try:
            raise FakeSDKError("Error code: 429", status_code=429, body=MINIMAX_2056)
        except FakeSDKError as sdk:
            raise RuntimeError("model request failed") from sdk  # the outer error says nothing about quota
    except RuntimeError as outer:
        assert provider_failure(outer, True)["kind"] == "quota-exhausted"


@pytest.mark.parametrize("status, body", [
    (429, {"error": {"message": "Rate limit reached for requests, please retry later"}}),
    (429, "rate limit"),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
    (503, "service unavailable"),
])
def test_rate_limits_and_overload_are_transient_not_quota(status, body):
    exc = provider_error(status, body)
    failure = provider_failure(exc, classify_failure(exc))
    assert failure["kind"] == "transient" and failure["status"] == status


def test_pydantic_ais_own_usage_limit_error_is_not_provider_quota():
    from pydantic_ai.exceptions import UsageLimitExceeded
    exc = UsageLimitExceeded("The next request would exceed the request_limit of 1. Consider raising the limit, or "
                             "see the docs on usage limits for budget-aware patterns: "
                             "https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits")
    assert provider_failure(exc, classify_failure(exc))["kind"] != "quota-exhausted"


def test_a_non_retryable_transport_error_is_permanent():
    exc = provider_error(400, {"error": {"message": "invalid request"}})
    failure = provider_failure(exc, classify_failure(exc))
    assert failure["kind"] == "permanent" and failure["status"] == 400 and failure["retry_after_seconds"] is None


def test_provider_failure_is_json_serialisable_and_names_the_error():
    failure = provider_failure(quota_error({"retry-after": "30"}), True)
    assert failure == {"kind": "quota-exhausted", "status": 429, "retry_after_seconds": 30.0,
                       "error": "FakeModelHTTPError"}
    json.dumps(failure)  # it travels as the details of an ApplicationError


def test_model_call_failed_from_a_quota_failure_is_never_retryable():
    exc = quota_error()
    assert classify_failure(exc) is True  # by status alone a 429 looks retryable ...
    failed = ModelCallFailed("quota", retryable=classify_failure(exc), failure=provider_failure(exc, True))
    assert failed.failure["kind"] == "quota-exhausted" and failed.retryable is False  # ... but it cannot succeed


def test_quota_exhausted_forces_retryable_false_whatever_the_caller_says():
    assert ModelCallFailed("q", retryable=True, failure={"kind": "quota-exhausted"}).retryable is False


def test_model_call_failed_defaults_classify_by_retryable():
    assert ModelCallFailed("x").failure == {"kind": "transient", "status": None, "retry_after_seconds": None}
    assert ModelCallFailed("x").retryable is True
    assert ModelCallFailed("x", retryable=False).failure["kind"] == "permanent"
    assert ModelCallFailed("x", failure=failure_of(529, retry_after=5)).retryable is True


def test_real_pydantic_ai_and_sdk_errors_are_classified_too():
    """The same checks against the real ``ModelHTTPError`` wrapping a real SDK error built without a network."""
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from pydantic_ai.exceptions import ModelHTTPError
    request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
    sdk = openai.RateLimitError("Error code: 429", body=MINIMAX_2056,
                                response=httpx.Response(429, headers={"Retry-After": "9"}, request=request))
    try:
        try:
            raise sdk
        except openai.RateLimitError as inner:
            raise ModelHTTPError(429, "MiniMax-M3", MINIMAX_2056) from inner
    except ModelHTTPError as exc:
        failure = provider_failure(exc, classify_failure(exc))
        assert failure["kind"] == "quota-exhausted" and failure["status"] == 429
        assert failure["retry_after_seconds"] == 9.0  # httpx.Headers is case-insensitive: "Retry-After"


# -- 2. Retry-After ---------------------------------------------------------------------------------------------------------
def test_retry_after_in_seconds():
    assert retry_after_seconds(overloaded_error({"retry-after": "12"})) == 12.0
    assert retry_after_seconds(overloaded_error({"retry-after": "1.5"})) == 1.5
    assert retry_after_seconds(overloaded_error({"retry-after": " 7 "})) == 7.0


def test_retry_after_ms_is_read_and_preferred():
    assert retry_after_seconds(overloaded_error({"retry-after-ms": "2500"})) == 2.5
    assert retry_after_seconds(overloaded_error({"retry-after-ms": "2500", "retry-after": "60"})) == 2.5


def test_retry_after_as_an_http_date_is_relative_to_now():
    date = formatdate(NOW + 45, usegmt=True)
    assert retry_after_seconds(overloaded_error({"retry-after": date}), now=NOW) == 45.0
    assert retry_after_seconds(overloaded_error({"retry-after": formatdate(NOW - 45, usegmt=True)}), now=NOW) == 0.0


def test_retry_after_http_date_defaults_to_the_wall_clock():
    date = formatdate(time.time() + 120, usegmt=True)
    assert 110 <= retry_after_seconds(overloaded_error({"retry-after": date})) <= 120


def test_retry_after_is_found_on_a_chained_causes_response_headers():
    exc = overloaded_error({"retry-after": "30"})
    assert not hasattr(exc, "response")  # only the SDK error it wraps has the response
    assert exc.__cause__ is not None and retry_after_seconds(exc) == 30.0


def test_retry_after_is_found_further_down_the_chain_and_on_a_plain_headers_attribute():
    try:
        try:
            try:
                raise FakeSDKError("429", status_code=429, headers={"retry-after": "4"})
            except FakeSDKError as sdk:
                raise ValueError("transport") from sdk
        except ValueError as mid:
            raise RuntimeError("model request failed") from mid
    except RuntimeError as outer:
        assert retry_after_seconds(outer) == 4.0
    bare = Exception("x")
    bare.headers = {"retry-after": "6"}  # type: ignore[attr-defined]
    assert retry_after_seconds(bare) == 6.0


def test_retry_after_absent_is_none():
    assert retry_after_seconds(overloaded_error({})) is None
    assert retry_after_seconds(overloaded_error({"content-type": "application/json"})) is None
    assert retry_after_seconds(overloaded_error()) is None
    assert retry_after_seconds(ValueError("no response at all")) is None


@pytest.mark.parametrize("value", ["soon", "", "12 seconds", "Tue, 99 Foo 2026 00:00:00 GMT", "nan?"])
def test_retry_after_garbage_is_none(value):
    assert retry_after_seconds(overloaded_error({"retry-after": value}), now=NOW) is None


def test_retry_after_ms_garbage_is_none_and_does_not_fall_back_to_retry_after():
    assert retry_after_seconds(overloaded_error({"retry-after-ms": "soon", "retry-after": "5"})) is None


def test_retry_after_negative_clamps_to_zero():
    assert retry_after_seconds(overloaded_error({"retry-after": "-5"})) == 0.0
    assert retry_after_seconds(overloaded_error({"retry-after-ms": "-100"})) == 0.0


def test_retry_after_from_a_headers_container_that_raises_is_none():
    class Hostile:
        def get(self, key, default=None):
            raise RuntimeError("no headers for you")

    exc = FakeSDKError("429", status_code=429)
    exc.response.headers = Hostile()  # type: ignore[assignment]
    assert retry_after_seconds(exc) is None


# -- 3. retry_delay: the next attempt's delay ---------------------------------------------------------------------------------
def completion(*, left: float, reserve: float = 40.0, now: float = NOW) -> dict[str, Any]:
    """A conclusion boundary ``left`` seconds after ``now`` (``deadline_unix - conclude_threshold_seconds``)."""
    return {"deadline_unix": now + left + reserve, "conclude_threshold_seconds": reserve}


def test_retry_delay_is_none_without_a_retry_after():
    assert retry_delay(failure_of(529), None, now=NOW) is None
    assert retry_delay({}, None, now=NOW) is None
    assert retry_delay(failure_of(529), completion(left=100), now=NOW) is None  # Temporal's own backoff applies


def test_retry_delay_jitter_comes_from_rng_and_is_at_most_a_fifth():
    f = failure_of(529, retry_after=10.0)
    assert retry_delay(f, None, now=NOW, rng=lambda: 0.0) == 10.0  # exactly the provider's Retry-After
    assert retry_delay(f, None, now=NOW, rng=lambda: 1.0) == pytest.approx(10.0 * (1 + RETRY_JITTER_FRACTION))
    assert retry_delay(f, None, now=NOW, rng=lambda: 1.0) == 12.0
    assert retry_delay(f, None, now=NOW, rng=lambda: 0.5) == 11.0
    assert RETRY_JITTER_FRACTION == 0.2


def test_retry_delay_default_rng_stays_within_the_jitter_band():
    delays = {retry_delay(failure_of(529, retry_after=10.0), None, now=NOW) for _ in range(40)}
    assert all(10.0 <= d <= 12.0 for d in delays)
    assert len(delays) > 1  # it is jittered, not constant


def test_retry_delay_is_capped_at_the_maximum():
    big = failure_of(529, retry_after=MAX_RETRY_DELAY_SECONDS * 10)
    assert retry_delay(big, None, now=NOW, rng=lambda: 0.0) == MAX_RETRY_DELAY_SECONDS
    assert retry_delay(big, None, now=NOW, rng=lambda: 1.0) == MAX_RETRY_DELAY_SECONDS
    just_under = failure_of(529, retry_after=MAX_RETRY_DELAY_SECONDS - 1)
    assert retry_delay(just_under, None, now=NOW, rng=lambda: 1.0) == MAX_RETRY_DELAY_SECONDS  # jitter is capped too


def test_retry_delay_is_bounded_by_the_completion_boundary():
    f = failure_of(529, retry_after=30.0)
    assert retry_delay(f, completion(left=100), now=NOW, rng=lambda: 0.0) == 30.0  # time enough: not bounded
    assert retry_delay(f, completion(left=20), now=NOW, rng=lambda: 0.0) == 20.0  # bounded by the time left
    assert retry_delay(f, completion(left=35), now=NOW, rng=lambda: 1.0) == 35.0  # jittered 36 -> bounded to 35
    assert retry_delay(f, completion(left=30), now=NOW, rng=lambda: 0.0) == 30.0  # exactly at the boundary


def test_retry_delay_is_zero_at_or_past_the_boundary():
    f = failure_of(529, retry_after=30.0)
    assert retry_delay(f, completion(left=0), now=NOW, rng=lambda: 0.0) == 0.0
    assert retry_delay(f, completion(left=-50), now=NOW, rng=lambda: 1.0) == 0.0  # never negative
    # the boundary is deadline_unix - conclude_threshold_seconds, not the deadline: inside the reserve is past it
    inside_reserve = {"deadline_unix": NOW + 30, "conclude_threshold_seconds": 40}
    assert retry_delay(f, inside_reserve, now=NOW, rng=lambda: 0.0) == 0.0


def test_retry_delay_without_a_completion_is_not_bounded_and_with_one_uses_the_wall_clock_by_default():
    f = failure_of(529, retry_after=30.0)
    assert retry_delay(f, {}, now=NOW, rng=lambda: 0.0) == 30.0  # no completion in the spec
    future = completion(left=1000, now=time.time())
    assert retry_delay(f, future, rng=lambda: 0.0) == 30.0
    past = completion(left=-10, now=time.time())
    assert retry_delay(f, past, rng=lambda: 0.0) == 0.0


def test_retry_delay_of_a_zero_retry_after_is_zero_not_none():
    assert retry_delay(failure_of(529, retry_after=0.0), None, now=NOW) == 0.0


# -- 4. model_failure_error: the activity error --------------------------------------------------------------------------------
def test_quota_failure_becomes_a_typed_non_retryable_error():
    exc = ModelCallFailed("MiniMax 429 2056", retryable=True, failure=failure_of(429, "quota-exhausted", 30.0))
    err = model_failure_error(exc, completion(left=1000, now=time.time()))
    assert isinstance(err, ApplicationError) and err.type == QUOTA_ERROR_TYPE == "PROVIDER_QUOTA_EXHAUSTED"
    assert err.non_retryable is True
    assert err.details[0]["kind"] == "quota-exhausted" and err.details[0]["status"] == 429
    assert err.next_retry_delay is None  # waiting for a Retry-After cannot help: the quota has to return
    assert "provider quota exhausted" in str(err)


def test_transient_failure_with_a_retry_after_sets_the_next_retry_delay():
    exc = ModelCallFailed("overloaded", failure=failure_of(529, retry_after=7.0))
    err = model_failure_error(exc, None)
    assert err.type == "MODEL_CALL_FAILED" and err.non_retryable is False
    delay = err.details[0]["next_retry_delay_seconds"]
    assert 7.0 <= delay <= 7.0 * (1 + RETRY_JITTER_FRACTION)
    assert err.next_retry_delay == timedelta(seconds=delay)
    assert err.details[0]["kind"] == "transient" and err.details[0]["retry_after_seconds"] == 7.0
    json.dumps(err.details[0])


def test_transient_failure_retry_delay_is_bounded_by_the_completion_boundary():
    exc = ModelCallFailed("overloaded", failure=failure_of(529, retry_after=120.0))
    now = time.time()
    err = model_failure_error(exc, completion(left=30, now=now))
    delay = err.details[0]["next_retry_delay_seconds"]
    assert 0 < delay <= 30.0 and delay > 25.0  # the exploration time left (30 s minus what the call took)
    assert err.next_retry_delay == timedelta(seconds=delay)


def test_transient_failure_past_the_boundary_asks_for_no_delay():
    exc = ModelCallFailed("overloaded", failure=failure_of(529, retry_after=120.0))
    err = model_failure_error(exc, completion(left=-5, now=time.time()))
    assert err.details[0]["next_retry_delay_seconds"] == 0.0
    assert err.next_retry_delay is None  # no override: the turn's schedule-to-close bound then ends it as a deadline
    assert err.non_retryable is False


def test_transient_failure_without_a_retry_after_leaves_temporals_backoff():
    exc = ModelCallFailed("connection reset", failure=failure_of(None, retry_after=None))
    err = model_failure_error(exc, completion(left=1000, now=time.time()))
    assert err.type == "MODEL_CALL_FAILED" and err.non_retryable is False
    assert err.next_retry_delay is None and "next_retry_delay_seconds" not in err.details[0]


def test_permanent_failure_is_non_retryable_and_has_no_delay():
    exc = ModelCallFailed("not found", retryable=False, failure=failure_of(404, "permanent", retry_after=5.0))
    err = model_failure_error(exc, None)
    assert err.type == "MODEL_CALL_FAILED" and err.non_retryable is True
    assert err.next_retry_delay is None and "next_retry_delay_seconds" not in err.details[0]


def test_model_failure_error_does_not_mutate_the_failure_it_was_given():
    failure = failure_of(529, retry_after=7.0)
    exc = ModelCallFailed("overloaded", failure=failure)
    model_failure_error(exc, None)
    assert "next_retry_delay_seconds" not in failure and exc.failure == failure_of(529, retry_after=7.0)


# -- 5. the runtime: PydanticAIRuntime over a FunctionModel that raises the provider's error -------------------------------------
def req() -> DecisionRequest:
    return DecisionRequest(turn_id="t1", slot=0, context="# Investigation\nQuestion: why?", turn_so_far=[],
                           limits={"max_calls": 4}, read_tools={})


def raising(exc: BaseException):
    calls: list[int] = []

    def fn(messages, info: AgentInfo):
        calls.append(len(messages))
        raise exc
    return SimpleNamespace(model=FunctionModel(fn), calls=calls)


def runtime(script, mode: str = "tool", **cfg) -> PydanticAIRuntime:
    return PydanticAIRuntime({"_model_object": script.model, "model": "openai-compatible:MiniMax-M3",
                              "output_mode": mode, **cfg})


@pytest.mark.parametrize("mode", ["tool", "repair"])
def test_runtime_quota_failure_stops_at_once_and_is_recorded_as_quota_exhausted(mode):
    script = raising(quota_error({"retry-after": "30"}))
    with pytest.raises(ModelCallFailed) as e:
        runtime(script, mode).decide(req())
    assert len(script.calls) == 1  # the runtime made no second request of its own
    assert e.value.failure["kind"] == "quota-exhausted" and e.value.failure["status"] == 429
    assert e.value.retryable is False
    failed = e.value.possible_usage["failed_request"]
    assert failed["kind"] == "quota-exhausted" and failed["status_code"] == 429
    assert failed["error"] == "FakeModelHTTPError" and failed["retry_after_seconds"] == 30.0
    assert e.value.possible_usage["provider"] == "openai-compatible"


@pytest.mark.parametrize("mode", ["tool", "repair"])
def test_runtime_overload_with_retry_after_is_retryable_and_carries_the_hint(mode):
    script = raising(overloaded_error({"retry-after": "7"}))
    with pytest.raises(ModelCallFailed) as e:
        runtime(script, mode).decide(req())
    assert e.value.retryable is True
    assert e.value.failure == {"kind": "transient", "status": 529, "retry_after_seconds": 7.0,
                               "error": "FakeModelHTTPError"}
    assert e.value.possible_usage["failed_request"]["kind"] == "transient"
    assert e.value.possible_usage["failed_request"]["retry_after_seconds"] == 7.0


def test_runtime_plain_rate_limit_without_retry_after_is_transient_with_no_hint():
    script = raising(provider_error(429, {"error": {"message": "Rate limit reached"}}))
    with pytest.raises(ModelCallFailed) as e:
        runtime(script).decide(req())
    assert e.value.retryable is True and e.value.failure["kind"] == "transient"
    assert e.value.failure["retry_after_seconds"] is None


def test_runtime_client_error_is_permanent():
    script = raising(provider_error(400, {"error": {"message": "invalid request"}}))
    with pytest.raises(ModelCallFailed) as e:
        runtime(script).decide(req())
    assert e.value.retryable is False and e.value.failure["kind"] == "permanent"


def test_runtime_failure_after_a_billed_reply_reports_that_reply_as_possible_spend():
    n = {"calls": 0}

    def fn(messages, info: AgentInfo):
        n["calls"] += 1
        if n["calls"] == 1:  # a malformed reply, billed; the re-ask then hits the quota
            from pydantic_ai.usage import RequestUsage
            return ModelResponse(parts=[TextPart("not json")], usage=RequestUsage(input_tokens=11, output_tokens=7))
        raise quota_error()

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "openai-compatible:m",
                            "output_mode": "repair"})
    with pytest.raises(ModelCallFailed) as e:
        rt.decide(req())
    usage = e.value.possible_usage
    assert usage["calls"] == 1 and usage["input_tokens"] == 11 and usage["output_tokens"] == 7
    assert usage["failed_request"]["kind"] == "quota-exhausted"
    assert usage["structured_output"]["errors"]  # the malformed reply before the failure is not lost


def test_model_call_failed_helper_classifies_without_a_runtime():
    failed = model_call_failed(quota_error(), {"calls": 0})
    assert failed.retryable is False and failed.possible_usage["failed_request"]["kind"] == "quota-exhausted"
    failed = model_call_failed(overloaded_error({"retry-after-ms": "1500"}), {"calls": 0})
    assert failed.retryable is True and failed.failure["retry_after_seconds"] == 1.5


# -- 6. the decide activity (the lab and the runtime are faked, as in test_decide_cancel_through_accept) --------------------------
P, INV, TURN, SLOT = "proj_1", "inv_1", "turn_1", 0
ARGS = {"project_id": P, "investigation_id": INV, "turn_id": TURN, "slot": SLOT}
ATTEMPT = 3


class World:
    """The ordered log of every effectful call of one decide, with the payload and the key of each."""

    def __init__(self, completion: dict | None = None):
        self.log: list[str] = []
        self.payloads: dict[str, list[dict]] = {}
        self.keys: dict[str, list[str]] = {}
        self.cancelled = False
        self.turn = {"id": TURN, "status": "open", "decisions": [],
                     "context": {"context_artifact_id": "art_ctx", "source_manifest_artifact_id": "art_src"},
                     "spec": {"runtime": {"runtime": "fixture", "model": None, "settings": {"abort_poll_seconds": 0.02}},
                              "limits": {"deadline_seconds": 600, "max_model_calls": 4, "max_calls": 4},
                              **({"completion": completion} if completion else {})}}

    def record(self, name: str, payload: dict | None, key: str | None) -> None:
        self.log.append(name)
        self.payloads.setdefault(name, []).append(payload or {})
        self.keys.setdefault(name, []).append(key or "")


class FakeWorkflowClient:
    def __init__(self, w: World):
        self.w = w

    def command(self, name: str, payload: dict | None = None, *, project: str | None = None, key: str | None = None,
                **_: Any) -> dict:
        self.w.record(name, payload, key)
        if name == "begin_decision":
            return {"result": {"epoch": 1}}
        if name == "reserve_model_budget":
            return {"result": {"reserved": True, "reservation_ref": "res_1"}}
        return {"result": {}}

    def query(self, name: str, params: dict | None = None, *, project: str | None = None) -> Any:
        if name == "get_turn":
            return self.w.turn
        if name == "get_record" and (params or {}).get("kind") == "investigation":
            return {"status": "active"}
        raise AssertionError(f"unexpected workflow query {name!r}")


class FakeInvestigatorClient:
    def __init__(self, w: World):
        self.w = w

    def download(self, project: str, artifact_id: str, dest: Any) -> None:
        text = {"art_ctx": "# Investigation\nQuestion: why?", "art_src": json.dumps({"view": "v"})}[artifact_id]
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)

    def upload(self, *a: Any, **kw: Any) -> dict:
        self.w.record("upload", {}, kw.get("key"))
        return {"result": {"artifact_id": "art_resp"}, "replayed": False}

    def command(self, name: str, payload: dict | None = None, **kw: Any) -> dict:
        self.w.record(name, payload, kw.get("key"))
        return {"result": {}, "replayed": False}

    def query(self, name: str, params: dict | None = None, **_: Any) -> Any:
        raise AssertionError(f"decide must not use the investigator query {name!r} here")


class FailingRuntime:
    """Stands in for ``build_runtime(cfg)``: after ``spend`` seconds of "provider call" it raises ``raises``."""

    def __init__(self, raises: Exception, spend: float = 0.0):
        self.raises, self.spend = raises, spend

    def decide(self, request: DecisionRequest):
        if self.spend:
            time.sleep(self.spend)
        raise self.raises


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    monkeypatch.setattr(activities.activity, "info",
                        lambda: SimpleNamespace(attempt=ATTEMPT, workflow_run_id="run_1", activity_id="act_1"))
    monkeypatch.setattr(activities.activity, "is_cancelled", lambda: w.cancelled)
    return w


def decide_with(world: World, monkeypatch, runtime: Any) -> InvestigatorActivities:
    monkeypatch.setattr(activities, "build_runtime", lambda cfg: runtime)
    return InvestigatorActivities(FakeInvestigatorClient(world), FakeWorkflowClient(world))


def failed_decide(acts: InvestigatorActivities) -> ApplicationError:
    with pytest.raises(ApplicationError) as err:
        acts.decide(dict(ARGS))
    return err.value


def test_decide_quota_failure_raises_the_typed_non_retryable_error_and_records_the_usage(world, monkeypatch):
    exc = ModelCallFailed("MiniMax 429 2056", retryable=True, failure=provider_failure(quota_error(), True),
                          possible_usage={"provider": "minimax", "input_tokens": 120, "output_tokens": 0, "calls": 1})
    acts = decide_with(world, monkeypatch, FailingRuntime(exc, spend=0.05))
    err = failed_decide(acts)
    assert err.type == "PROVIDER_QUOTA_EXHAUSTED" and err.non_retryable is True
    assert err.details[0]["kind"] == "quota-exhausted" and err.details[0]["status"] == 429
    assert err.__cause__ is exc
    # the failed attempt's spend is recorded (as uncertain) and the reservation settled; nothing is proposed or accepted
    assert world.log == ["begin_decision", "reserve_model_budget", "record_model_usage"]
    (recorded,) = world.payloads["record_model_usage"]
    usage = recorded["usage"]
    assert usage["uncertain"] is True and usage["attempt"] == ATTEMPT
    assert usage["elapsed_seconds"] >= 0.05 and isinstance(usage["elapsed_seconds"], float)
    assert usage["failure"]["kind"] == "quota-exhausted" and usage["failure"] == err.details[0]
    assert usage["error"] == str(exc) and usage["input_tokens"] == 120 and usage["provider"] == "minimax"
    assert recorded["reserved"] is True and recorded["reservation_ref"] == "res_1"
    assert world.keys["record_model_usage"] == [f"usage-fail:{TURN}:{SLOT}:{ATTEMPT}:1"]


def test_decide_transient_failure_carries_the_retry_after_bounded_by_the_boundary(world, monkeypatch):
    now = time.time()
    world.turn["spec"]["completion"] = completion(left=30, now=now)
    failure = provider_failure(overloaded_error({"retry-after": "120"}), True)
    exc = ModelCallFailed("overloaded", failure=failure, possible_usage={"calls": 1})
    err = failed_decide(decide_with(world, monkeypatch, FailingRuntime(exc)))
    assert err.type == "MODEL_CALL_FAILED" and err.non_retryable is False
    delay = err.details[0]["next_retry_delay_seconds"]
    assert err.next_retry_delay == timedelta(seconds=delay)
    boundary_left = world.turn["spec"]["completion"]["deadline_unix"] \
        - world.turn["spec"]["completion"]["conclude_threshold_seconds"] - now
    assert 0 < delay <= boundary_left + 0.001  # never later than the start of the conclusion's reserve (3-decimal rounding)
    usage = world.payloads["record_model_usage"][0]["usage"]
    assert usage["uncertain"] is True and usage["attempt"] == ATTEMPT
    assert usage["failure"]["kind"] == "transient" and usage["failure"]["retry_after_seconds"] == 120.0
    assert usage["failure"]["next_retry_delay_seconds"] == delay  # the record says what the retry waited for


def test_decide_transient_failure_without_a_completion_waits_at_least_the_retry_after(world, monkeypatch):
    exc = ModelCallFailed("overloaded", failure=provider_failure(overloaded_error({"retry-after": "7"}), True))
    err = failed_decide(decide_with(world, monkeypatch, FailingRuntime(exc)))
    delay = err.details[0]["next_retry_delay_seconds"]
    assert 7.0 <= delay <= 8.4 and err.next_retry_delay == timedelta(seconds=delay)


def test_decide_transient_failure_past_the_boundary_records_zero_delay_and_sets_no_override(world, monkeypatch):
    world.turn["spec"]["completion"] = completion(left=-10, now=time.time())
    exc = ModelCallFailed("overloaded", failure=provider_failure(overloaded_error({"retry-after": "30"}), True))
    err = failed_decide(decide_with(world, monkeypatch, FailingRuntime(exc)))
    assert err.details[0]["next_retry_delay_seconds"] == 0.0 and err.next_retry_delay is None
    assert world.payloads["record_model_usage"][0]["usage"]["failure"]["next_retry_delay_seconds"] == 0.0


def test_decide_permanent_failure_is_non_retryable_and_still_recorded(world, monkeypatch):
    exc = ModelCallFailed("not found", retryable=False,
                          failure=provider_failure(provider_error(404, {"error": "no such model"}), False))
    err = failed_decide(decide_with(world, monkeypatch, FailingRuntime(exc)))
    assert err.type == "MODEL_CALL_FAILED" and err.non_retryable is True and err.next_retry_delay is None
    usage = world.payloads["record_model_usage"][0]["usage"]
    assert usage["uncertain"] is True and usage["failure"]["kind"] == "permanent"


def test_decide_activity_with_the_real_runtime_over_a_quota_error(world, monkeypatch):
    """End to end: the real ``PydanticAIRuntime`` over a fixture model that raises the quota error."""
    script = raising(quota_error({"retry-after": "30"}))
    rt = runtime(script)
    err = failed_decide(decide_with(world, monkeypatch, rt))
    assert err.type == "PROVIDER_QUOTA_EXHAUSTED" and err.non_retryable is True
    usage = world.payloads["record_model_usage"][0]["usage"]
    assert usage["failure"]["kind"] == "quota-exhausted" and usage["failed_request"]["kind"] == "quota-exhausted"
    assert usage["uncertain"] is True and usage["attempt"] == ATTEMPT and usage["elapsed_seconds"] >= 0
    assert len(script.calls) == 1 and "accept_decision" not in world.log


# -- 7. the coordinator: turn_failure and turn_retry_delay -------------------------------------------------------------------------
def activity_error(cause: BaseException | None, retry_state: RetryState | None, activity_type: str = "decide"):
    exc = ActivityError("Activity task failed", scheduled_event_id=5, started_event_id=6, identity="worker@test",
                        activity_type=activity_type, activity_id="1", retry_state=retry_state)
    exc.__cause__ = cause  # what temporalio sets when it converts the failure proto
    return exc


def test_turn_failure_carries_the_provider_failure_class_and_retry_after():
    cause = ApplicationError("x", {"kind": "transient", "status": 529, "retry_after_seconds": 30},
                             type="MODEL_CALL_FAILED")
    f = turn_failure(activity_error(cause, RetryState.MAXIMUM_ATTEMPTS_REACHED))
    assert f["provider"] == {"kind": "transient", "status": 529, "retry_after_seconds": 30}
    assert f["error_type"] == "MODEL_CALL_FAILED" and f["retryable"] is True and f["message"] == "x"
    json.dumps(f)  # it travels in activity inputs and workflow results


def test_turn_failure_for_quota_exhaustion_is_not_retryable_and_names_the_provider_failure():
    cause = ApplicationError("provider quota exhausted", failure_of(429, "quota-exhausted"),
                             type="PROVIDER_QUOTA_EXHAUSTED", non_retryable=True)
    f = turn_failure(activity_error(cause, RetryState.NON_RETRYABLE_FAILURE))
    assert f["error_type"] == "PROVIDER_QUOTA_EXHAUSTED" and f["retryable"] is False
    assert f["provider"] == {"kind": "quota-exhausted", "status": 429, "retry_after_seconds": None}


def test_turn_failure_provider_copies_only_the_three_keys_and_tolerates_missing_ones():
    cause = ApplicationError("x", {"kind": "transient", "error": "FakeModelHTTPError", "next_retry_delay_seconds": 9.0},
                             type="MODEL_CALL_FAILED")
    f = turn_failure(activity_error(cause, RetryState.MAXIMUM_ATTEMPTS_REACHED))
    assert f["provider"] == {"kind": "transient", "status": None, "retry_after_seconds": None}


def test_turn_failure_without_provider_details_has_no_provider_entry():
    for cause in (ApplicationError("upstream 503", type="MODEL_CALL_FAILED"),  # no details
                  ApplicationError("x", {"status": 529}, type="MODEL_CALL_FAILED"),  # details without a kind
                  ApplicationError("x", "not a dict", type="MODEL_CALL_FAILED"),
                  None):
        assert "provider" not in turn_failure(activity_error(cause, RetryState.MAXIMUM_ATTEMPTS_REACHED))


def test_the_activity_error_round_trips_into_turn_failure():
    """What ``model_failure_error`` raises is exactly what ``turn_failure`` reads."""
    exc = ModelCallFailed("overloaded", failure=provider_failure(overloaded_error({"retry-after": "30"}), True))
    f = turn_failure(activity_error(model_failure_error(exc, None), RetryState.MAXIMUM_ATTEMPTS_REACHED))
    assert f["provider"] == {"kind": "transient", "status": 529, "retry_after_seconds": 30.0}
    quota = ModelCallFailed("q", failure=provider_failure(quota_error(), True))
    f = turn_failure(activity_error(model_failure_error(quota, None), RetryState.NON_RETRYABLE_FAILURE))
    assert f["error_type"] == "PROVIDER_QUOTA_EXHAUSTED" and f["provider"]["kind"] == "quota-exhausted"
    assert f["retryable"] is False


def retry_failure(after: float | None) -> dict[str, Any]:
    return {"error_type": "MODEL_CALL_FAILED", "retryable": True,
            "provider": {"kind": "transient", "status": 529, "retry_after_seconds": after}}


def test_turn_retry_delay_is_the_base_without_a_retry_after():
    assert turn_retry_delay(60, {"error_type": "x", "retryable": True}, {}, NOW, 0.0) == 60.0
    assert turn_retry_delay(60, retry_failure(None), {}, NOW, 0.0) == 60.0
    assert turn_retry_delay(60, retry_failure(0), {}, NOW, 0.0) == 60.0


def test_turn_retry_delay_honours_the_larger_of_base_and_retry_after():
    assert turn_retry_delay(60, retry_failure(120), {}, NOW, 0.0) == 120.0  # the provider asks for longer
    assert turn_retry_delay(60, retry_failure(10), {}, NOW, 0.0) == 60.0  # the base backoff is longer
    assert turn_retry_delay(60, retry_failure(60), {}, NOW, 0.0) == 60.0


def test_turn_retry_delay_adds_at_most_a_fifth_of_jitter():
    assert turn_retry_delay(60, retry_failure(120), {}, NOW, 1.0) == pytest.approx(144.0)
    assert turn_retry_delay(60, retry_failure(120), {}, NOW, 0.5) == pytest.approx(132.0)
    for jitter in (0.0, 0.1, 0.25, 0.5, 0.75, 0.999):
        d = turn_retry_delay(60, retry_failure(100), {}, NOW, jitter)
        assert 100.0 <= d <= 120.0


def test_turn_retry_delay_is_capped_at_the_maximum_failed_turn_backoff():
    cap = float(MAX_FAILED_TURN_BACKOFF_SECONDS)
    assert turn_retry_delay(60, retry_failure(5000), {}, NOW, 0.0) == cap
    assert turn_retry_delay(60, retry_failure(5000), {}, NOW, 1.0) == cap
    assert turn_retry_delay(2000, retry_failure(None), {}, NOW, 1.0) == cap
    assert turn_retry_delay(800, retry_failure(None), {}, NOW, 1.0) == cap  # 960 with jitter, capped


def test_turn_retry_delay_never_passes_the_reserve_boundary():
    c = completion(left=400, now=NOW)
    assert turn_retry_delay(60, retry_failure(600), c, NOW, 0.0) == 400.0  # 600 -> the exploration time left
    assert turn_retry_delay(60, retry_failure(100), c, NOW, 0.0) == 100.0  # time enough: not bounded
    assert turn_retry_delay(60, retry_failure(390), c, NOW, 1.0) == pytest.approx(400.0)  # 468 -> bounded
    # the boundary is the start of the reserve, not the deadline itself
    assert turn_retry_delay(60, retry_failure(None), {"deadline_unix": NOW + 100, "conclude_threshold_seconds": 70},
                            NOW, 0.0) == 30.0


def test_turn_retry_delay_is_never_negative():
    assert turn_retry_delay(60, retry_failure(30), completion(left=0, now=NOW), NOW, 0.0) == 0.0
    assert turn_retry_delay(60, retry_failure(30), completion(left=-500, now=NOW), NOW, 1.0) == 0.0
    assert turn_retry_delay(-5, retry_failure(None), {}, NOW, 0.0) == 0.0
    assert turn_retry_delay(0, retry_failure(None), {}, NOW, 0.5) == 0.0
