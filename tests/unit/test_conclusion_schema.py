"""The published ``conclusion`` record schema (ADR 0021): schemas/eidos-wire/v1/conclusion.schema.json.

(a) valid fixtures (tests/fixtures/conclusion, generated once from real ``derive_claim_state`` output and kept as static
JSON, plus a record from before ADR 0021) validate against the published schema and against the server's boundary
model; invalid ones fail both;
(b) every claim state the rules can derive passes inside a ``conclusion`` record;
(c) unknown fields are accepted (forward compatibility), as for the worker messages.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import eidos_wire
from eidos.boundary import wire
from eidos.domain import assessment as A

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas" / "eidos-wire" / f"v{eidos_wire.WIRE_MAJOR}"
SCHEMA_FILE = SCHEMAS / "conclusion.schema.json"
FIXTURES = ROOT / "tests" / "fixtures" / "conclusion"
CLAIM_ID = "clm_schema_0001"


def _fixtures(valid: bool) -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json") if p.name.startswith("invalid-") != valid)


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_FILE.read_text()))


def _schema_errors(data: Any) -> list[str]:
    return [e.message for e in _validator().iter_errors(data)]


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# -- the schema file -----------------------------------------------------------------------------------------------
def test_the_schema_file_set_includes_conclusion_and_it_is_a_record_not_a_message():
    assert "conclusion.schema.json" in {p.name for p in SCHEMAS.glob("*.schema.json")}
    assert "conclusion" in wire.RECORDS and "conclusion" in wire.PUBLISHED
    assert "conclusion" not in wire.MESSAGES  # not a worker message
    doc = json.loads(SCHEMA_FILE.read_text())
    Draft202012Validator.check_schema(doc)
    assert doc["$id"] == f"urn:eidos:schema:eidos-wire:v{eidos_wire.WIRE_MAJOR}:conclusion"
    assert doc["x-eidos"]["source"] == "eidos.boundary.wire.Conclusion"


def test_the_schema_pins_the_claim_state_vocabulary_to_the_domain_rules():
    doc = json.loads(SCHEMA_FILE.read_text())
    claim = doc["$defs"]["ConclusionClaim"]["properties"]
    assert set(claim["state"]["enum"]) == set(A.CLAIM_STATES)
    assert claim["schema"]["const"] == A.CLAIM_STATE_SCHEMA
    assert doc["properties"]["schema"]["anyOf"][0]["const"] == A.CONCLUSION_SCHEMA
    dispositions = [alt["enum"] for alt in claim["requested_disposition"]["anyOf"] if "enum" in alt]
    assert dispositions == [list(A.DISPOSITIONS)]
    assert set(doc["required"]) == {"turn_id", "summary", "action"}
    assert set(doc["$defs"]["ConclusionClaim"]["required"]) == {"schema", "state", "confirmed", "label"}


# -- the fixtures --------------------------------------------------------------------------------------------------
def test_the_fixture_set_has_the_valid_and_invalid_records_it_documents():
    assert {p.stem for p in _fixtures(True)} == {"conclusion-protocol-passed", "conclusion-candidate-submitted",
                                                 "conclusion-not-supported", "conclusion-legacy"}
    assert {p.stem for p in _fixtures(False)} == {"invalid-conclusion-state", "invalid-conclusion-action"}


@pytest.mark.parametrize("path", _fixtures(True), ids=lambda p: p.stem)
def test_valid_fixtures_pass_the_schema_and_the_server_validator(path):
    data = json.loads(path.read_text())
    assert not _schema_errors(data)
    wire.validate_wire("conclusion", data)


@pytest.mark.parametrize("path", _fixtures(False), ids=lambda p: p.stem)
def test_invalid_fixtures_fail_the_schema_and_the_server_validator(path):
    data = json.loads(path.read_text())
    assert _schema_errors(data), f"{path.name} unexpectedly satisfies the schema"
    with pytest.raises(wire.WireSchemaError) as exc:
        wire.validate_wire("conclusion", data)
    assert exc.value.kind == "conclusion" and exc.value.problems
    assert str(exc.value).startswith("conclusion violates the eidos-wire/1 schema: ")


def test_the_invalid_fixtures_are_invalid_for_the_reason_they_name():
    with pytest.raises(wire.WireSchemaError) as state:
        wire.validate_wire("conclusion", _load("invalid-conclusion-state"))
    assert any(p.startswith("claim_state.state") for p in state.value.problems)
    with pytest.raises(wire.WireSchemaError) as action:
        wire.validate_wire("conclusion", _load("invalid-conclusion-action"))
    assert any(p.startswith("action") for p in action.value.problems)


@pytest.mark.parametrize(("name", "state", "confirmed"), [
    ("conclusion-protocol-passed", "protocol-passed", True),
    ("conclusion-candidate-submitted", "candidate-submitted", False),
    ("conclusion-not-supported", "not-supported", False),
])
def test_the_generated_fixtures_carry_the_state_they_are_named_for(name, state, confirmed):
    data = _load(name)
    assert data["schema"] == A.CONCLUSION_SCHEMA and data["claim_state"]["schema"] == A.CLAIM_STATE_SCHEMA
    assert data["claim_state"]["state"] == state and data["claim_state"]["confirmed"] is confirmed
    assert data["claim_state"]["claim_id"] == "clm_fixture_0001"
    json.dumps(data, allow_nan=False)  # strict JSON: no NaN or infinity


def test_the_legacy_record_has_no_schema_and_no_claim_state():
    legacy = _load("conclusion-legacy")
    assert set(legacy) == {"turn_id", "summary", "scope", "action"}
    parsed = wire.Conclusion.model_validate(legacy)
    assert parsed.schema_ is None and parsed.claim_state is None


def test_only_the_downgraded_fixture_carries_a_downgrade():
    not_supported = _load("conclusion-not-supported")["claim_state"]
    assert not_supported["requested_disposition"] == "confirmed" and not_supported["downgraded_from"] == "confirmed"
    assert "downgraded_from" not in _load("conclusion-protocol-passed")["claim_state"]


# -- real derive_claim_state output in a conclusion record ---------------------------------------------------------
def _claim(**over: Any) -> dict[str, Any]:
    return {"id": CLAIM_ID, "revision": 1, "statement": "the candidate raises held-out accuracy",
            "scope": {"task": "toy-3class"}, **over}


def _assessment(outcome: str = "protocol-passed", **over: Any) -> dict[str, Any]:
    effect = {"estimate": 0.04, "ci_low": 0.01, "ci_high": 0.07, "method": "cluster-robust normal"}
    return {"id": "asm_0001", "claim_id": CLAIM_ID, "claim_revision": 1, "plan_id": "plan_0001", "outcome": outcome,
            "conformance": "conformant", "health": "intact", "success_rule_ref": "practical-accuracy-gain@1",
            "evaluation": {"metric": "classification-report/1:accuracy", "direction": "increase", "effect": effect},
            "scope": {"task": "toy-3class"}, "remaining_alternatives": ["population-specific gain"], **over}


def _derive(control: dict[str, Any], *, claims: list | None = None, assessments: list | None = None,
            plans: list | None = None) -> dict[str, Any]:
    charter = {"population": "held-out examples", "success_rules": [{"decision_standard": {"confidence": 0.95}}]}
    if plans is None:  # each assessment ran on a plan that was verified at freeze as able to confirm
        plans = [{"plan_id": a["plan_id"], "status": "assessed", "obligations": [], "confirmation": {"eligible": True}}
                 for a in assessments or []]
    return A.derive_claim_state(control=control, claims=[_claim()] if claims is None else claims,
                                assessments=assessments or [], plans=plans, charter=charter)


def _record(claim_state: dict[str, Any] | None, action: str = "conclude") -> dict[str, Any]:
    record = {"schema": A.CONCLUSION_SCHEMA, "turn_id": "turn_schema_0001", "summary": "s", "scope": {"a": 1},
              "action": action}
    return record if claim_state is None else {**record, "claim_state": claim_state}


REAL_STATES = {
    "protocol-passed": lambda: _derive({"claim": {"disposition": "confirmed"}}, assessments=[_assessment()]),
    "candidate-submitted": lambda: _derive({"claim": {"disposition": "candidate", "point_estimate": {
        "baseline": 0.5, "candidate": 0.6}}}, plans=[{"plan_id": "plan_0001", "status": "frozen", "obligations": ["x"]}]),
    "not-supported": lambda: _derive({"claim": {"disposition": "confirmed"}},
                                     assessments=[_assessment("protocol-failed")]),
    "abstained": lambda: _derive({"claim": {"disposition": "abstain", "evidence_refs": ["art_1"]}},
                                 assessments=[_assessment()]),
    "null-reported": lambda: _derive({"claim": {"disposition": "null", "point_estimate": {"value": 0.0}}}),
    "no-claim": lambda: _derive({}, claims=[]),
}


def test_the_real_states_cover_the_whole_vocabulary():
    assert set(REAL_STATES) == set(A.CLAIM_STATES)


@pytest.mark.parametrize("state", sorted(REAL_STATES))
def test_real_derive_claim_state_output_is_a_valid_conclusion_for_every_state(state):
    claim_state = REAL_STATES[state]()
    assert claim_state["state"] == state
    record = _record(claim_state)
    assert not _schema_errors(record)
    wire.validate_wire("conclusion", record)
    assert not _schema_errors(_record(claim_state, action="stop"))


@pytest.mark.parametrize("state", sorted(REAL_STATES))
def test_real_output_declares_only_properties_the_schema_names(state):
    """Unknown fields are tolerated on receipt, so a field the rules add would pass silently; this catches it."""
    declared = set(json.loads(SCHEMA_FILE.read_text())["$defs"]["ConclusionClaim"]["properties"])
    assert set(REAL_STATES[state]()) <= declared


def test_a_downgraded_confirmation_and_an_unbounded_interval_are_valid_records():
    downgraded = _derive({"claim": {"disposition": "confirmed"}})
    assert downgraded["downgraded_from"] == "confirmed"
    unbounded_effect = {"estimate": 0.04, "ci_low": float("-inf"), "ci_high": float("inf")}
    unbounded = _derive({"claim": {"disposition": "confirmed"}},
                        assessments=[_assessment(evaluation={"metric": "m", "direction": "increase",
                                                             "effect": unbounded_effect})])
    assert unbounded["uncertainty"]["low"] is None and unbounded["uncertainty"]["high"] is None
    for claim_state in (downgraded, unbounded):
        record = _record(claim_state)
        assert not _schema_errors(record)
        wire.validate_wire("conclusion", record)


def test_a_designated_but_unknown_claim_is_a_valid_record():
    claim_state = _derive({"claim": {"disposition": "candidate", "claim_id": "clm_missing"}})
    assert claim_state["claim_id"] is None
    wire.validate_wire("conclusion", _record(claim_state))


# -- the schema refuses what the rules never produce ---------------------------------------------------------------
@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("turn_id"),
    lambda r: r.pop("summary"),
    lambda r: r.pop("action"),
    lambda r: r.update(schema="conclusion/1"),
    lambda r: r.update(action="cancel"),
    lambda r: r.update(turn_id=7),
    lambda r: r["claim_state"].update(state="confirmed"),
    lambda r: r["claim_state"].pop("state"),
    lambda r: r["claim_state"].pop("confirmed"),
    lambda r: r["claim_state"].pop("label"),
    lambda r: r["claim_state"].update(confirmed="yes"),
    lambda r: r["claim_state"].update(schema="conclusion-claim/2"),
    lambda r: r["claim_state"].update(requested_disposition="proven"),
    lambda r: r["claim_state"].update(downgraded_from="maybe"),
    lambda r: r["claim_state"].update(claim_revision="one"),
    lambda r: r["claim_state"].update(point_estimate=0.4),
    lambda r: r["claim_state"].update(unfulfilled_obligations="independent confirmation"),
    lambda r: r["claim_state"].update(evidence_refs=[1]),
    lambda r: r["claim_state"].update(assessments_considered=["asm_1"]),
    lambda r: r.update(claim_state="protocol-passed"),
])
def test_malformed_records_fail_the_schema_and_the_server_validator(mutate):
    record = _record(REAL_STATES["candidate-submitted"]())
    mutate(record)
    assert _schema_errors(record)
    with pytest.raises(wire.WireSchemaError):
        wire.validate_wire("conclusion", record)


def test_a_non_object_is_not_a_conclusion():
    for data in (None, [], "conclusion", 3):
        assert _schema_errors(data)
        with pytest.raises(wire.WireSchemaError):
            wire.validate_wire("conclusion", data)


def test_non_finite_numbers_are_refused_as_not_strict_json():
    record = _record(REAL_STATES["candidate-submitted"]())
    record["claim_state"]["point_estimate"]["value"] = float("nan")
    with pytest.raises(wire.WireSchemaError, match="not strict JSON"):
        wire.validate_wire("conclusion", record)


# -- forward compatibility -----------------------------------------------------------------------------------------
def test_unknown_fields_are_accepted_at_every_level():
    record = copy.deepcopy(_load("conclusion-protocol-passed"))
    record["added_in_a_later_minor"] = {"a": 1}
    record["claim_state"]["added_in_a_later_minor"] = ["x"]
    assert not _schema_errors(record)
    wire.validate_wire("conclusion", record)
    legacy = {**_load("conclusion-legacy"), "future_field": True}
    assert not _schema_errors(legacy)
    wire.validate_wire("conclusion", legacy)
    # accepted and ignored: the boundary model does not carry them
    assert "added_in_a_later_minor" not in wire.Conclusion.model_validate(record).model_dump(by_alias=True)
