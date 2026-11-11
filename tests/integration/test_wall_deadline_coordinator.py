"""ADR 0020, the wall deadline on a Temporal development server: a quiet wait ends when the conclusion's reserve begins, a
retry's backoff and the decide retries stay within the remaining wall time, and the deadline ends the coordinator."""

from __future__ import annotations

import asyncio
import time

import pytest
from temporalio import activity
from test_failed_turn_policy import Scripted, env, running, until  # noqa: F401 - env is a fixture

temporal = pytest.mark.temporal


def completion(deadline: float, threshold: float) -> dict:
    remaining = deadline - time.time()
    return {"deadline_unix": deadline, "remaining_seconds": remaining, "conclude_threshold_seconds": threshold,
            "conclude_now": remaining <= threshold}


class WallScripted(Scripted):
    """``Scripted`` with a wall deadline in every begun turn, and a quiet ledger (``check_news`` finds nothing)."""

    def __init__(self, script: dict | None, stop_after: int, deadline: float, threshold: float, reason: str | None = None):
        super().__init__(script, stop_after)
        self.deadline, self.threshold, self.reason = deadline, threshold, reason

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        if self.reason:
            return {"started": False, "reason": self.reason}
        r = await Scripted.begin_turn(self, a)
        return {**r, "completion": completion(self.deadline, self.threshold)} if r.get("started") else r

    @activity.defn(name="check_news")
    async def check_news(self, a: dict) -> dict:
        return {"news": False}


@temporal
async def test_a_quiet_wait_ends_when_the_reserve_begins(env):  # noqa: F811
    now = time.time()
    fake = WallScripted(None, stop_after=2, deadline=now + 62, threshold=60)  # the reserve begins in about 2 s
    began = time.monotonic()
    async with running(env, fake, idle_poll_seconds=120) as (h, _):  # the poll alone would wait 2 minutes
        await until(lambda: 2 in fake.started, 30, "a turn at the start of the reserve, without news")
        elapsed = time.monotonic() - began
        result = await asyncio.wait_for(h.result(), 30)
    assert elapsed < 30
    assert result["reason"] == "max-turns-reached"


@temporal
async def test_retries_and_backoff_stay_within_the_wall_time(env):  # noqa: F811
    fake = WallScripted({1: "retryable"}, stop_after=2, deadline=time.time() + 3, threshold=0.5)
    async with running(env, fake, failed_turn_backoff_seconds=100) as (h, _):
        await until(lambda: fake.retries, 30, "the recorded retry")
        await until(lambda: 2 in fake.started, 30, "the retry turn")
        await asyncio.wait_for(h.result(), 30)
    # the MODEL policy's third attempt starts 6 s in, past the deadline
    assert fake.decide_attempts[1] <= 2
    assert fake.retries[0]["delay_seconds"] <= 3


@temporal
async def test_the_wall_deadline_ends_the_coordinator_and_surfaces_it(env):  # noqa: F811
    fake = WallScripted(None, stop_after=5, deadline=time.time() - 1, threshold=1, reason="wall-deadline-reached")
    async with running(env, fake) as (h, _):
        result = await asyncio.wait_for(h.result(), 30)
    assert result["status"] == "finished" and result["reason"] == "wall-deadline-reached"
    assert any("wall-deadline-reached" in str(m.get("reason")) for m in fake.marks)
