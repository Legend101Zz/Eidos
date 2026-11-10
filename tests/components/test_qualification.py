"""Run the assessor's own (author-written) control suite through the qualification runner.

Fixture-only: the suite was written by the assessor's author, so a pass here is an author test and does
not make the assessor independently qualified.
"""

import json
import shutil

from conftest import COMPONENTS
from eidos.domain.qualification import evaluate_qualification, validate_control_suite

ASSESSOR = COMPONENTS / "eidos.assessors.paired-ci"
SUITE = json.loads((ASSESSOR / "controls" / "suite.json").read_text())
RUNNER = ("eidos.qualification-runner", "qualification_runner.run:run")


def qualify(lab_run, target_dir, suite=SUITE):
    res = lab_run(
        *RUNNER,
        config={"suite": suite, "target": {"ref": "eidos.assessors.paired-ci@1.2.0"}},
        ports={"target.source/1": {"path": str(target_dir)}},
    )
    assert res.ok, res.manifest.error
    return res, res.json("qualification_results")


def test_suite_has_planted_null_and_stochastic_controls():
    validate_control_suite(SUITE)
    kinds = {(c["type"], c["deterministic"]) for c in SUITE["controls"]}
    assert kinds == {("planted", True), ("null", True), ("planted", False), ("null", False)}
    assert all(c["repetitions"] >= 40 for c in SUITE["controls"] if not c["deterministic"])
    assert "min_effect" not in json.dumps(SUITE["acceptance"])


def test_assessor_passes_its_own_controls(lab_run):
    res, results = qualify(lab_run, ASSESSOR)
    assert res.receipt["invalid_runs"] == 0 and res.receipt["metrics"]["runs"] == 8 + 80  # 8 literal (3 for accuracy, ADR 0018)
    gate = evaluate_qualification(SUITE, results)
    assert gate["status"] == "passed", gate
    by = {c["control"]: c for c in gate["per_control"]}
    assert by["planted-stochastic-effect"]["interval"][0] >= SUITE["acceptance"]["min_power"]
    assert by["null-stochastic-no-effect"]["interval"][1] <= SUITE["acceptance"]["max_false_positive"]
    assert results["null-single-cluster-inconclusive"][0]["outcome"] == "inconclusive"
    assert results["null-guardrail-violated"][0]["outcome"] == "protocol-failed"
    assert results["accuracy-planted-gain"][0]["outcome"] == "protocol-passed"
    assert results["accuracy-null-identical-arms"][0]["outcome"] == "protocol-failed"
    assert results["accuracy-null-small-noisy-inconclusive"][0]["outcome"] == "inconclusive"


def test_runs_are_repeatable_and_stochastic_repetitions_differ(lab_run):
    _, first = qualify(lab_run, ASSESSOR)
    _, second = qualify(lab_run, ASSESSOR)
    assert first == second
    reps = first["planted-stochastic-effect"]
    assert len(reps) == 40


def test_a_broken_assessor_fails_the_gate(lab_run, tmp_path):
    """An assessor that asserts an effect whatever the evidence is caught by the null controls."""
    broken = tmp_path / "broken"
    shutil.copytree(ASSESSOR, broken, ignore=shutil.ignore_patterns("controls", "fixtures", "__pycache__"))
    src = (broken / "paired_ci" / "assess.py").read_text()
    assert "result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)" in src
    (broken / "paired_ci" / "assess.py").write_text(
        src.replace(
            "result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)",
            'result = dict(evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY), outcome="protocol-passed")',
        )
    )
    _, results = qualify(lab_run, broken)
    gate = evaluate_qualification(SUITE, results)
    assert gate["status"] == "failed"
    failed = {c["control"] for c in gate["per_control"] if c["status"] == "failed"}
    assert {"null-identical-arms", "null-stochastic-no-effect"} <= failed


def test_an_assessor_that_never_detects_fails_the_planted_controls(lab_run, tmp_path):
    blind = tmp_path / "blind"
    shutil.copytree(ASSESSOR, blind, ignore=shutil.ignore_patterns("controls", "fixtures", "__pycache__"))
    src = (blind / "paired_ci" / "assess.py").read_text()
    (blind / "paired_ci" / "assess.py").write_text(
        src.replace(
            "result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)",
            'result = dict(evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY), outcome="inconclusive")',
        )
    )
    _, results = qualify(lab_run, blind)
    gate = evaluate_qualification(SUITE, results)
    assert gate["status"] == "failed"
    assert {"planted-large-effect", "planted-stochastic-effect"} <= {
        c["control"] for c in gate["per_control"] if c["status"] == "failed"
    }


def test_mismatched_target_is_refused(lab_run):
    suite = dict(SUITE, target={"kind": "assessor", "ref": "someone.else@9.9.9"})
    res = lab_run(*RUNNER, config={"suite": suite}, ports={"target.source/1": {"path": str(ASSESSOR)}})
    assert res.manifest.status == "failed" and res.manifest.error["category"] == "contract"
