"""Drift guard for assessment by default (ADR 0021).

``eidos.domain.assessment.classification_draft`` and ``train_evaluate_draft`` reproduce the draft expansion of
``eidos.templates.improves-under-budget`` (its classification and train/evaluate paths) so that ``assess_candidate``
can answer in one transaction. These tests run the real template through the worker protocol and require the two to
produce the same draft, key for key, for requests of the shape the domain functions expect. Author tests.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from typing import Any

import pytest

from conftest import COMPONENTS
from eidos.domain import assessment as A

TEMPLATE = ("eidos.templates.improves-under-budget", "improves_template.expand:run")
FIXTURES = COMPONENTS / "eidos.templates.improves-under-budget" / "fixtures"


def _template_module():
    path = str(COMPONENTS / "eidos.templates.improves-under-budget")
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("improves_template.expand")


expand = _template_module()


def _request(fixture: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{fixture}.json").read_text())["inputs"]["request"]["json"]


def _template_draft(lab_run, request: dict[str, Any]) -> dict[str, Any]:
    res = lab_run(*TEMPLATE, inputs={"request": request}, config={})
    assert res.ok, res.manifest.error
    assert res.manifest.outputs[0].schema == "evidence-plan/1"
    return res.json("draft")


def _manifest(component: str) -> dict[str, Any]:
    return json.loads((COMPONENTS / component / "eidos-component.json").read_text())


# -- classification ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fixture", ["classification-three-seeds", "classification-one-seed"])
def test_the_classification_draft_equals_the_templates(lab_run, fixture):
    request = _request(fixture)
    template = _template_draft(lab_run, request)
    assert A.classification_draft(copy.deepcopy(request)) == template
    assert template["obligations"] == []  # assessor and required_isolation are present


def test_the_classification_draft_equals_the_templates_for_a_fully_specified_request(lab_run):
    request = _request("classification-three-seeds")
    request.update({
        "tuning": {"split": "validation", "used_for": "checkpoint selection"},
        "budget": {"steps": 45, "wall_seconds": 600},
        "alternatives_unresolved": ["gain explained by a different data order"],
        "baseline": {"description": "reference recipe, five-epoch"},
        "candidate": {"description": "candidate with label smoothing"},
    })
    request["classification"]["id_field"] = "uid"
    template = _template_draft(lab_run, request)
    assert A.classification_draft(copy.deepcopy(request)) == template
    assert template["alternatives_unresolved"] == ["gain explained by a different data order"]
    assert [a["description"] for a in template["arms"]] == ["reference recipe, five-epoch",
                                                           "candidate with label smoothing"]


def test_the_classification_draft_equals_the_templates_for_numeric_and_text_seeds_in_any_key_order(lab_run):
    request = _request("classification-three-seeds")
    seeds = ["10", "9", "11", "seed-b", "seed-a"]
    request["classification"]["predictions"] = {
        "baseline": {s: f"art_b_{s}" for s in seeds},
        "candidate": {s: f"art_c_{s}" for s in reversed(seeds)},
    }
    template = _template_draft(lab_run, request)
    assert template["seed_policy"]["seeds"] == [9, 10, 11, "seed-a", "seed-b"]
    assert A.classification_draft(copy.deepcopy(request)) == template


def test_without_an_assessor_or_isolation_the_template_only_adds_obligations(lab_run):
    """The domain function is called after the command has resolved both; the template records them as obligations."""
    request = _request("classification-one-seed")
    request.pop("assessor")
    request.pop("required_isolation")
    template = _template_draft(lab_run, request)
    assert template["obligations"] == ["required_isolation", "assessor"]
    draft = A.classification_draft(copy.deepcopy(request))
    assert draft == {**template, "obligations": []}


# -- train / evaluate ----------------------------------------------------------------------------------------------
def _train_evaluate_request() -> dict[str, Any]:
    request = _request("from-scratch")
    request["seeds"] = [41, 42, 43]  # explicit integer seeds
    return request


def test_the_train_evaluate_draft_equals_the_templates_for_explicit_seeds(lab_run):
    request = _train_evaluate_request()
    assert "parent" not in request and request["evaluation"]
    template = _template_draft(lab_run, request)
    assert A.train_evaluate_draft(copy.deepcopy(request)) == template
    assert template["obligations"] == []
    assert template["analysis_unit"] == "pair" and template["seed_policy"]["seeds"] == [41, 42, 43]


def test_the_template_expands_a_seed_count_to_the_same_draft_as_the_explicit_seeds(lab_run):
    counted = _request("from-scratch")
    assert counted["seeds"] == {"count": 3, "start": 41}
    template = _template_draft(lab_run, counted)
    assert A.train_evaluate_draft(_train_evaluate_request()) == template


@pytest.mark.parametrize("edit", [
    pytest.param(lambda r: r["candidate"].pop("post"), id="no-post-step"),
    pytest.param(lambda r: r["baseline"].update(post=[{"component": "eidos.pytorch.weight-edit",
                                                       "config": {"edits": []}}, {"component": "x.post"}]),
                 id="two-baseline-post-steps"),
    pytest.param(lambda r: r["candidate"].update(train={"steps": 90, "lr": 0.01}), id="candidate-train-overrides"),
    pytest.param(lambda r: (r.pop("common_train"), r.pop("common_evaluate")), id="no-common-config"),
    pytest.param(lambda r: r.update(tuning={"split": "validation"}, budget={"steps": 45, "seconds": 90},
                                    alternatives_unresolved=["only this one"]), id="tuning-budget-alternatives"),
    pytest.param(lambda r: r.update(seeds=[7]), id="one-seed"),
    pytest.param(lambda r: r.pop("baseline"), id="no-baseline-recipe"),
])
def test_the_train_evaluate_draft_equals_the_templates_across_recipe_variations(lab_run, edit):
    request = _train_evaluate_request()
    edit(request)
    template = _template_draft(lab_run, request)
    assert A.train_evaluate_draft(copy.deepcopy(request)) == template


def test_a_candidate_post_step_chains_from_the_previous_job_in_both(lab_run):
    request = _train_evaluate_request()
    template = _template_draft(lab_run, request)
    ours = A.train_evaluate_draft(copy.deepcopy(request))
    for draft in (template, ours):
        candidate = next(a for a in draft["arms"] if a["role"] == "candidate")
        jobs = candidate["runs"][0]["jobs"]
        assert [j["id"] for j in jobs] == ["candidate.s41.train", "candidate.s41.post0", "candidate.s41.evaluate"]
        assert jobs[1]["inputs"] == {"checkpoint": "candidate.s41.train.checkpoint"}
        assert jobs[2]["inputs"] == {"checkpoint": "candidate.s41.post0.checkpoint"}
        assert jobs[2]["config"]["evaluation"]["cluster"] == "41"


# -- the shared vocabulary -----------------------------------------------------------------------------------------
def test_the_domain_constants_are_the_templates():
    assert (A.TRAIN, A.EVALUATE, A.REPORT_REF, A.ROLES) == (expand.TRAIN, expand.EVALUATE, expand.REPORT, expand.ROLES)


@pytest.mark.parametrize(("ref", "component"), [
    (A.TEMPLATE_REF, "eidos.templates.improves-under-budget"),
    (A.ASSESSOR_REF, "eidos.assessors.paired-ci"),
    (A.REPORT_REF, "eidos.task.paired-classification-report"),
])
def test_the_pinned_component_refs_are_the_releases_in_the_tree(ref, component):
    manifest = _manifest(component)
    assert ref == f"{manifest['id']}@{manifest['release']}"


def test_the_fixtures_request_the_pinned_assessor():
    for fixture in ("classification-three-seeds", "classification-one-seed", "from-scratch"):
        assert _request(fixture)["assessor"] == {"ref": A.ASSESSOR_REF}
