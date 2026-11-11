"""ADR 0024: the investigator's guide orders diagnosis, a broad template sweep and targeted challengers; diagnosis results
are pinned with sweeps; a component config contract can offer alternatives (``anyOf``), as the recipe sweep's template
mode needs. Author tests of constants and pure functions: no database, no models, no network."""

from __future__ import annotations

import json
import re
from pathlib import Path

from eidos.domain.contracts import validate_config
from eidos.investigator.context import TOOL_GUIDE, _sweep_facts

ROOT = Path(__file__).resolve().parents[2]
SWEEP_SCHEMA = json.loads((ROOT / "components/eidos.search.recipe-sweep/eidos-component.json").read_text())["config_schema"]


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_the_guide_orders_diagnosis_then_a_template_sweep_then_challengers():
    guide = flat(TOOL_GUIDE)
    first, second, third = (guide.index(s) for s in ("(1) After the baseline, eidos.diagnose.baseline-errors",
                                                      "(2) A broad sweep in ONE call", "(3) Targeted challengers"))
    assert first < second < third
    assert 'template: {name: "quick"|"broad"' in guide and "no authored code" in guide
    assert "additive offset" in guide and "spatial/positional shift" in guide and "information loss" in guide
    assert "a prediction and a falsifier" in guide
    assert "exploratory: no claim, no test data. Lock the winner before confirming it." in guide


def test_a_diagnosis_job_is_pinned_with_its_top_hypothesis_and_report():
    view = {"jobs": [
        {"id": "job-d", "component": "eidos.diagnose.baseline-errors@1.0.0", "status": "succeeded",
         "receipt": {"top_hypothesis": "per-example-additive-offset",
                     "hypotheses": ["per-example-additive-offset", "no-transfer-shift-detected"]},
         "outputs": [{"slot": "report", "artifact_id": "art-report"}]},
        {"id": "job-s", "component": "eidos.search.recipe-sweep@1.1.0", "status": "succeeded",
         "receipt": {"metrics": {"best_objective": 0.9}},
         "outputs": [{"slot": "best_recipe", "artifact_id": "art-best"}, {"slot": "evidence_table", "artifact_id": "art-t"}]},
        {"id": "job-o", "component": "eidos.project.command@1.0.0", "status": "succeeded", "receipt": {}, "outputs": []},
    ]}
    facts = _sweep_facts(view)
    assert len(facts) == 2
    assert facts[0].startswith("- Diagnosis job-d (succeeded): report art-report, top hypothesis per-example-additive-offset")
    assert "Exploratory" in facts[0] and "falsifier" in facts[0]
    assert facts[1].startswith("- Sweep job-s (succeeded)") and "art-best" in facts[1]


def test_a_failed_diagnosis_without_outputs_is_still_named():
    facts = _sweep_facts({"jobs": [{"id": "job-x", "component": "eidos.diagnose.baseline-errors@1.0.0",
                                     "status": "failed", "receipt": None, "outputs": []}]})
    assert facts == [facts[0]] and "job-x (failed): report None, top hypothesis None" in facts[0]


def test_any_of_accepts_either_alternative_and_reports_both_when_neither_matches():
    assert validate_config({"template": {"name": "quick"}}, SWEEP_SCHEMA) == []
    assert validate_config(SWEEP_SCHEMA["examples"][0], SWEEP_SCHEMA) == []
    problems = validate_config({"stopping": {"rule": "none"}}, SWEEP_SCHEMA)
    assert len(problems) == 1 and "matches none of its alternatives" in problems[0]
    assert "config.template: required" in problems[0] and "config.command: required" in problems[0]


def test_any_of_does_not_relax_the_rest_of_the_schema():
    problems = validate_config({"template": {"name": "everything", "extra": 1}}, SWEEP_SCHEMA)
    assert "config.template.name: 'everything' not one of ['quick', 'broad']" in problems
    assert "config.template.extra: not allowed" in problems
    schema = {"type": "object", "required": ["a"], "anyOf": [{"required": ["b"]}, {"required": ["c"]}]}
    assert validate_config({"a": 1, "b": 2}, schema) == []
    assert validate_config({"b": 2}, schema) == ["config.a: required"]
    assert validate_config({"a": 1}, schema)[0].startswith("config: matches none of its alternatives")


def test_the_template_schema_is_typed():
    template = SWEEP_SCHEMA["properties"]["template"]
    assert template["additionalProperties"] is False and template["required"] == ["name"]
    assert set(template["properties"]["families"]["items"]["enum"]) == {"linear", "knn", "kernel", "mlp", "svm", "gbm"}
    assert set(template["properties"]["preprocess"]["items"]["enum"]) == {
        "none", "standardize", "per_example_center", "per_example_standardize"}
    for bad in ({"template": {"name": "quick", "families": ["forest"]}},
                {"template": {"name": "quick", "preprocess": ["whiten"]}},
                {"template": {"name": "quick", "rank_by": "test"}},
                {"template": {"name": "quick", "cv_folds": 1}}):
        assert validate_config(bad, SWEEP_SCHEMA), bad
