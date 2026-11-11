"""F2c (review R3 findings 2 and 3): model usage that cannot be known is charged as uncertain usage, never as zero,
against PostgreSQL. Exact amounts throughout: budget rows (reserved / used / uncertain) and ledger entries.

* Finding 2: a provider request abandoned in flight (stop, pause, cancel, deadline) may still be billed. Completed
  responses settle as certain ``usage``; the abandoned request settles at its admitted worst case (``in_flight_estimate``)
  as ``possible-usage`` (the budget's ``uncertain`` column grows by it); without an estimate, whatever the reservation
  still held does. The rest of the reservation is released.
* Finding 3: a reservation still open when its turn completes, or when a retry of the same decision slot reserves, belongs
  to an attempt that died, possibly with a request at the provider. It is settled as provisional ``possible-usage`` of
  the whole open amount (never released as zero). A late settlement of that reservation reverses the provisional charge
  (a ``correction`` entry) and books what it reports, so nothing is charged twice and nothing is released twice.
"""

from __future__ import annotations

import pytest
from pytest import approx
from test_turn_recovery import setup

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

RESERVED = 120_000          # the decision's token reservation
USD_RESERVED = 5.0          # the decision's dollar reservation (when the investigation has a model_usd budget)
OTHER = 300_000             # tokens held by another reservation of the investigation: must never be freed by mistake
PRICE = {"input": 3.0, "output": 15.0}   # USD per million tokens
ADMITTED = {"input_tokens": 3000, "output_tokens": 1024}   # the abandoned request's input estimate and output cap
ESTIMATE = 3000 + 1024                                     # = 4024 tokens
ESTIMATE_USD = (3000 * PRICE["input"] + 1024 * PRICE["output"]) / 1e6      # 0.02436
KNOWN = {"input_tokens": 500, "output_tokens": 200}                        # one response that completed
KNOWN_TOKENS = 700
KNOWN_USD = (500 * PRICE["input"] + 200 * PRICE["output"]) / 1e6           # 0.0045
LIMITS = {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100}


def _usage(known: dict | None = None, *, in_flight: bool | None = None, estimate: dict | None = None,
           uncertain: bool | None = None, priced: bool = False, reason: str = "investigation-stopped") -> dict:
    """Usage as the runtime reports it: ``known`` is what completed responses billed; ``in_flight`` is None for a normal
    decision, otherwise the ``aborted`` record (a request abandoned in flight, or a decision stopped between requests)."""
    known = known or {"input_tokens": 0, "output_tokens": 0}
    u: dict = {**known, "calls": 1 if known["input_tokens"] or known["output_tokens"] else 0}
    if priced:
        u["usd"] = (known["input_tokens"] * PRICE["input"] + known["output_tokens"] * PRICE["output"]) / 1e6
    if in_flight is not None:
        u["aborted"] = {"reason": reason, "in_flight": in_flight}
        if in_flight:
            u["aborted"]["abandoned_request"] = {"input_estimate": ADMITTED["input_tokens"],
                                                 "output_cap": ADMITTED["output_tokens"], "spent_before": 0}
            u["uncertain"] = True
    if uncertain is not None:
        u["uncertain"] = uncertain
    if estimate is not None:
        u["in_flight_estimate"] = estimate
    return u


def _estimate(priced: bool = False) -> dict:
    est = {**ADMITTED, "basis": "admitted-worst-case"}
    if priced:
        est["usd"] = ESTIMATE_USD
    return est


class Slot:
    """One turn of one investigation, driven through the same commands the decide activity sends."""

    def __init__(self, lab, *, usd: bool = False):
        self.lab = lab
        self.pid, inv, _ = setup(lab)
        if usd:  # a second investigation of the project, with a dollar budget as well (a fixture runtime needs no price)
            inv = lab.cmd(lab.admin, "start_investigation", {
                "question": "q", "charter_id": f"charter:s{self.pid}", "limits": {**LIMITS, "model_usd": 100.0},
                "investigator": {"runtime": "fixture", "settings": {"policy": "test-run-echo-then-wait"}}},
                project=self.pid)["result"]["investigation_id"]
        self.inv, self.usd = inv, usd
        self.wf = lab.principal(f"wf-{self.pid}", ["workflow"], [self.pid])
        b = lab.cmd(self.wf, "begin_turn", {"investigation_id": inv, "runtime_release": "test"},
                    project=self.pid)["result"]
        assert b["started"], b
        self.tid = b["turn_id"]
        self.scope = f"inv:{inv}"
        self.epochs: dict[tuple[int, int], int] = {}  # (attempt, slot) -> begin_decision epoch, when reserved with one

    # -- the lab commands ---------------------------------------------------------------------------------------------
    def ref(self, attempt: int = 1, slot: int = 0) -> str:
        epoch = self.epochs.get((attempt, slot))  # F5 (C2): a begin_decision epoch is part of the ref
        return f"model:{self.tid}:{slot}:{attempt}" + (f":e{epoch}" if epoch is not None else "")

    def hold(self, tokens: float = OTHER) -> None:
        """Another holder of the same dimension (as in test_late_settlement_after_turn_release_does_not_release_twice)."""
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            tx.reserve(self.scope, "model_tokens", tokens, "test:other-holder")

    def reserve(self, attempt: int = 1, slot: int = 0, **extra) -> dict:
        estimate = {"model_tokens": RESERVED, **({"model_usd": USD_RESERVED} if self.usd else {})}
        if extra.get("epoch") is not None:
            self.epochs[(attempt, slot)] = int(extra["epoch"])
        r = self.lab.cmd(self.wf, "reserve_model_budget", {"turn_id": self.tid, "slot": slot, "attempt": attempt,
                                                           "estimate": estimate, **extra}, project=self.pid)["result"]
        assert r["reservation_ref"] == self.ref(attempt, slot)
        return r

    def settle(self, usage: dict, attempt: int = 1, slot: int = 0, tokens: float = RESERVED) -> dict:
        """``record_model_usage`` for that attempt's reservation, as the decide activity sends it."""
        reserved = {"model_tokens": tokens, **({"model_usd": USD_RESERVED} if self.usd else {})}
        return self.lab.cmd(self.wf, "record_model_usage", {
            "turn_id": self.tid, "usage": usage, "reserved": reserved,
            "reservation_ref": self.ref(attempt, slot)}, project=self.pid)["result"]

    def complete(self) -> dict:
        return self.lab.cmd(self.wf, "complete_turn", {"turn_id": self.tid, "stop_reason": "calls-exhausted"},
                            project=self.pid)["result"]

    # -- what the ledger says -----------------------------------------------------------------------------------------
    def budget(self, dim: str = "model_tokens") -> dict:
        return self.lab.q(self.lab.admin, "budgets", {"investigation_id": self.inv},
                          project=self.pid)["budgets"][dim]

    def raw_entries(self, dim: str = "model_tokens", ref: str | None = None) -> list[dict]:
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            return [e for e in tx.budget_entries(self.scope)
                    if e["dimension"] == dim and (ref is None or e["ref"] == ref)]

    def entries(self, dim: str = "model_tokens", attempt: int = 1, slot: int = 0) -> list[tuple[str, float]]:
        return [(e["kind"], float(e["amount"])) for e in self.raw_entries(dim, self.ref(attempt, slot))]

    def snapshot(self) -> tuple:
        """Everything a repeated, idempotent call must leave alone: all budget rows and all ledger entries."""
        dims = ("model_tokens", "model_usd") if self.usd else ("model_tokens",)
        return tuple((d, tuple(sorted(self.budget(d).items())),
                      tuple((e["kind"], float(e["amount"]), e["ref"]) for e in self.raw_entries(d))) for d in dims)

    def open_reservations(self) -> dict[str, float]:
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            return tx.open_reservations(self.scope, f"model:{self.tid}:")

    def turn_result(self) -> dict:
        return self.lab.q(self.lab.admin, "get_turn", {"turn_id": self.tid}, project=self.pid)["result"]


def _approx(values: list[float]):
    return approx(values, abs=1e-9)


def _kinds(entries: list[tuple[str, float]]) -> list[str]:
    return [k for k, _ in entries]


def _amounts(entries: list[tuple[str, float]]) -> list[float]:
    return [a for _, a in entries]


# -- finding 2: a request abandoned in flight ----------------------------------------------------------------------------
def test_a_request_abandoned_before_any_response_is_charged_at_its_admitted_estimate(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": 0, "uncertain": 0}
    s.settle(_usage(in_flight=True, estimate=_estimate(priced=True)))
    # Tokens: the admitted worst case (input estimate 3000 + output cap 1024) as possible usage, the rest released.
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", ESTIMATE), ("release", RESERVED - ESTIMATE)]
    assert s.entries()[1][1] == 4024 and s.entries()[2][1] == 115_976
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": ESTIMATE, "uncertain": ESTIMATE}
    # Dollars: the same request priced, the rest of the dollar reservation released.
    usd = s.entries("model_usd")
    assert _kinds(usd) == ["reserve", "possible-usage", "release"]
    assert _amounts(usd) == _approx([USD_RESERVED, ESTIMATE_USD, USD_RESERVED - ESTIMATE_USD])
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == approx(ESTIMATE_USD, abs=1e-9)
    assert b["uncertain"] == approx(ESTIMATE_USD, abs=1e-9) and abs(ESTIMATE_USD - 0.02436) < 1e-12
    # The ledger says it is an estimate, and why the decision ended.
    possible = s.raw_entries(ref=s.ref())[1]
    assert possible["detail"]["in_flight_estimate"] is True
    assert possible["detail"]["aborted"]["in_flight"] is True
    assert s.open_reservations()["model_tokens"] == 0


def test_a_priced_estimate_without_a_dollar_reservation_is_still_charged(lab):
    """The estimate's dollars are charged even if this settlement reports no dollar reservation."""
    s = Slot(lab, usd=True)
    s.reserve()
    s.lab.cmd(s.wf, "record_model_usage", {
        "turn_id": s.tid, "usage": _usage(in_flight=True, estimate=_estimate(priced=True)),
        "reserved": {"model_tokens": RESERVED}, "reservation_ref": s.ref()}, project=s.pid)
    assert s.budget()["used"] == ESTIMATE and s.budget()["uncertain"] == ESTIMATE
    b = s.budget("model_usd")
    assert b["used"] == approx(ESTIMATE_USD, abs=1e-9) and b["uncertain"] == approx(ESTIMATE_USD, abs=1e-9)
    assert b["reserved"] == USD_RESERVED  # the dollar reservation was not named by this settlement: untouched


def test_an_unpriced_estimate_charges_tokens_at_the_estimate_and_dollars_at_what_the_reservation_held(lab):
    """No price, so the estimate carries no dollars: the dollar dimension falls back to the whole reservation."""
    s = Slot(lab, usd=True)
    s.reserve()
    s.settle(_usage(in_flight=True, estimate=_estimate(priced=False)))
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", ESTIMATE), ("release", RESERVED - ESTIMATE)]
    assert s.entries("model_usd") == [("reserve", USD_RESERVED), ("possible-usage", USD_RESERVED)]
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == USD_RESERVED and b["uncertain"] == USD_RESERVED


def test_one_completed_response_is_certain_usage_and_the_abandoned_request_is_possible_usage(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    s.settle(_usage(KNOWN, in_flight=True, estimate=_estimate(priced=True), priced=True))
    rest = RESERVED - KNOWN_TOKENS - ESTIMATE
    assert s.entries() == [("reserve", RESERVED), ("usage", KNOWN_TOKENS), ("possible-usage", ESTIMATE),
                           ("release", rest)]
    assert rest == 115_276
    # The completed response is certain: only the abandoned request grows the uncertain column.
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS + ESTIMATE,
                          "uncertain": ESTIMATE}
    usd = s.entries("model_usd")
    assert _kinds(usd) == ["reserve", "usage", "possible-usage", "release"]
    assert _amounts(usd) == _approx([USD_RESERVED, KNOWN_USD, ESTIMATE_USD, USD_RESERVED - KNOWN_USD - ESTIMATE_USD])
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == approx(KNOWN_USD + ESTIMATE_USD, abs=1e-9)
    assert b["uncertain"] == approx(ESTIMATE_USD, abs=1e-9)
    entries = {e["kind"]: e for e in s.raw_entries(ref=s.ref())}
    assert "in_flight_estimate" not in entries["usage"]["detail"]
    assert entries["possible-usage"]["detail"]["in_flight_estimate"] is True


def test_in_flight_without_an_estimate_charges_what_the_reservation_still_held(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    s.settle(_usage(KNOWN, in_flight=True, priced=True))   # no in_flight_estimate
    held = RESERVED - KNOWN_TOKENS
    assert s.entries() == [("reserve", RESERVED), ("usage", KNOWN_TOKENS), ("possible-usage", held)]
    assert held == 119_300
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": RESERVED, "uncertain": held}
    usd = s.entries("model_usd")
    assert _kinds(usd) == ["reserve", "usage", "possible-usage"]
    assert _amounts(usd) == _approx([USD_RESERVED, KNOWN_USD, USD_RESERVED - KNOWN_USD])
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == approx(USD_RESERVED, abs=1e-9)
    assert b["uncertain"] == approx(USD_RESERVED - KNOWN_USD, abs=1e-9)


def test_in_flight_with_nothing_known_and_no_estimate_charges_the_whole_reservation(lab):
    s = Slot(lab)
    s.hold()
    s.reserve()
    s.settle(_usage(in_flight=True))
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": RESERVED, "uncertain": RESERVED}


def test_a_normal_settlement_is_unchanged(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    s.settle(_usage(KNOWN, priced=True))
    assert s.entries() == [("reserve", RESERVED), ("usage", KNOWN_TOKENS), ("release", RESERVED - KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS, "uncertain": 0}
    usd = s.entries("model_usd")
    assert _kinds(usd) == ["reserve", "usage", "release"]
    assert _amounts(usd) == _approx([USD_RESERVED, KNOWN_USD, USD_RESERVED - KNOWN_USD])
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == approx(KNOWN_USD, abs=1e-9) and b["uncertain"] == 0


def test_an_estimate_is_ignored_when_nothing_was_abandoned_in_flight(lab):
    """A decision stopped between requests (``in_flight`` false) has only certain, completed usage."""
    s = Slot(lab)
    s.hold()
    s.reserve()
    s.settle(_usage(KNOWN, in_flight=False, estimate=_estimate()))
    assert s.entries() == [("reserve", RESERVED), ("usage", KNOWN_TOKENS), ("release", RESERVED - KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS, "uncertain": 0}


def test_usage_flagged_uncertain_without_an_abandoned_request_is_possible_usage_as_before(lab):
    """A failed call (timeout after a possible charge) reports ``uncertain`` itself: its usage is possible usage."""
    s = Slot(lab)
    s.hold()
    s.reserve()
    s.settle(_usage(KNOWN, uncertain=True))
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", KNOWN_TOKENS),
                           ("release", RESERVED - KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS, "uncertain": KNOWN_TOKENS}


# -- finding 3: reservations left open ----------------------------------------------------------------------------------
def test_complete_turn_charges_an_open_reservation_as_provisional_possible_usage(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    assert s.budget()["reserved"] == OTHER + RESERVED
    s.complete()
    assert s.turn_result()["uncertain_model_reservations"] == {"model_tokens": RESERVED, "model_usd": USD_RESERVED}
    assert "released_model_reservations" not in s.turn_result()
    # The whole open amount: charged, not released; no release entry.
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    provisional = s.raw_entries(ref=s.ref())[1]
    assert provisional["detail"] == {"provisional": True, "reconciled_by": "complete_turn"}
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": RESERVED, "uncertain": RESERVED}
    assert s.entries("model_usd") == [("reserve", USD_RESERVED), ("possible-usage", USD_RESERVED)]
    usd = s.budget("model_usd")
    assert usd["reserved"] == 0 and usd["used"] == USD_RESERVED and usd["uncertain"] == USD_RESERVED
    assert s.open_reservations() == {"model_tokens": 0.0, "model_usd": 0.0}
    # Completing again (another command key) changes nothing: the turn is closed, no reservation is open.
    before = s.snapshot()
    again = s.complete()
    assert again["already_closed"] is True
    assert s.snapshot() == before
    assert s.turn_result()["uncertain_model_reservations"] == {"model_tokens": RESERVED, "model_usd": USD_RESERVED}


def test_complete_turn_leaves_a_settled_reservation_alone(lab):
    s = Slot(lab)
    s.hold()
    s.reserve()
    s.settle(_usage(KNOWN))
    before = s.snapshot()
    s.complete()
    assert s.turn_result()["uncertain_model_reservations"] == {}
    assert s.snapshot() == before
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS, "uncertain": 0}


def test_complete_turn_charges_every_open_reservation_of_the_turn_once_each(lab):
    s = Slot(lab)
    s.hold()
    s.reserve(attempt=1, slot=0)
    s.reserve(attempt=1, slot=1)
    s.settle(_usage(KNOWN), attempt=1, slot=1)    # slot 1 settled; slot 0 is left open
    s.complete()
    assert s.turn_result()["uncertain_model_reservations"] == {"model_tokens": RESERVED}
    assert s.entries(slot=0) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.entries(slot=1) == [("reserve", RESERVED), ("usage", KNOWN_TOKENS), ("release", RESERVED - KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": RESERVED + KNOWN_TOKENS,
                          "uncertain": RESERVED}


def test_a_late_in_flight_settlement_after_complete_turn_replaces_the_provisional_charge(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    s.complete()
    assert s.budget()["used"] == RESERVED and s.budget()["uncertain"] == RESERVED   # provisional
    s.settle(_usage(KNOWN, in_flight=True, estimate=_estimate(priced=True), priced=True))
    entries = s.entries()
    assert _kinds(entries) == ["reserve", "possible-usage", "correction", "usage", "possible-usage"]
    assert entries == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                       ("usage", KNOWN_TOKENS), ("possible-usage", ESTIMATE)]
    correction = s.raw_entries(ref=s.ref())[2]
    assert correction["detail"] == {"reverses": "provisional", "replaced_by": "settlement"}
    # Used is what was reported (known + estimate), uncertain only the estimate. Nothing was released a second time:
    # the other holder's tokens are still held, and no release entry exists for the reservation.
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS + ESTIMATE,
                          "uncertain": ESTIMATE}
    assert "release" not in _kinds(entries)
    assert s.open_reservations() == {"model_tokens": 0.0, "model_usd": 0.0}
    # Dollars: provisional USD_RESERVED reversed, then the known usage and the priced estimate booked.
    usd = s.entries("model_usd")
    assert _kinds(usd) == ["reserve", "possible-usage", "correction", "usage", "possible-usage"]
    assert _amounts(usd) == _approx([USD_RESERVED, USD_RESERVED, -USD_RESERVED, KNOWN_USD, ESTIMATE_USD])
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == approx(KNOWN_USD + ESTIMATE_USD, abs=1e-9)
    assert b["uncertain"] == approx(ESTIMATE_USD, abs=1e-9)


def test_a_late_in_flight_settlement_without_an_estimate_keeps_the_provisional_amount_as_possible_usage(lab):
    s = Slot(lab)
    s.hold()
    s.reserve()
    s.complete()
    s.settle(_usage(KNOWN, in_flight=True))      # no estimate: what the reservation held, less the known usage
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                           ("usage", KNOWN_TOKENS), ("possible-usage", RESERVED - KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": RESERVED,
                          "uncertain": RESERVED - KNOWN_TOKENS}


def test_a_late_normal_settlement_after_complete_turn_charges_only_what_it_reports(lab):
    s = Slot(lab, usd=True)
    s.hold()
    s.reserve()
    s.complete()
    s.settle(_usage(KNOWN, priced=True))
    assert s.entries() == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                           ("usage", KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS, "uncertain": 0}
    b = s.budget("model_usd")
    assert b["reserved"] == 0 and b["used"] == approx(KNOWN_USD, abs=1e-9) and b["uncertain"] == 0


def test_a_late_zero_settlement_restores_the_budget_to_its_values_before_the_reservation(lab):
    s = Slot(lab, usd=True)
    s.hold()
    # Something already charged on this investigation: a certain usage and an uncertain one, on other reservations.
    s.reserve(attempt=1, slot=1)
    s.settle(_usage({"input_tokens": 5000, "output_tokens": 0}, priced=True), attempt=1, slot=1)
    s.reserve(attempt=1, slot=2)
    s.settle(_usage({"input_tokens": 300, "output_tokens": 0}, uncertain=True, priced=True), attempt=1, slot=2)
    before = {d: s.budget(d) for d in ("model_tokens", "model_usd")}
    assert before["model_tokens"] == {"limit_amount": 1e6, "reserved": OTHER, "used": 5300, "uncertain": 300}
    # A decision reserves, dies with its reservation open, and the turn closes: provisional charge.
    s.reserve(attempt=1, slot=0)
    s.complete()
    mid = s.budget()
    assert mid["used"] == 5300 + RESERVED and mid["uncertain"] == 300 + RESERVED and mid["reserved"] == OTHER
    # The zombie attempt reports nothing and was not in flight: the provisional charge is reversed, exactly.
    s.settle(_usage(), attempt=1, slot=0)
    after = {d: s.budget(d) for d in ("model_tokens", "model_usd")}
    assert after["model_tokens"] == before["model_tokens"]
    for k in ("limit_amount", "reserved", "used", "uncertain"):
        assert after["model_usd"][k] == approx(before["model_usd"][k], abs=1e-9), k
    assert s.entries(slot=0) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED)]
    # A second late settlement (another command key) finds nothing left to reverse.
    snapshot = s.snapshot()
    s.settle(_usage(), attempt=1, slot=0)
    assert s.snapshot() == snapshot


# -- finding 3: a retry of the same decision slot -----------------------------------------------------------------------
@pytest.mark.parametrize("epochs", [False, True], ids=["attempt-numbers", "begin_decision-epochs"])
def test_a_retry_charges_the_previous_attempts_open_reservation_as_provisional_possible_usage(lab, epochs):
    s = Slot(lab)
    s.hold()

    def begin(attempt: int) -> dict:
        if not epochs:
            return {}
        r = lab.cmd(s.wf, "begin_decision", {"turn_id": s.tid, "slot": 0, "attempt": attempt,
                                             "execution": "run-A/decide-slot-0"}, project=s.pid)["result"]
        return {"epoch": r["epoch"]}

    s.reserve(attempt=1, **begin(1))                    # attempt 1 reserved and died (possibly with a request out)
    assert s.budget()["reserved"] == OTHER + RESERVED
    s.reserve(attempt=2, **begin(2))                    # the retry
    first, second = s.ref(1), s.ref(2)
    # Attempt 1: its whole open amount, charged as provisional possible usage; attempt 2: its own reservation, intact.
    assert s.entries(attempt=1) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.raw_entries(ref=first)[1]["detail"] == {"provisional": True, "reconciled_by": second}
    assert s.entries(attempt=2) == [("reserve", RESERVED)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": RESERVED, "uncertain": RESERVED}
    assert s.open_reservations() == {"model_tokens": RESERVED}      # only attempt 2's
    # Attempt 1's request reports late (a normal 700-token response): trued up exactly, attempt 2 untouched.
    stale = {"input_tokens": 500, "output_tokens": 200}
    mid_reserved = s.budget()["reserved"]
    s.settle(_usage(stale), attempt=1)
    assert s.entries(attempt=1) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                                    ("usage", KNOWN_TOKENS)]
    assert s.budget()["used"] == KNOWN_TOKENS and s.budget()["uncertain"] == 0
    assert s.budget()["reserved"] == mid_reserved                     # nothing released: attempt 2 still holds its own
    assert s.entries(attempt=2)[0] == ("reserve", RESERVED)
    assert "release" not in _kinds(s.entries(attempt=2))


def test_a_retry_whose_previous_attempt_reports_a_late_abandoned_request_is_trued_up_exactly(lab):
    s = Slot(lab)
    s.hold()
    s.reserve(attempt=1)
    s.reserve(attempt=2)
    # The cancelled attempt 1 had completed one response (500/200) and abandoned a second request (3000 + 1024).
    s.settle(_usage(KNOWN, in_flight=True, estimate=_estimate()), attempt=1)
    assert s.entries(attempt=1) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                                    ("usage", KNOWN_TOKENS), ("possible-usage", ESTIMATE)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": KNOWN_TOKENS + ESTIMATE,
                          "uncertain": ESTIMATE}
    # The retry then settles its own reservation normally: only its own usage is added.
    s.settle(_usage({"input_tokens": 1000, "output_tokens": 0}), attempt=2)
    assert s.entries(attempt=2) == [("reserve", RESERVED), ("usage", 1000), ("release", RESERVED - 1000)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS + ESTIMATE + 1000,
                          "uncertain": ESTIMATE}


def test_a_retry_after_the_previous_attempt_settled_charges_nothing_provisionally(lab):
    s = Slot(lab)
    s.hold()
    s.reserve(attempt=1)
    s.settle(_usage(KNOWN), attempt=1)
    s.reserve(attempt=2)
    assert s.entries(attempt=1) == [("reserve", RESERVED), ("usage", KNOWN_TOKENS), ("release", RESERVED - KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": KNOWN_TOKENS, "uncertain": 0}


def test_each_further_retry_charges_only_the_reservation_left_open_by_the_one_before(lab):
    s = Slot(lab)
    s.hold()
    s.reserve(attempt=1)
    s.reserve(attempt=2)
    s.reserve(attempt=3)
    assert s.entries(attempt=1) == [("reserve", RESERVED), ("possible-usage", RESERVED)]    # charged once, by attempt 2
    assert s.entries(attempt=2) == [("reserve", RESERVED), ("possible-usage", RESERVED)]    # charged once, by attempt 3
    assert s.entries(attempt=3) == [("reserve", RESERVED)]
    assert s.raw_entries(ref=s.ref(2))[1]["detail"] == {"provisional": True, "reconciled_by": s.ref(3)}
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": 2 * RESERVED,
                          "uncertain": 2 * RESERVED}
    # Closing the turn then charges only attempt 3's open reservation.
    s.complete()
    assert s.turn_result()["uncertain_model_reservations"] == {"model_tokens": RESERVED}
    assert s.entries(attempt=1) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.entries(attempt=3) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": 3 * RESERVED, "uncertain": 3 * RESERVED}


def test_a_repeated_reservation_of_the_same_attempt_is_not_charged_as_a_dead_attempt(lab):
    """Only reservations of OTHER attempts are reconciled: a second reserve command for the same attempt (another
    command key) never settles that attempt's own open reservation as provisional usage."""
    s = Slot(lab)
    s.reserve(attempt=1)
    s.reserve(attempt=1)
    assert "possible-usage" not in _kinds(s.entries(attempt=1))
    assert s.budget()["used"] == 0 and s.budget()["uncertain"] == 0
