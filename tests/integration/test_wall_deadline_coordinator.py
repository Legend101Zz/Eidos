"""ADR 0020, the wall deadline on a Temporal development server: a quiet wait ends when the conclusion's reserve begins, a
turn (retries and hung decisions included) ends at the reserve and a fresh conclusion turn starts there, and the deadline
ends the coordinator."""

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
        if not r.get("started"):
            return r
        # as persisted: the first begin of a turn fixes its completion state, as the lab's turn spec does
        self.begun[a["index"]] = {**r, "completion": r.get("completion") or completion(self.deadline, self.threshold)}
        return self.begun[a["index"]]

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
async def test_a_retry_that_would_cross_the_reserve_ends_the_turn_and_a_fresh_turn_starts(env):  # noqa: F811
    fake = WallScripted({1: "retryable"}, stop_after=2, deadline=time.time() + 3, threshold=0.5)
    async with running(env, fake, failed_turn_backoff_seconds=100) as (h, _):
        await until(lambda: 2 in fake.started, 30, "the fresh turn at the reserve")
        await asyncio.wait_for(h.result(), 30)
    # the MODEL policy's third attempt would start 6 s in, past the boundary at 2.5 s
    assert fake.decide_attempts[1] <= 2
    assert fake.completed_turn(1)["stop_reason"] == "deadline"
    assert fake.retries == []  # not a failed turn: no backoff, no retry bookkeeping


@temporal
async def test_a_hung_decision_ends_at_the_reserve_and_the_next_turn_is_the_conclusion(env):  # noqa: F811
    fake = WallScripted({1: "hang"}, stop_after=2, deadline=time.time() + 62.5, threshold=60)  # the reserve begins in 2.5 s
    began = time.monotonic()
    async with running(env, fake) as (h, _):
        await until(lambda: 2 in fake.started, 30, "the conclusion turn")
        elapsed = time.monotonic() - began
        await asyncio.wait_for(h.result(), 30)
    assert elapsed < 9  # the hang lasts 10 s; the model timeout and the MODEL retries would run far longer
    assert fake.completed_turn(1)["stop_reason"] == "deadline" and not fake.completed_turn(1).get("failure")
    assert fake.begun[2]["completion"]["conclude_now"] is True  # a fresh state, not the first turn's snapshot


@temporal
async def test_the_wall_deadline_ends_the_coordinator_and_surfaces_it(env):  # noqa: F811
    fake = WallScripted(None, stop_after=5, deadline=time.time() - 1, threshold=1, reason="wall-deadline-reached")
    async with running(env, fake) as (h, _):
        result = await asyncio.wait_for(h.result(), 30)
    assert result["status"] == "finished" and result["reason"] == "wall-deadline-reached"
    assert any("wall-deadline-reached" in str(m.get("reason")) for m in fake.marks)
