"""R14 finding #5: provider retries and their activity lifetime are bounded by the exploration deadline, not by the
conclusion's reserve, and a retry whose requested delay (Retry-After) cannot fit before that deadline is stopped rather
than clamped to a pointless shorter wait.

``completion["exploration_deadline_unix"]`` is what ``begin_turn`` records for an exploratory turn: where the
reproduction hold-back begins. Without it (a finalize or concluding turn, an older turn spec) the boundary is the start
of the conclusion's reserve, ``deadline_unix - conclude_threshold_seconds``, as before.

Plain dicts and fixture exceptions only: nothing here calls a provider, a database or the workflow engine."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from eidos.domain.turns import completion_state, turn_boundary
from eidos.investigator.activities import (
    MAX_RETRY_DELAY_SECONDS,
    RETRY_JITTER_FRACTION,
    model_failure_error,
    retry_boundary,
    retry_delay,
    retry_fits,
)
from eidos.investigator.runtime import ModelCallFailed
from eidos.orchestration.workflows import turn_retry_delay

pytestmark = pytest.mark.fixture_llm

NOW = 1_800_000_000.0
RESERVE = 40.0  # conclude_threshold_seconds
STOPPED = "retry-after-exceeds-exploration-time"


def exploring(*, exploration_left: float, conclusion_left: float = 5000.0, now: float = NOW) -> dict[str, Any]:
    """An exploratory turn's completion: the reproduction hold-back begins ``exploration_left`` seconds after ``now``;
    the conclusion's reserve (``conclusion_left`` seconds after ``now``) is much later, as in the reviewer's example."""
    return {"deadline_unix": now + conclusion_left + RESERVE, "conclude_threshold_seconds": RESERVE,
            "conclude_now": False, "exploration_deadline_unix": now + exploration_left}


def conclusion_only(*, left: float, now: float = NOW) -> dict[str, Any]:
    """A completion without an exploration deadline: the boundary is the start of the conclusion's reserve."""
    return {"deadline_unix": now + left + RESERVE, "conclude_threshold_seconds": RESERVE, "conclude_now": False}


def transient(retry_after: float | None) -> dict[str, Any]:
    return {"kind": "transient", "status": 529, "retry_after_seconds": retry_after}


def overloaded(retry_after: float | None) -> ModelCallFailed:
    return ModelCallFailed("overloaded", failure=transient(retry_after))


# -- retry_boundary -----------------------------------------------------------------------------------------------------------
def test_retry_boundary_is_the_exploration_deadline_when_the_turn_has_one():
    assert retry_boundary(exploring(exploration_left=10)) == NOW + 10
    # the exploration deadline wins whatever the conclusion's boundary is
    assert retry_boundary({**exploring(exploration_left=10), "conclude_threshold_seconds": 3000.0}) == NOW + 10


def test_retry_boundary_without_an_exploration_deadline_is_the_start_of_the_conclusions_reserve():
    assert retry_boundary(conclusion_only(left=100)) == NOW + 100
    assert retry_boundary({"deadline_unix": NOW + 500, "conclude_threshold_seconds": 120.0}) == NOW + 380
    # an explicit None is the same as absent
    assert retry_boundary({**conclusion_only(left=100), "exploration_deadline_unix": None}) == NOW + 100


# -- retry_fits -----------------------------------------------------------------------------------------------------------------
def test_retry_fits_compares_the_retry_after_with_the_exploration_time_left():
    c = exploring(exploration_left=10)
    assert retry_fits(transient(5.0), c, now=NOW) is True
    assert retry_fits(transient(10.0), c, now=NOW) is True  # exactly what is left still fits
    assert retry_fits(transient(10.5), c, now=NOW) is False
    assert retry_fits(transient(200.0), c, now=NOW) is False
    assert retry_fits(transient(5.0), c, now=NOW + 6) is False  # 4 s left by then
    assert retry_fits(transient(5.0), c, now=NOW + 20) is False  # the deadline has passed


def test_retry_fits_without_a_retry_after_or_a_completion_is_true():
    assert retry_fits(transient(None), exploring(exploration_left=-5), now=NOW) is True  # nothing was asked for
    assert retry_fits(transient(200.0), None, now=NOW) is True
    assert retry_fits(transient(200.0), {}, now=NOW) is True


def test_retry_fits_without_an_exploration_deadline_uses_the_conclusions_boundary():
    c = conclusion_only(left=100)
    assert retry_fits(transient(100.0), c, now=NOW) is True
    assert retry_fits(transient(100.5), c, now=NOW) is False


def test_retry_fits_defaults_to_the_wall_clock():
    assert retry_fits(transient(5.0), exploring(exploration_left=60, now=time.time())) is True
    assert retry_fits(transient(5.0), exploring(exploration_left=1, now=time.time())) is False


# -- retry_delay still clamps a delay that fits (jitter can carry it past the boundary) -----------------------------------------
def test_retry_delay_is_clamped_to_the_exploration_deadline():
    f = transient(9.0)
    c = exploring(exploration_left=10)
    assert retry_delay(f, c, now=NOW, rng=lambda: 0.0) == 9.0
    assert retry_delay(f, c, now=NOW, rng=lambda: 1.0) == 10.0  # 9 s + 20 % = 10.8 s, clamped to the 10 s left
    assert retry_delay(f, exploring(exploration_left=-5), now=NOW, rng=lambda: 0.0) == 0.0
    # not the conclusion's boundary, which is far later
    assert retry_delay(transient(100.0), c, now=NOW, rng=lambda: 0.0) == 10.0


def test_retry_delay_without_an_exploration_deadline_is_clamped_to_the_conclusions_boundary():
    f = transient(30.0)
    assert retry_delay(f, conclusion_only(left=20), now=NOW, rng=lambda: 0.0) == 20.0
    assert retry_delay(f, conclusion_only(left=100), now=NOW, rng=lambda: 0.0) == 30.0


# -- model_failure_error: the reviewer's example --------------------------------------------------------------------------------
def test_a_retry_after_of_200_seconds_with_10_seconds_of_exploration_left_stops_the_retry():
    # exploration_deadline_unix = now + 10; the conclusion's boundary is far later and would have allowed the wait
    now = time.time()
    c = exploring(exploration_left=10, now=now)
    err = model_failure_error(overloaded(200.0), c)
    assert isinstance(err, ApplicationError) and err.type == "MODEL_CALL_FAILED"
    assert err.non_retryable is True
    assert err.details[0]["retry_stopped"] == STOPPED
    assert "next_retry_delay_seconds" not in err.details[0]
    assert err.next_retry_delay is None
    assert err.details[0]["kind"] == "transient" and err.details[0]["retry_after_seconds"] == 200.0
    # the same failure would have been retried, after the full wait, had the exploration deadline not been there
    without = {k: v for k, v in c.items() if k != "exploration_deadline_unix"}
    old = model_failure_error(overloaded(200.0), without)
    assert old.non_retryable is False and old.details[0]["next_retry_delay_seconds"] >= 200.0


def test_a_retry_after_of_5_seconds_with_10_seconds_of_exploration_left_is_retried_within_it():
    now = time.time()
    c = exploring(exploration_left=10, now=now)
    for _ in range(30):  # the jitter is random: 5 s + up to 20 % stays inside the 10 s
        err = model_failure_error(overloaded(5.0), c)
        assert err.non_retryable is False and "retry_stopped" not in err.details[0]
        delay = err.details[0]["next_retry_delay_seconds"]
        assert 5.0 <= delay <= 5.0 * (1 + RETRY_JITTER_FRACTION) <= 10.0
        assert err.next_retry_delay == timedelta(seconds=delay)


def test_a_retry_after_that_fits_but_whose_jitter_does_not_is_clamped_to_the_exploration_deadline():
    now = time.time()
    c = exploring(exploration_left=10, now=now)
    for _ in range(30):
        err = model_failure_error(overloaded(9.5), c)  # fits; 9.5 s + 20 % = 11.4 s does not
        assert err.non_retryable is False
        delay = err.details[0]["next_retry_delay_seconds"]
        assert 9.5 <= delay <= 10.0
        assert err.next_retry_delay == timedelta(seconds=delay)


def test_a_failure_that_cannot_be_retried_anyway_is_not_marked_as_retry_stopped():
    err = model_failure_error(ModelCallFailed("not found", retryable=False,
                                              failure={"kind": "permanent", "status": 404, "retry_after_seconds": 200.0}),
                              exploring(exploration_left=10, now=time.time()))
    assert err.non_retryable is True and "retry_stopped" not in err.details[0]


def test_a_quota_failure_is_still_typed_quota_exhausted_when_its_retry_after_does_not_fit():
    quota = ModelCallFailed("usage limit", failure={"kind": "quota-exhausted", "status": 429, "retry_after_seconds": 200.0})
    err = model_failure_error(quota, exploring(exploration_left=10, now=time.time()))
    assert err.type == "PROVIDER_QUOTA_EXHAUSTED" and err.non_retryable is True
    assert "retry_stopped" not in err.details[0]


def test_a_failure_without_a_retry_after_is_retried_whatever_the_exploration_time_left():
    err = model_failure_error(overloaded(None), exploring(exploration_left=1, now=time.time()))
    assert err.non_retryable is False and err.next_retry_delay is None
    assert "retry_stopped" not in err.details[0] and "next_retry_delay_seconds" not in err.details[0]


# -- without exploration_deadline_unix the conclusion's boundary applies, as before ---------------------------------------------
def test_without_an_exploration_deadline_the_conclusions_boundary_decides_whether_a_retry_fits():
    now = time.time()
    # 200 s asked, the conclusion's reserve 1000 s away: it fits and is waited for
    err = model_failure_error(overloaded(200.0), conclusion_only(left=1000, now=now))
    assert err.non_retryable is False and "retry_stopped" not in err.details[0]
    assert 200.0 <= err.details[0]["next_retry_delay_seconds"] <= MAX_RETRY_DELAY_SECONDS
    # the same 200 s asked with the reserve 10 s away does not fit: stopped
    err = model_failure_error(overloaded(200.0), conclusion_only(left=10, now=now))
    assert err.non_retryable is True and err.details[0]["retry_stopped"] == STOPPED
    assert err.next_retry_delay is None


def test_without_a_completion_nothing_bounds_the_retry():
    err = model_failure_error(overloaded(200.0), None)
    assert err.non_retryable is False and "retry_stopped" not in err.details[0]
    assert err.details[0]["next_retry_delay_seconds"] >= 200.0


# -- workflows.turn_retry_delay: the coordinator's backoff before a retry turn -----------------------------------------------------
def turn_failure_with(retry_after: float | None) -> dict[str, Any]:
    return {"error_type": "MODEL_CALL_FAILED", "retryable": True,
            "provider": {"kind": "transient", "status": 529, "retry_after_seconds": retry_after}}


@pytest.mark.parametrize("retry_after", [None, 0, 5, 60, 200, 5000])
@pytest.mark.parametrize("base", [0, 30, 60, 480])
@pytest.mark.parametrize("jitter", [0.0, 0.5, 0.999])
def test_turn_retry_delay_never_passes_the_exploration_deadline(base, retry_after, jitter):
    c = exploring(exploration_left=10)
    delay = turn_retry_delay(base, turn_failure_with(retry_after), c, NOW, jitter)
    assert 0.0 <= delay <= 10.0  # exploration_deadline_unix - now, not the conclusion's far later boundary


def test_turn_retry_delay_ends_at_the_exploration_deadline_when_the_requested_delay_cannot_fit():
    c = exploring(exploration_left=10)
    assert turn_retry_delay(60, turn_failure_with(200), c, NOW, 0.0) == 10.0
    assert turn_retry_delay(60, turn_failure_with(200), c, NOW + 4, 0.0) == pytest.approx(6.0)  # time passes
    assert turn_retry_delay(60, turn_failure_with(200), c, NOW + 10, 0.0) == 0.0  # at the deadline
    assert turn_retry_delay(60, turn_failure_with(200), c, NOW + 60, 1.0) == 0.0  # past it: never negative


def test_turn_retry_delay_is_unbounded_by_a_far_exploration_deadline():
    c = exploring(exploration_left=1000)
    assert turn_retry_delay(60, turn_failure_with(120), c, NOW, 0.0) == 120.0
    assert turn_retry_delay(60, turn_failure_with(None), c, NOW, 0.0) == 60.0


def test_turn_retry_delay_without_an_exploration_deadline_uses_the_conclusions_boundary():
    c = conclusion_only(left=400)
    assert turn_retry_delay(60, turn_failure_with(600), c, NOW, 0.0) == 400.0
    assert turn_retry_delay(60, turn_failure_with(100), c, NOW, 0.0) == 100.0
    assert turn_retry_delay(60, turn_failure_with(600), conclusion_only(left=-50), NOW, 1.0) == 0.0
    # the same Retry-After with an exploration deadline 10 s away is bounded by that instead
    assert turn_retry_delay(60, turn_failure_with(600), {**c, "exploration_deadline_unix": NOW + 10}, NOW, 0.0) == 10.0
    assert turn_retry_delay(60, turn_failure_with(600), {**c, "exploration_deadline_unix": None}, NOW, 0.0) == 400.0


def test_turn_retry_delay_without_a_completion_is_not_bounded_by_either_boundary():
    assert turn_retry_delay(60, turn_failure_with(120), {}, NOW, 0.0) == 120.0


# -- domain.turns.turn_boundary ---------------------------------------------------------------------------------------------------
def test_turn_boundary_of_an_exploratory_turn_is_the_exploration_deadline():
    state = {**completion_state(1000.0, 500.0, 60.0), "exploration_deadline_unix": 800.0}
    assert state["conclude_now"] is False
    assert turn_boundary(state) == 800.0  # not the conclusion's 940


def test_turn_boundary_of_the_concluding_turn_is_the_wall_deadline_even_with_an_exploration_deadline():
    state = {**completion_state(1000.0, 950.0, 60.0), "exploration_deadline_unix": 800.0}
    assert state["conclude_now"] is True
    assert turn_boundary(state) == 1000.0


def test_turn_boundary_without_an_exploration_deadline_is_where_the_reserve_begins():
    state = completion_state(1000.0, 500.0, 60.0)
    assert "exploration_deadline_unix" not in state
    assert turn_boundary(state) == 940.0
    assert turn_boundary({**state, "exploration_deadline_unix": None}) == 940.0


def test_turn_boundary_never_passes_the_conclusions_reserve():
    state = {**completion_state(1000.0, 500.0, 60.0), "exploration_deadline_unix": 990.0}
    assert turn_boundary(state) == 940.0  # an exploration deadline inside the reserve cannot extend the turn
