"""ADR 0023 on a Temporal development server (tests only): provider resilience refines the existing retry owners.

* Quota exhaustion (``PROVIDER_QUOTA_EXHAUSTED``, non-retryable) is not retried by the activity policy and gets no
  retry turn: the coordinator marks the investigation needs-input with code ``provider-unavailable`` at once.
* A transient failure whose error carries the provider's Retry-After (``next_retry_delay``) is retried by Temporal after
  that delay, and the turn retry's recorded backoff is at least the Retry-After.

Activities are the scripted fakes of ``test_failed_turn_policy``; no model is called."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from test_failed_turn_policy import Scripted, env, running, until  # noqa: F401 - env is a fixture

temporal = pytest.mark.temporal


class Provider(Scripted):
    """``quota``: the decide activity fails as the real one does on exhausted quota; ``retry-after``: a transient
    failure with the provider's Retry-After as the next retry's delay, every attempt."""

    def __init__(self, script: dict[int, str], stop_after: int = 1000, retry_after: float = 2.0):
        super().__init__(script, stop_after)
        self.retry_after = retry_after
        self.attempt_times: dict[int, list[float]] = {}

    @activity.defn(name="decide")
    async def decide(self, a: dict) -> dict:
        index = int(a["turn_id"][1:])
        mode = self.script.get(index, "ok")
        self.attempt_times.setdefault(index, []).append(time.monotonic())
        self.decide_attempts[index] = self.decide_attempts.get(index, 0) + 1
        if mode == "quota":
            raise ApplicationError("provider quota exhausted: HTTP 429 error 2056 usage limit exceeded",
                                   {"kind": "quota-exhausted", "status": 429, "retry_after_seconds": None},
                                   type="PROVIDER_QUOTA_EXHAUSTED", non_retryable=True)
        if mode == "retry-after":
            raise ApplicationError("MODEL_CALL_FAILED: 529 overloaded",
                                   {"kind": "transient", "status": 529, "retry_after_seconds": self.retry_after},
                                   type="MODEL_CALL_FAILED",
                                   next_retry_delay=timedelta(seconds=self.retry_after))
        return await super().decide(a)

    def acts(self):
        return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn, self.enter_finalization,
                self.reconcile_finalization, self.check_news, self.mark_needs_input, self.record_turn_retry]


@temporal
async def test_quota_exhaustion_is_recorded_as_provider_unavailable_without_any_retry(env):  # noqa: F811
    fake = Provider({1: "quota"}, stop_after=2)
    async with running(env, fake) as (h, _):
        await until(lambda: fake.marks, 20, "the needs-input mark")
        (mark,) = fake.marks
        assert mark["code"] == "provider-unavailable"
        assert mark["reason"].startswith("turn failed (provider quota exhausted): PROVIDER_QUOTA_EXHAUSTED")
        failure = mark["details"]["failure"]
        assert failure["error_type"] == "PROVIDER_QUOTA_EXHAUSTED" and failure["retryable"] is False
        assert failure["provider"]["kind"] == "quota-exhausted"
        assert fake.decide_attempts[1] == 1  # the activity policy did not retry it
        await asyncio.sleep(2)
        assert fake.retries == [] and fake.started == [1]  # no retry turn
        assert fake.completed_turn(1)["stop_reason"] == "error"


@temporal
async def test_a_transient_failure_waits_for_the_providers_retry_after(env):  # noqa: F811
    fake = Provider({1: "retry-after"}, stop_after=2, retry_after=3.0)
    async with running(env, fake, failed_turn_backoff_seconds=1) as (h, _):
        result = await asyncio.wait_for(h.result(), 90)
    assert result["status"] == "finished"
    times = fake.attempt_times[1]
    assert len(times) == 3  # the MODEL policy's attempts, unchanged
    assert all(b - a >= 2.9 for a, b in zip(times, times[1:], strict=False))  # the Retry-After, not 2 s then 4 s
    (retry,) = fake.retries
    assert retry["failure"]["provider"]["retry_after_seconds"] == 3.0
    assert 3.0 <= retry["delay_seconds"] <= 3.6  # at least the Retry-After (base 1 s), with at most 20 % jitter
    assert fake.started == [1, 2]
