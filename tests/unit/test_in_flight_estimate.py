"""F2c (review R3 finding 2): a request abandoned in flight carries a conservative bill estimate.

A provider request abandoned when the investigation stops, pauses, is cancelled or reaches the decision deadline may
still be billed, and its response never arrives. The runtime reports, with the aborted usage, ``in_flight_estimate``:
the worst case the request was admitted with (its input estimate plus its output cap), priced when the runtime has a
price. The lab settles it as uncertain ``possible-usage`` (never as zero). Fixture models only: no network, no lab."""

from __future__ import annotations

import time

import pytest
from pytest import approx
from test_decision_abort import (
    MODES,
    Provider,
    read_reply,
    read_tools,
    request,
    response,
    runtime,
    timed,
    triggered_after,
)

from eidos.investigator import pydantic_ai_runtime as par
from eidos.investigator.runtime import DecisionAbort, DecisionAborted

pytestmark = pytest.mark.fixture_llm

PRICE = {"input": 3.0, "output": 15.0}  # USD per million tokens
ADMITTED = {"input_estimate": 3000, "output_cap": 1024, "spent_before": 0}


# -- the math ------------------------------------------------------------------------------------------------------
def test_estimate_without_a_price_is_the_admitted_tokens_and_has_no_dollars():
    est = par.in_flight_estimate(ADMITTED, None)
    assert est == {"input_tokens": 3000, "output_tokens": 1024, "basis": "admitted-worst-case"}
    assert "usd" not in est
    assert par.in_flight_estimate(ADMITTED, {}) == est  # an empty price is no price


def test_estimate_with_a_price_adds_dollars_priced_like_reported_usage():
    est = par.in_flight_estimate(ADMITTED, PRICE)
    assert est["input_tokens"] == 3000 and est["output_tokens"] == 1024 and est["basis"] == "admitted-worst-case"
    assert est["usd"] == approx((3000 * 3.0 + 1024 * 15.0) / 1e6, abs=1e-12) == approx(0.02436, abs=1e-12)
    # The same formula as the usage of a completed response (usage_from_messages) with those tokens.
    done = par.usage_from_messages([response(3000, 1024)], "openai", "m", PRICE)
    assert done["input_tokens"] == 3000 and done["output_tokens"] == 1024
    assert est["usd"] == approx(done["usd"], abs=1e-12)
    assert par.in_flight_estimate({"input_estimate": 1_000_000, "output_cap": 0}, PRICE)["usd"] == approx(3.0)
    assert par.in_flight_estimate({"input_estimate": 0, "output_cap": 1_000_000}, PRICE)["usd"] == approx(15.0)


def test_estimate_ignores_what_was_already_spent_before_the_request():
    """``spent_before`` is the tokens of earlier responses, already reported as certain usage: not part of the estimate."""
    est = par.in_flight_estimate({"input_estimate": 3000, "output_cap": 1024, "spent_before": 9999}, PRICE)
    assert est["input_tokens"] + est["output_tokens"] == 4024
    assert est["usd"] == approx(0.02436, abs=1e-12)


@pytest.mark.parametrize("abandoned", [{}, {"input_estimate": None, "output_cap": None}])
def test_estimate_of_a_request_with_no_admission_record_is_zero_not_an_error(abandoned):
    assert par.in_flight_estimate(abandoned, None) == {"input_tokens": 0, "output_tokens": 0,
                                                       "basis": "admitted-worst-case"}
    assert par.in_flight_estimate(abandoned, PRICE)["usd"] == 0.0


def test_estimate_values_are_integers_of_tokens():
    est = par.in_flight_estimate({"input_estimate": 3000.0, "output_cap": "1024"}, None)
    assert est["input_tokens"] == 3000 and est["output_tokens"] == 1024
    assert isinstance(est["input_tokens"], int) and isinstance(est["output_tokens"], int)


# -- the runtime attaches it when a request is abandoned in flight -------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
def test_stop_in_flight_reports_the_admitted_worst_case_of_the_abandoned_request(mode):
    provider = Provider()
    rt = runtime(provider, mode, max_output_tokens=512)
    abort = DecisionAbort()
    with triggered_after(abort, 0.2, "investigation-stopped"):
        res, _ = timed(lambda: rt.decide(request(abort)))
    assert isinstance(res, DecisionAborted) and res.in_flight is True
    pu = res.possible_usage
    assert pu["uncertain"] is True and pu["aborted"]["in_flight"] is True
    assert res.abandoned["output_cap"] == 512 and res.abandoned["input_estimate"] > 0
    assert pu["in_flight_estimate"] == {"input_tokens": res.abandoned["input_estimate"], "output_tokens": 512,
                                        "basis": "admitted-worst-case"}
    # Nothing completed: the certain tokens are zero, and the estimate is separate from them.
    assert pu["input_tokens"] == 0 and pu["output_tokens"] == 0 and pu["calls"] == 0
    assert pu["in_flight_estimate"]["input_tokens"] + pu["in_flight_estimate"]["output_tokens"] > 512


@pytest.mark.parametrize("mode", MODES)
def test_stop_in_flight_with_a_price_reports_the_priced_estimate(mode):
    provider = Provider()
    rt = runtime(provider, mode, max_output_tokens=512, price_per_mtok=PRICE)
    abort = DecisionAbort()
    with triggered_after(abort, 0.2, "investigation-stopped"):
        res, _ = timed(lambda: rt.decide(request(abort)))
    assert isinstance(res, DecisionAborted)
    est, inp = res.possible_usage["in_flight_estimate"], res.abandoned["input_estimate"]
    assert est["input_tokens"] == inp and est["output_tokens"] == 512
    assert est["usd"] == approx((inp * 3.0 + 512 * 15.0) / 1e6, abs=1e-12) and est["usd"] > 0
    assert res.possible_usage["usd"] == 0.0  # no response completed: nothing certain to price


@pytest.mark.parametrize("mode", MODES)
def test_deadline_in_flight_reports_the_admitted_worst_case_in_the_no_effect_decision(mode):
    provider = Provider()
    rt = runtime(provider, mode, max_output_tokens=512, price_per_mtok=PRICE)
    abort = DecisionAbort(deadline=time.monotonic() + 0.3)
    res, _ = timed(lambda: rt.decide(request(abort)))
    assert not isinstance(res, BaseException), res
    u = res.usage
    abandoned = u["aborted"]["abandoned_request"]
    assert u["uncertain"] is True and u["aborted"]["in_flight"] is True
    assert u["in_flight_estimate"] == {"input_tokens": abandoned["input_estimate"], "output_tokens": 512,
                                       "basis": "admitted-worst-case",
                                       "usd": approx((abandoned["input_estimate"] * 3.0 + 512 * 15.0) / 1e6,
                                                     abs=1e-12)}


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_a_completed_response_stays_certain_and_separate_from_the_abandoned_requests_estimate(mode):
    provider = Provider(fast=[read_reply(mode, inp=111, out=22)])    # one response, then the second request hangs
    rt = runtime(provider, mode, max_output_tokens=512, price_per_mtok=PRICE)
    abort = DecisionAbort()
    with triggered_after(abort, 0.4, "investigation-stopped"):
        res, _ = timed(lambda: rt.decide(request(abort, read_tools())))
    assert isinstance(res, DecisionAborted) and provider.calls == 2
    pu = res.possible_usage
    assert pu["input_tokens"] == 111 and pu["output_tokens"] == 22 and pu["calls"] == 1
    assert pu["usd"] == approx((111 * 3.0 + 22 * 15.0) / 1e6, abs=1e-12)    # the known bill: the response's tokens only
    est = pu["in_flight_estimate"]
    assert est["input_tokens"] == res.abandoned["input_estimate"] and est["output_tokens"] == 512
    # The second request was admitted on top of the first's 133 tokens, and its estimate is its own: at least the
    # first response's input and output (the text is sent back), not the sum with it.
    assert est["input_tokens"] >= 111 + 22 and res.abandoned["spent_before"] == 133
    assert est["usd"] == approx((est["input_tokens"] * 3.0 + 512 * 15.0) / 1e6, abs=1e-12)


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_no_estimate_when_nothing_was_abandoned_in_flight(mode):
    # A stop set before the first request: no request started.
    provider = Provider(fast=[read_reply(mode)])
    abort = DecisionAbort()
    abort.trigger("investigation-stopped")
    with pytest.raises(DecisionAborted) as e:
        runtime(provider, mode, price_per_mtok=PRICE).decide(request(abort, read_tools()))
    assert provider.calls == 0 and e.value.in_flight is False
    assert "in_flight_estimate" not in e.value.possible_usage and not e.value.possible_usage.get("uncertain")

    # The deadline passing between two requests: the first response is exact, the second request never started.
    now = [0.0]
    late = DecisionAbort(deadline=10.0, clock=lambda: now[0])

    def first(info):
        now[0] = 11.0
        return read_reply(mode, inp=40, out=9)

    provider2 = Provider(fast=[first, read_reply(mode)])
    res = runtime(provider2, mode, price_per_mtok=PRICE).decide(request(late, read_tools()))
    assert provider2.calls == 1 and res.usage["aborted"]["in_flight"] is False
    assert "in_flight_estimate" not in res.usage and not res.usage.get("uncertain")
    assert res.usage["input_tokens"] == 40 and res.usage["output_tokens"] == 9
