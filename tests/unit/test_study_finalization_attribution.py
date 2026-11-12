"""Authored terminal failures and read coverage stay distinct from provider or budget outcomes."""
import pytest
from study.eidos_arms.arm4 import finalization_read_coverage, investigator_abstention_reason
from study.eidos_arms.common import classify

from eidos.domain.turns import AUTHORED_FINALIZATION_FAILURES


def stopped(reason, *, kind="investigator-output-invalid", record="typed-runtime", code="finalization-failed"):
    return {"status": "stopped", "body": {"controller_abstention": {"code": code, "failure": {
        "reason": reason, "kind": kind, "runtime_record_id": record,
    }}}}


@pytest.mark.parametrize("cause", sorted(AUTHORED_FINALIZATION_FAILURES))
def test_source_bound_authored_finalization_cause_is_invalid_output(cause):
    assert classify(stopped(cause), [], [])[0] == "invalid_output"
    assert classify(stopped(cause, record=None), [], [])[0] == "crash"
    assert classify(stopped(cause, kind="finalization-incomplete"), [], [])[0] == "crash"
    assert classify(stopped(cause), [], [], timed_out=True)[0] == "timeout"
    assert classify(stopped(cause, code="provider-unavailable"), [], [])[0] == "incident"


@pytest.mark.parametrize("cause, status", [
    ("reasoning-runaway", "crash"), ("runtime-bounded", "crash"),
    ("budget-exhausted", "token_budget"), ("wall-deadline-reached", "timeout"),
])
def test_non_authored_terminal_cause_keeps_existing_status(cause, status):
    assert classify(stopped(cause), [], [])[0] == status


@pytest.mark.parametrize("telemetry, expected", [
    (None, None),
    ({"read_rounds_forfeited": False, "finalization_read_round_limit": 0}, "no_read_rounds_available"),
    ({"read_rounds_forfeited": False, "finalization_read_round_limit": 2}, "not_forfeited"),
    ({"read_rounds_forfeited": True, "finalization_read_round_limit": 2}, "forfeited"),
])
def test_run_record_read_coverage_remains_distinct(telemetry, expected):
    assert finalization_read_coverage([{"phase": "finalization", "structured_output": telemetry}]) == expected

def test_valid_authored_no_lock_abstention_is_ok_with_a_descriptive_reason():
    shown = {"status": "concluded", "body": {"conclusion": {"incumbent": None, "claim_state": {"state": "abstained"}}},
             "finalization": {"trigger": "ordinary-finalization-time-exhausted"},
             "finalization_export_receipt": {"investigator_concluded": True, "locked_candidate": None}}
    status, detail = classify(shown, [], [])
    assert (status, detail) == ("ok", None)
    assert investigator_abstention_reason(shown, status) == "no-eligible-lock-at-boundary"
    assert investigator_abstention_reason(shown, "invalid_output") is None
    assert investigator_abstention_reason(stopped("finalization-no-lock"), "invalid_output") is None


def test_valid_conclusion_after_correction_has_no_terminal_cause():
    shown = {"status": "concluded", "body": {"conclusion": {"claim_state": {"state": "candidate-submitted"}}}}
    earlier = {"calls": 2, "structured_output": {"errors": ["prior invalid shape"]}}
    assert classify(shown, [earlier], []) == ("ok", None)
    assert investigator_abstention_reason(shown, "ok") is None
    assert classify(stopped("unknown"), [], [])[0] == "crash"


def test_early_no_lock_abstention_does_not_claim_boundary_or_unknown_eligibility():
    shown = {"status": "concluded", "body": {"conclusion": {"claim_state": {"state": "abstained"}}},
             "finalization": {"trigger": "refused-proposals"},
             "finalization_export_receipt": {"investigator_concluded": True, "locked_candidate": None}}
    assert investigator_abstention_reason(shown, "ok") == "no-eligible-lock-at-finalization"
    shown["finalization_export_receipt"]["locked_candidate"] = {"id": "fixture"}
    assert investigator_abstention_reason(shown, "ok") is None
    del shown["finalization_export_receipt"]["locked_candidate"]
    assert investigator_abstention_reason(shown, "ok") is None

def test_ordinary_search_ceiling_is_not_a_wall_boundary():
    shown = {"status": "concluded", "body": {"conclusion": {"incumbent": None, "claim_state": {"state": "abstained"}}},
             "finalization_phase": {"trigger": "perfect-score"}}
    assert investigator_abstention_reason(shown, "ok") == "no-eligible-lock-at-finalization"
