"""No-model verification of common and adapter-delivered prompt provenance."""

import json
from hashlib import sha256

import pytest
from study import launcher
from study.common import record_live_delivery, record_prompt, render_prompt
from study.exp.treatment import resolve


@pytest.mark.parametrize("arm", (1, 2, 3, 4))
def test_expected_and_exact_delivered_hashes_are_separate(tmp_path, monkeypatch, arm):
    run = tmp_path / f"arm{arm}"
    run.mkdir()
    meta = dict(treatment=resolve().record(), block_id="N05-r1", card="N05", run_index=1, arm=arm,
                charter_amendment_commit="a" * 40, charter_amendment_sha256="b" * 64)
    normal = render_prompt(arm, "N05")
    record_prompt(run, meta)
    monkeypatch.setenv("EIDOS_STUDY_LIVE_DISPATCH", "1")
    delivered = normal + "\n\nPackaged task files:\n{}" if arm == 4 else normal
    surface = "investigation-question" if arm == 4 else "claude-prompt"
    record_live_delivery(run, meta, delivered, surface)
    outside = run.parent / "prompt-receipts"
    expected = json.loads((outside / f"arm{arm}-expected.json").read_bytes())
    actual = json.loads((outside / f"arm{arm}-delivered.json").read_bytes())
    assert expected["expected_render_sha256"] == sha256(normal.encode()).hexdigest()
    assert actual["delivered_sha256"] == sha256(delivered.encode()).hexdigest()
    assert actual["common_template_sha256"] == expected["common_template_sha256"]
    assert not list(run.iterdir())


@pytest.mark.parametrize("override", ("EIDOS_STUDY_SMOKE_PROMPT", "EIDOS_STUDY_LANE_A_ROOT"))
def test_live_launcher_refuses_override_before_writing(tmp_path, monkeypatch, override):
    monkeypatch.setenv(override, "")
    with pytest.raises(ValueError, match="forbids smoke"):
        launcher.run_block([{"block_id": "N05-r1"}], tmp_path)
    assert not list(tmp_path.iterdir())


def test_mismatched_live_delivery_is_refused(tmp_path):
    run = tmp_path / "arm1"
    run.mkdir()
    meta = dict(treatment=resolve().record(), block_id="N05-r1", card="N05", run_index=1, arm=1,
                charter_amendment_commit="a" * 40, charter_amendment_sha256="b" * 64)
    record_prompt(run, meta)
    with pytest.raises(ValueError, match="differs"):
        record_prompt(run, meta, delivered="altered", surface="claude-prompt")


def test_installed_worker_environment_preserves_smoke_but_refuses_live_override(monkeypatch):
    from study.eidos_arms import lab

    monkeypatch.setenv("EIDOS_STUDY_SMOKE_PROMPT", "offline-fixture")
    monkeypatch.delenv("EIDOS_STUDY_LIVE_DISPATCH", raising=False)
    assert lab.environment()["EIDOS_STUDY_SMOKE_PROMPT"] == "offline-fixture"
    monkeypatch.setenv("EIDOS_STUDY_LIVE_DISPATCH", "1")
    with pytest.raises(ValueError, match="forbids smoke"):
        lab.environment()
