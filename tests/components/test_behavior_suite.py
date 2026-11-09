"""behavior-suite/1 (CheckList-style): author templates -> cases + transformed examples -> raw predictions ->
scored report with per-group rates; the scorer reuses raw predictions and never runs a model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

COMP = Path(__file__).resolve().parents[2] / "components" / "eidos.behavior.suite"
sys.path.insert(0, str(COMP))

from eidos_behavior.score import score_suite  # noqa: E402
from eidos_behavior.templates import direction, invariance, suite  # noqa: E402

EXAMPLES = [{"id": f"e{i}", "text": t, "domain": d} for i, (t, d) in enumerate(
    [("great food", "food"), ("great service", "service"), ("good pasta", "food"), ("nice staff", "service")])]


def model(text):  # a toy sentiment model: the score to predict with
    s = 0.9 if any(w in text for w in ("great", "good", "nice")) else 0.5
    return s - (0.6 if "not" in text else 0.0) - (0.3 if "pasta" in text and "!" in text else 0.0)


def test_templates_build_cases_and_the_scorer_reports_by_test_and_group():
    typo = invariance("exclamation", EXAMPLES, lambda e: {**e, "text": e["text"] + "!"}, group=lambda e: e["domain"],
                      capability="robustness", on="score", tolerance=0.05, min_pass_rate=1.0)
    neg = direction("negation-lowers", EXAMPLES, lambda e: {**e, "text": "not " + e["text"]}, expect="decrease",
                    group=lambda e: e["domain"], capability="negation")
    skip = direction("only-food", EXAMPLES, lambda e: None if e["domain"] != "food" else {**e, "text": e["text"] + " :)"},
                     expect="not-decrease", capability="robustness")
    cases, transformed = suite("sentiment", [typo, neg, skip])
    assert cases["schema"] == "behavior-cases/1" and len(transformed) == 4 + 4 + 2
    assert all("_examples" not in t for t in cases["tests"])
    preds = [{"id": e["id"], "score": model(e["text"])} for e in EXAMPLES + transformed]  # predicted once
    rep = score_suite(cases, preds)
    by = {t["name"]: t for t in rep["tests"]}
    assert by["exclamation"]["status"] == "fail" and by["exclamation"]["pass_rate"] == 0.75
    assert by["exclamation"]["by_group"]["food"]["pass_rate"] == 0.5 and by["exclamation"]["by_group"]["service"]["pass_rate"] == 1
    assert by["exclamation"]["failures"][0]["original"] == "e2"
    assert by["negation-lowers"]["status"] == "pass" and by["only-food"]["n"] == 2
    assert rep["capabilities"] == {"robustness": {"tests": 2, "failed": 1}, "negation": {"tests": 1, "failed": 0}}
    # a second scorer pass over the same raw predictions gives the same report (nothing re-predicted)
    assert score_suite(cases, {"rows": preds}) == rep


def test_missing_predictions_make_a_test_incomplete_and_direction_requires_a_valid_expectation():
    cases, transformed = suite("s", [invariance("i", EXAMPLES[:1], lambda e: {**e, "text": "x"})])
    rep = score_suite(cases, [{"id": "e0", "label": 1}])
    assert rep["tests"][0]["status"] == "incomplete" and rep["tests"][0]["missing"] == 1
    with pytest.raises(ValueError):
        direction("d", EXAMPLES, lambda e: e, expect="sideways")
