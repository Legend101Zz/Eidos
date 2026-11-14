"""Treatment and controller isolation contracts, with no provider or installed lab."""
import json
from unittest.mock import Mock

from study.eidos_arms import arm4, provider_bridge
from study.exp.treatment import resolve


def test_autopilot_treatment_resolves_to_fixture_without_changing_default():
    baseline = resolve("pilot4-v1")
    treatment = resolve("autopilot-v2")
    assert baseline.investigator["runtime"] == "pydantic-ai"
    assert treatment.flags == resolve("v2-portfolio").flags
    assert treatment.investigator["runtime"] == "fixture"
    assert treatment.investigator["settings"]["policy"] == "autopilot-v2"
    assert "investigator" not in treatment.investigator


def test_fixture_study_run_records_no_provider_and_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOS_STUDY_TREATMENT", "autopilot-v2")
    resolved = resolve("autopilot-v2")
    shown = dict(status="concluded", turns=[], jobs=[],
                 body=dict(conclusion=dict(summary="Fixture null.", scope={}, claim_state=dict(state="null-reported"))))
    client = Mock()
    client.query.return_value = shown
    client.command.return_value = dict(result=dict(investigation_id="fixture-inv"))
    start = Mock()
    monkeypatch.setattr(arm4.lab, "start", start)
    monkeypatch.setattr(arm4.lab, "client", lambda: client)
    monkeypatch.setattr(arm4.lab, "guard_disk", lambda: None)
    keychain = Mock(side_effect=AssertionError("Keychain forbidden"))
    bridge = Mock(side_effect=AssertionError("bridge forbidden"))
    monkeypatch.setattr(arm4.lab.subprocess, "check_output", keychain)
    monkeypatch.setattr(provider_bridge, "start", bridge)
    monkeypatch.setattr(arm4, "prepare", lambda *a: ("fixture-project", dict(charter_id="charter", artifacts={}, task={})))
    monkeypatch.setattr(arm4, "prompt", lambda *a: "fixture prompt")
    monkeypatch.setattr("study.common.record_live_delivery", lambda *a: None)
    monkeypatch.setattr(arm4, "ledger", lambda *a: (shown, [], dict(note=[], assessment=[], evidence_plan=[])))
    monkeypatch.setattr(arm4, "usage", lambda *a: ({}, [], 0, []))
    monkeypatch.setattr(arm4, "select_job", lambda *a: ("baseline", "fixture baseline"))
    monkeypatch.setattr(arm4, "candidate_provenance", lambda *a: {})
    monkeypatch.setattr(arm4, "export_answer", lambda *a, **k: "model.pt")
    monkeypatch.setattr(arm4, "validate", lambda *a: True)
    (tmp_path / "task").mkdir()
    record = arm4.run(tmp_path, dict(treatment=resolved.record(), envelope=dict(wall_s=30), card="D03"))
    assert record["status"] == "ok"
    assert record["provider"] == "none (fixture runtime)" and record["policy"] == "autopilot-v2"
    assert record["provider_endpoint"] == "none (fixture runtime)" and record["provider_outer_kill"] is False
    setup = json.loads((tmp_path / "setup.json").read_text())
    assert setup["provider"] == "none (fixture runtime)" and setup["policy"] == "autopilot-v2"
    start.assert_called_once_with(profile=resolved.provider, credentials=False)
    keychain.assert_not_called()
    bridge.assert_not_called()
