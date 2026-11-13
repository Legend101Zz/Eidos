"""Published portfolio bundles execute through the real ledger and worker runner."""

import json

import pytest
import test_diagnose_and_template_sweep_lab as template_lab
from test_command_path import _publish
from test_diagnose_and_template_sweep_lab import ROOT, RUN, _bundle, _output, _run

from eidos.cli.main import bundle_dir
from eidos.domain.errors import ResolutionError

scene = template_lab.scene


@pytest.mark.pg
@pytest.mark.parametrize("kind", ["classification", "regression"])
def test_portfolio_table_oof_and_generated_source_are_real_job_outputs(scene, kind):
    name = "eidos.search.recipe-portfolio"
    scene["refs"][name] = _publish(scene["lab"], scene["pid"], *_bundle(name),
                                   bundle_bytes=bundle_dir(ROOT / "components" / name))["component_ref"]
    job = _run(scene, name, dict(mode="portfolio", task_type=kind, include=["linear", "neighbors"],
                                 scheme="cv-train", cv_folds=3, cv_repeats=1, budget=dict(max_seconds=60)))
    assert job["status"] == "succeeded", job.get("terminal")
    table = _output(scene, job, "portfolio_table")
    oof = _output(scene, job, "oof")
    source = _output(scene, job, "promotion_source")
    assert table["coverage_complete"] and oof["recipes"]
    assert table["library_digest"] == source["library_digest"] == oof["library_digest"]
    assert table["selection"]["basis"] == source["basis"] == "one-se-choice"
    assert source["claim"] is None and source["exploratory"] is True
    assert source["files"]["project.py"] == (ROOT / "components" / name / "templates/project.py").read_text()
    assert json.loads(source["files"]["recipe.json"])["task_type"] == kind
    assert job["attempts"][-1]["receipt"]["metrics"]["round_evaluated"] == 1.


@pytest.mark.pg
def test_portfolio_mode_is_checked_at_acceptance(scene):
    name = "eidos.search.recipe-portfolio"
    scene["refs"][name] = _publish(scene["lab"], scene["pid"], *_bundle(name),
                                   bundle_bytes=bundle_dir(ROOT / "components" / name))["component_ref"]
    with pytest.raises(ResolutionError):
        scene["lab"].cmd(scene["lab"].admin, "run_component", {
            "component_ref": scene["refs"][name], **RUN, "config": {"mode": "confirm"},
            "inputs": dict(scene["arts"]),
        }, project=scene["pid"])
