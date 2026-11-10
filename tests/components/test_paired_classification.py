"""Ordinary multiclass accuracy evidence (ADR 0018): the paired-classification-report component, the
paired-ci assessor's ``accuracy`` metric family and the improves-under-budget ``classification`` request.

Author tests: the fixtures and the expectations were written by the same developer as the components.
Expected statistics are re-derived here from the formulas (cluster-robust or plain paired difference,
normal approximation), not read back from the code under test.
"""

import copy
import importlib.util
import json
import math
import sys
from statistics import NormalDist

import pytest

from conftest import COMPONENTS
from eidos.domain.charter import charter_digest, reject_threshold_overrides, validate_charter
from eidos.domain.evidence import compile_plan, evaluate_success_rule
from eidos_worker.errors import ContractError

REPORT = ("eidos.task.paired-classification-report", "paired_report.report:run")
ASSESSOR = ("eidos.assessors.paired-ci", "paired_ci.assess:run")
TEMPLATE = ("eidos.templates.improves-under-budget", "improves_template.expand:run")
REPORT_REF = "eidos.task.paired-classification-report@1.0.0"
ROLES = ("baseline", "candidate")
CLASSES = ("cat", "dog", "bird")
Z95 = NormalDist().inv_cdf(0.975)


def _load_helpers():
    """The assessor module and the fixture builder (both are plain directories, not installed packages)."""
    for path in (COMPONENTS / "eidos.assessors.paired-ci", COMPONENTS):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import build_assessor_fixtures as fixtures
    from paired_ci import assess

    return assess, fixtures


assess, fixtures = _load_helpers()


# --------------------------------------------------------------------------------------------------
# Component: eidos.task.paired-classification-report
# --------------------------------------------------------------------------------------------------


def _preds(pairs):
    return [{"id": i, "prediction": p} for i, p in pairs]


def _labels(pairs):
    return [{"id": i, "label": v} for i, v in pairs]


def _six():
    """Six examples; baseline in id order, candidate and labels reordered; labels are cat/dog/bird."""
    labels = [(0, "cat"), (1, "dog"), (2, "bird"), (3, "cat"), (4, "dog"), (5, "bird")]
    baseline = [(0, "cat"), (1, "cat"), (2, "bird"), (3, "dog"), (4, "dog"), (5, "cat")]
    candidate = [(5, "cat"), (4, "dog"), (3, "cat"), (2, "bird"), (1, "dog"), (0, "cat")]
    return baseline, candidate, labels


def _run_report(lab_run, baseline, candidate, labels, **config):
    config.setdefault("cluster", "11")
    return lab_run(*REPORT, inputs={"baseline": baseline, "candidate": candidate, "labels": labels}, config=config)


def _emitted(res):
    return [json.loads(line) for line in res.text("predictions").splitlines()]


def _assert_refused(res, *needles):
    assert res.manifest.status == "failed", f"expected a refusal, got {res.manifest.status}"
    assert res.manifest.error["category"] == "contract", res.manifest.error
    message = res.manifest.error["message"]
    for needle in needles:
        assert needle in message, message
    assert res.slots() == ["log"]  # nothing is published from a failed run


def test_each_role_emits_only_its_own_rows_over_the_same_examples(lab_run):
    baseline, candidate, labels = _six()
    kw = {"baseline": _preds(baseline), "candidate": _preds(candidate), "labels": _labels(labels)}
    got = {role: lab_run(*REPORT, inputs=kw, config={"role": role, "cluster": "11"}) for role in ROLES}
    for role, res in got.items():
        assert res.ok, res.manifest.error
        assert res.slots() == ["predictions", "log"]
        assert res.manifest.outputs[0].schema == "classification-pair-rows/1"
        assert res.receipt["role"] == role and res.receipt["cluster"] == "11"
        assert res.receipt["examples"] == 6 and res.receipt["classes"] == 3
        assert res.receipt["aligned"] is True and res.receipt["metrics"] == {"examples": 6}
    base_rows, cand_rows = _emitted(got["baseline"]), _emitted(got["candidate"])
    assert [r["example_id"] for r in base_rows] == [r["example_id"] for r in cand_rows] == list("012345")
    assert {r["role"] for r in base_rows} == {"baseline"} and {r["role"] for r in cand_rows} == {"candidate"}
    assert all(r["cluster"] == "11" for r in base_rows + cand_rows)
    assert [r["label"] for r in base_rows] == [r["label"] for r in cand_rows]
    assert [r["label"] for r in base_rows] == ["cat", "dog", "bird", "cat", "dog", "bird"]
    assert [r["prediction"] for r in base_rows] == ["cat", "cat", "bird", "dog", "dog", "cat"]
    assert [r["prediction"] for r in cand_rows] == ["cat", "dog", "bird", "cat", "dog", "cat"]
    assert base_rows[0] == {"example_id": "0", "cluster": "11", "role": "baseline", "label": "cat", "prediction": "cat"}
    # the rows carry no correctness flag: the assessor recomputes it from label and prediction
    assert set(base_rows[0]) == {"example_id", "cluster", "role", "label", "prediction"}


def test_numeric_ids_sort_by_length_then_text_and_cluster_is_canonical(lab_run):
    ids = [10, 9, 2, 100]
    triples = [(i, "a") for i in ids]
    res = _run_report(
        lab_run, _preds(triples), _preds(reversed(triples)), _labels(triples), role="baseline", cluster=11
    )
    assert res.ok, res.manifest.error
    assert [r["example_id"] for r in _emitted(res)] == ["2", "9", "10", "100"]
    assert res.receipt["cluster"] == "11"  # an integer seed is accepted and stored as its decimal string
    assert {r["cluster"] for r in _emitted(res)} == {"11"}


def test_int_float_and_string_ids_and_labels_align_canonically(lab_run):
    baseline = _preds([(3, 1), (1.0, 2.0), ("2", "0")])
    candidate = {"ids": ["3", 1, 2.0], "predictions": ["1", 2, 0.0]}
    labels = {"rows": [{"id": 2, "label": 0.0}, {"id": "3", "label": "1"}, {"id": 1, "label": 2}]}
    res = _run_report(lab_run, baseline, candidate, labels, role="candidate")
    assert res.ok, res.manifest.error
    assert _emitted(res) == [
        {"example_id": "1", "cluster": "11", "role": "candidate", "label": "2", "prediction": "2"},
        {"example_id": "2", "cluster": "11", "role": "candidate", "label": "0", "prediction": "0"},
        {"example_id": "3", "cluster": "11", "role": "candidate", "label": "1", "prediction": "1"},
    ]
    assert res.receipt["classes"] == 3


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("7", "7"),
        ("+7", "7"),
        ("3.0", "3"),
        ("3.", "3"),
        ("-2", "-2"),
        ("-2.0", "-2"),
        ("007", "7"),
        ("0.00", "0"),
        ("-0", "0"),
        (" 12 ", "12"),
        ("cat", "cat"),
        (" dog ", "dog"),
        ("3.5", "3.5"),
        ("1e3", "1e3"),
        ("v1", "v1"),
        ("1.2.3", "1.2.3"),
    ],
)
def test_strings_spelling_an_integer_are_that_integer(lab_run, raw, want):
    """Text "1.0", JSON 1 and a float 1.0 are one id / class; any other string is only trimmed."""
    res = _run_report(lab_run, _preds([(raw, raw)]), _preds([(raw, raw)]), _labels([(raw, raw)]), role="candidate")
    assert res.ok, res.manifest.error
    assert _emitted(res) == [
        {"example_id": want, "cluster": "11", "role": "candidate", "label": want, "prediction": want}
    ]


def test_different_spellings_of_one_integer_are_a_duplicate_id(lab_run):
    rows = _preds([("7", "a"), ("+7.0", "b")])
    res = _run_report(lab_run, rows, rows, _labels([(7, "a")]), role="baseline")
    _assert_refused(res, "duplicate id '7'", "'baseline'")


def test_csv_float_text_agrees_with_json_integers(lab_run):
    baseline = (b"id,prediction\n0.0,1.0\n1.0,2.0\n+2,3.\n003,0\n", "text/csv")
    candidate = _preds([(3, 0), (2, 3), (1.0, 2.0), (0, 1)])
    labels = (
        "\n".join(json.dumps({"id": str(i), "label": v}) for i, v in [(0, "1.0"), (1, 2), (2, 3), (3, "+0")]),
        "application/jsonl",
    )
    for role in ROLES:
        res = _run_report(lab_run, baseline, candidate, labels, role=role)
        assert res.ok, res.manifest.error
        assert [(r["example_id"], r["label"], r["prediction"]) for r in _emitted(res)] == [
            ("0", "1", "1"),
            ("1", "2", "2"),
            ("2", "3", "3"),
            ("3", "0", "0"),
        ]


def test_other_strings_stay_distinct_after_trimming(lab_run):
    """Labels are not case-folded and a non-integer decimal is not an integer: only integer spellings merge."""
    res = _run_report(
        lab_run,
        _preds([(0, " cat"), (1, "Cat"), (2, "3.50"), (3, "3.5")]),
        _preds([(0, "cat"), (1, "cat"), (2, "3.5"), (3, "3.5")]),
        _labels([(0, "cat"), (1, "cat"), (2, "3.5"), (3, "3.5")]),
        role="baseline",
    )
    assert res.ok, res.manifest.error
    assert [(r["label"], r["prediction"]) for r in _emitted(res)] == [
        ("cat", "cat"),
        ("cat", "Cat"),
        ("3.5", "3.50"),
        ("3.5", "3.5"),
    ]


def test_csv_jsonl_and_columnar_json_inputs(lab_run):
    csv_baseline = (b"id,prediction\n1,cat\n2,dog\n3,bird\n", "text/csv")
    jsonl_candidate = (
        '{"id": 3, "prediction": "bird"}\n\n{"id": 1, "prediction": "dog"}\n{"id": 2, "prediction": "dog"}\n',
        "application/jsonl",
    )
    columnar_labels = {"ids": [2, 3, 1], "y": ["dog", "bird", "cat"]}
    res = _run_report(lab_run, csv_baseline, jsonl_candidate, columnar_labels, role="baseline")
    assert res.ok, res.manifest.error
    assert [(r["example_id"], r["label"], r["prediction"]) for r in _emitted(res)] == [
        ("1", "cat", "cat"),
        ("2", "dog", "dog"),
        ("3", "bird", "bird"),
    ]
    other = _run_report(lab_run, csv_baseline, jsonl_candidate, columnar_labels, role="candidate")
    assert [r["prediction"] for r in _emitted(other)] == ["dog", "dog", "bird"]


def test_custom_id_field_names_the_id_of_records(lab_run):
    def records(pairs, field):
        return [{"uid": i, field: v} for i, v in pairs]

    base, cand, lab = _six()
    res = lab_run(
        *REPORT,
        inputs={
            "baseline": records(base, "prediction"),
            "candidate": (b"uid,prediction\n" + b"".join(f"{i},{p}\n".encode() for i, p in cand), "text/csv"),
            "labels": records(lab, "label"),
        },
        config={"role": "candidate", "cluster": "7", "id_field": "uid"},
    )
    assert res.ok, res.manifest.error
    assert [(r["example_id"], r["prediction"]) for r in _emitted(res)] == [
        ("0", "cat"),
        ("1", "dog"),
        ("2", "bird"),
        ("3", "cat"),
        ("4", "dog"),
        ("5", "cat"),
    ]
    # without id_field the same records lack the default 'id' field
    refused = lab_run(
        *REPORT,
        inputs={
            "baseline": records(base, "prediction"),
            "candidate": records(cand, "prediction"),
            "labels": records(lab, "label"),
        },
        config={"role": "candidate", "cluster": "7"},
    )
    _assert_refused(refused, "record 0 needs 'id'")


def test_classes_filter_accepts_listed_labels_including_integers(lab_run):
    labels = _labels([(0, 0), (1, 1), (2, 2)])
    res = _run_report(
        lab_run,
        _preds([(0, 0), (1, 2), (2, 2)]),
        _preds([(0, 1), (1, 1), (2, 2)]),
        labels,
        role="candidate",
        classes=["0", 1, 2.0],
    )
    assert res.ok, res.manifest.error
    assert [r["prediction"] for r in _emitted(res)] == ["1", "1", "2"]


def _fail_cases():
    base, cand, lab = _six()
    cases = {}

    def case(name, needles, *, baseline=None, candidate=None, labels=None, config=None):
        cases[name] = (
            _preds(base) if baseline is None else baseline,
            _preds(cand) if candidate is None else candidate,
            _labels(lab) if labels is None else labels,
            {"role": "baseline", **(config or {})},
            needles,
        )

    case(
        "candidate-missing-id",
        ("example ids do not align", "candidate lacks 1 labelled ids", "'5'"),
        candidate=_preds(cand[1:]),
    )
    case(
        "baseline-extra-id",
        ("example ids do not align", "baseline has 1 ids without a label", "'99'"),
        baseline=_preds([*base, (99, "cat")]),
    )
    case(
        "baseline-missing-and-candidate-extra",
        ("baseline lacks 1 labelled ids", "candidate has 1 ids"),
        baseline=_preds(base[:-1]),
        candidate=_preds([*cand, (77, "dog")]),
    )
    case("labels-duplicate-id", ("duplicate id '0'", "'labels'"), labels=_labels([*lab, (0, "dog")]))
    case(
        "candidate-duplicate-after-canonicalisation",
        ("duplicate id '1'", "'candidate'"),
        candidate=[{"id": "1", "prediction": "cat"}, *_preds(cand[:1]), {"id": 1.0, "prediction": "dog"}],
    )
    case("string-nan-prediction", ("non-finite", "'NaN'"), candidate=_preds([*cand[:-1], (0, "NaN")]))
    case("string-inf-prediction", ("non-finite",), baseline=_preds([*base[:-1], (5, "-inf")]))
    case("non-integral-float-label", ("1.5", "not an integer class label"), labels=_labels([*lab[:-1], (5, 1.5)]))
    case("bool-label", ("True", "not a valid id or class label"), labels=_labels([*lab[:-1], (5, True)]))
    case("bool-prediction", ("False", "not a valid id or class label"), baseline=_preds([*base[:-1], (5, False)]))
    case("null-id", ("None", "not a valid id or class label"), baseline=_preds([*base[:-1], (None, "cat")]))
    case("null-label", ("None", "not a valid id or class label"), labels=_labels([*lab[:-1], (5, None)]))
    case("empty-label", ("empty value",), labels=_labels([*lab[:-1], (5, "  ")]))
    case("bool-id", ("True", "not a valid id or class label"), baseline=_preds([*base[:-1], (True, "cat")]))
    case("value-outside-classes", ("values outside config.classes", "bird"), config={"classes": ["cat", "dog"]})
    case("classes-not-a-list", ("config.classes must be a non-empty list",), config={"classes": "cat"})
    case("empty-baseline", ("has no examples", "'baseline'"), baseline=[])
    case("empty-labels", ("has no examples", "'labels'"), labels=[])
    case(
        "record-without-value",
        ("record 1", "one of ['prediction', 'predictions']"),
        baseline=[{"id": 0, "prediction": "cat"}, {"id": 1}],
    )
    case(
        "columns-of-unequal-length",
        ("3 ids but 2 predictions",),
        candidate={"ids": [0, 1, 2], "predictions": ["cat", "dog"]},
    )
    case("not-json", ("not JSON, JSONL, CSV or NPZ",), baseline=(b"not json at all", "application/json"))
    return cases


@pytest.mark.parametrize("name", sorted(_fail_cases()))
def test_report_fails_closed(lab_run, name):
    baseline, candidate, labels, config, needles = _fail_cases()[name]
    res = lab_run(
        *REPORT,
        inputs={"baseline": baseline, "candidate": candidate, "labels": labels},
        config={"cluster": "11", **config},
    )
    _assert_refused(res, *needles)


def test_report_refuses_a_literal_nan_in_json(lab_run):
    """Python's json parser accepts a bare NaN; the component must not let it through as a class."""
    base, cand, lab = _six()
    text = json.dumps(_preds(base)).replace('"id": 5, "prediction": "cat"', '"id": 5, "prediction": NaN')
    assert "NaN" in text
    res = _run_report(lab_run, (text.encode(), "application/json"), _preds(cand), _labels(lab), role="baseline")
    _assert_refused(res, "non-finite")


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        ({"cluster": "11"}, "config.role must be one of"),
        ({"role": "ensemble", "cluster": "11"}, "config.role must be one of"),
        ({"role": "baseline"}, "config.cluster"),
        ({"role": "baseline", "cluster": " "}, "empty value"),
        ({"role": "baseline", "cluster": True}, "not a valid id or class label"),
    ],
    ids=["missing-role", "unknown-role", "missing-cluster", "blank-cluster", "bool-cluster"],
)
def test_report_requires_role_and_cluster(lab_run, config, needle):
    base, cand, lab = _six()
    res = lab_run(
        *REPORT, inputs={"baseline": _preds(base), "candidate": _preds(cand), "labels": _labels(lab)}, config=config
    )
    _assert_refused(res, needle)


@pytest.mark.skipif(importlib.util.find_spec("numpy") is not None, reason="numpy is installed here")
def test_npz_input_says_numpy_is_unavailable(lab_run):
    base, cand, lab = _six()
    res = lab_run(
        *REPORT,
        inputs={
            "baseline": (b"PK\x03\x04 not really a zip", "application/x-npz"),
            "candidate": _preds(cand),
            "labels": _labels(lab),
        },
        config={"role": "baseline", "cluster": "11"},
    )
    _assert_refused(res, "NPZ", "numpy")


# --------------------------------------------------------------------------------------------------
# Assessor: eidos.assessors.paired-ci 1.1.0, metric accuracy
# --------------------------------------------------------------------------------------------------


def _arm_rows(role, seed, n, ok_count):
    """n examples with ids 0..n-1; the first ok_count are predicted correctly, the rest as the next class."""
    rows = []
    for i in range(n):
        label = CLASSES[i % 3]
        rows.append(
            {
                "example_id": str(i),
                "cluster": str(seed),
                "role": role,
                "label": label,
                "prediction": label if i < ok_count else CLASSES[(i + 1) % 3],
            }
        )
    return rows


def _evidence(plan, per_seed):
    """per_seed = {seed: (n, baseline_correct, candidate_correct)}; correct examples are nested prefixes."""
    arms = {"baseline": {"role": "baseline", "rows": []}, "candidate": {"role": "candidate", "rows": []}}
    for seed, (n, base_ok, cand_ok) in per_seed.items():
        arms["baseline"]["rows"] += _arm_rows("baseline", seed, n, base_ok)
        arms["candidate"]["rows"] += _arm_rows("candidate", seed, n, cand_ok)
    return {"plan": plan, "arms": arms}


def _evaluate(evidence):
    return evaluate_success_rule(evidence["plan"], assess.per_unit_by_role(evidence["plan"], evidence["arms"]))


def _se_of_clusters(resid_sums, n):
    g = len(resid_sums)
    return math.sqrt(g / (g - 1) * sum(s * s for s in resid_sums) / (n * n))


PLANTED = {11: (100, 60, 78), 12: (100, 60, 80), 13: (100, 60, 82)}  # gains 0.18, 0.20, 0.22: mean 0.20


def test_planted_gain_passes_with_a_clustered_lower_bound_above_the_bar():
    result = _evaluate(_evidence(fixtures.accuracy_plan(), PLANTED))
    se = _se_of_clusters([-2, 0, 2], 300)  # per-seed residual sums of (diff - 0.2): 18-20, 0, 22-20
    eff = result["effect"]
    assert result["outcome"] == "protocol-passed" and result["min_effect"] == 0.02
    assert eff["estimate"] == pytest.approx(0.2)
    assert eff["se"] == pytest.approx(se, rel=1e-9)
    assert eff["ci_low"] == pytest.approx(0.2 - Z95 * se, rel=1e-6) and eff["ci_low"] >= 0.02
    assert eff["ci_high"] == pytest.approx(0.2 + Z95 * se, rel=1e-6)
    assert eff["units"] == 300 and eff["independent_units"] == 3 and eff["confidence"] == 0.95
    assert result["guardrails"] == [] and result["metric"] == "accuracy" and result["direction"] == "increase"


def test_per_unit_values_are_recomputed_from_label_and_prediction():
    plan = fixtures.accuracy_plan()
    evidence = _evidence(plan, {11: (10, 6, 8)})
    by_role = assess.per_unit_by_role(plan, evidence["arms"])
    assert set(by_role) == {"baseline", "candidate"}
    assert sum(r["accuracy"] for r in by_role["baseline"]) == 6
    assert sum(r["accuracy"] for r in by_role["candidate"]) == 8
    assert {(r["cluster"], r["unit"]) for r in by_role["baseline"]} == {("11", str(i)) for i in range(10)}
    # numbers and their string forms are the same class; surrounding whitespace is not part of it
    row = {"example_id": 1, "cluster": 11, "role": "baseline", "label": 3, "prediction": 3.0}
    row2 = {"example_id": 2, "cluster": 11, "role": "baseline", "label": " cat ", "prediction": "cat"}
    row3 = {"example_id": 3, "cluster": 11, "role": "baseline", "label": 3, "prediction": "3"}
    row4 = {"example_id": 4, "cluster": 11, "role": "baseline", "label": 3, "prediction": 4}
    arms = {
        "baseline": {"role": "baseline", "rows": [row, row2, row3, row4]},
        "candidate": {"role": "candidate", "rows": [{**r, "role": "candidate"} for r in (row, row2, row3, row4)]},
    }
    got = assess.per_unit_by_role(plan, arms)
    assert [r["accuracy"] for r in got["baseline"]] == [1.0, 1.0, 1.0, 0.0]


def test_strings_spelling_an_integer_are_that_integer_in_rows():
    plan = fixtures.accuracy_plan()
    pairs = [
        ("7", "+7", 1.0),
        ("3.0", 3, 1.0),
        ("3.", "3.0", 1.0),
        ("007", 7, 1.0),
        ("-0", 0.0, 1.0),
        ("3.50", "3.5", 0.0),
        ("3.5", "3.5", 1.0),
        ("Cat", "cat", 0.0),
        ("1e3", 1000, 0.0),
        (" cat ", "cat", 1.0),
    ]
    rows = [{"example_id": i, "cluster": "11", "label": a, "prediction": b} for i, (a, b, _) in enumerate(pairs)]
    arms = {role: {"role": role, "rows": [{**r, "role": role} for r in rows]} for role in ROLES}
    got = assess.per_unit_by_role(plan, arms)
    assert [r["accuracy"] for r in got["baseline"]] == [want for _, _, want in pairs]
    # the example id and cluster are used as given (the report component emits them canonically)
    assert {r["unit"] for r in got["candidate"]} == {str(i) for i in range(len(pairs))}


def test_zero_gain_is_a_protocol_failure_with_a_zero_estimate():
    plan = fixtures.accuracy_plan()
    evidence = _evidence(plan, {11: (100, 60, 60), 12: (100, 70, 70), 13: (100, 65, 65)})
    base = evidence["arms"]["baseline"]["rows"]
    evidence["arms"]["candidate"]["rows"] = [{**r, "role": "candidate"} for r in copy.deepcopy(base)]
    result = _evaluate(evidence)
    assert result["outcome"] == "protocol-failed"
    assert result["effect"]["estimate"] == 0 and result["effect"]["ci_low"] == 0 == result["effect"]["ci_high"]
    assert result["effect"]["units"] == 300


def test_noisy_small_samples_are_inconclusive():
    # 10 examples in each of 3 seeds, accuracy 0.6 vs 0.7 overall, but the per-seed gains disagree
    result = _evaluate(_evidence(fixtures.accuracy_plan(), {11: (10, 6, 8), 12: (10, 6, 6), 13: (10, 6, 7)}))
    se = _se_of_clusters([1, -1, 0], 30)
    eff = result["effect"]
    assert eff["estimate"] == pytest.approx(0.1) and eff["se"] == pytest.approx(se, rel=1e-9)
    assert eff["ci_low"] == pytest.approx(0.1 - Z95 * se, rel=1e-6) and eff["ci_low"] < 0.02
    assert eff["ci_high"] >= 0.02
    assert result["outcome"] == "inconclusive"


def test_a_worse_candidate_fails():
    result = _evaluate(_evidence(fixtures.accuracy_plan(), {11: (100, 70, 55), 12: (100, 70, 60), 13: (100, 70, 57)}))
    assert result["outcome"] == "protocol-failed" and result["effect"]["estimate"] < 0
    assert result["effect"]["ci_high"] < 0.02


def test_one_seed_is_decided_by_examples_only_when_the_plan_has_no_cluster_by():
    n = 2000
    one = {11: (n, 1200, 1400)}  # 0.60 vs 0.70: 200 of 2000 examples fixed
    unclustered = _evaluate(_evidence(fixtures.accuracy_plan(seeds=(11,), cluster_by=None), one))
    se = math.sqrt((200 * 0.9**2 + 1800 * 0.1**2) / (n - 1) / n)
    assert unclustered["outcome"] == "protocol-passed"
    eff = unclustered["effect"]
    assert eff["estimate"] == pytest.approx(0.1) and eff["independent_units"] == n
    assert eff["ci_low"] == pytest.approx(0.1 - Z95 * se, rel=1e-6) and eff["ci_low"] >= 0.02
    # the same rows under cluster_by "seed" have one cluster: no variance estimate, an infinite interval
    clustered = _evaluate(_evidence(fixtures.accuracy_plan(seeds=(11,), cluster_by="seed"), one))
    assert clustered["outcome"] == "inconclusive"
    assert clustered["effect"]["estimate"] == pytest.approx(0.1)
    assert clustered["effect"]["ci_low"] == -math.inf and clustered["effect"]["ci_high"] == math.inf
    assert clustered["effect"]["independent_units"] == 1


def _mutated(evidence, edit):
    out = copy.deepcopy(evidence)
    edit(out)
    return out


def _small_evidence(plan=None, **kw):
    return _evidence(plan or fixtures.accuracy_plan(), {11: (6, 3, 4), 12: (6, 3, 4), 13: (6, 3, 5)}, **kw)


def _reject(evidence, *needles):
    with pytest.raises(ContractError) as err:
        assess.per_unit_by_role(evidence["plan"], evidence["arms"])
    for needle in needles:
        assert needle in str(err.value), str(err.value)


def test_example_id_mismatch_between_arms_is_rejected():
    ev = _mutated(_small_evidence(), lambda e: e["arms"]["candidate"]["rows"][0].update(example_id="not-labelled"))
    _reject(ev, "do not match", "1 only in 'baseline'", "1 only in 'candidate'", "11/0", "11/not-labelled")


def test_a_cluster_present_only_in_the_candidate_is_rejected():
    def edit(e):
        e["arms"]["candidate"]["rows"] += _arm_rows("candidate", 14, 6, 5)

    _reject(_mutated(_small_evidence(), edit), "do not match", "0 only in 'baseline'", "6 only in 'candidate'", "14/0")


def test_a_cluster_missing_from_the_candidate_is_rejected():
    def edit(e):
        e["arms"]["candidate"]["rows"] = [r for r in e["arms"]["candidate"]["rows"] if r["cluster"] != "13"]

    _reject(_mutated(_small_evidence(), edit), "do not match", "6 only in 'baseline'")


def test_an_incomplete_arm_without_rows_is_inconclusive_not_refused():
    """A failed report job leaves its arm without rows: the assessment is recorded as inconclusive (no paired
    units; the server's conformance check marks it incomplete) instead of failing the assessor."""
    def edit(e):
        e["arms"]["candidate"].update(rows=[], status="incomplete")

    result = _evaluate(_mutated(_small_evidence(), edit))
    assert result["outcome"] == "inconclusive" and result["reason"] == "no paired units" and result["effect"] is None


def test_an_incomplete_arm_missing_a_cluster_pairs_only_the_shared_clusters():
    def edit(e):
        e["arms"]["candidate"]["rows"] = [r for r in e["arms"]["candidate"]["rows"] if r["cluster"] != "13"]
        e["arms"]["candidate"]["status"] = "incomplete"

    evidence = _mutated(_small_evidence(), edit)
    per_unit = assess.per_unit_by_role(evidence["plan"], evidence["arms"])
    result = evaluate_success_rule(evidence["plan"], per_unit)
    assert {r["cluster"] for r in per_unit["candidate"]} == {"11", "12"}
    assert result["effect"]["units"] == len(per_unit["candidate"])


def test_an_incomplete_arm_still_refuses_mismatched_ids_inside_a_shared_cluster():
    def edit(e):
        e["arms"]["candidate"]["rows"][0]["example_id"] = "not-a-labelled-id"
        e["arms"]["candidate"]["status"] = "incomplete"

    _reject(_mutated(_small_evidence(), edit), "do not match")


def test_a_label_disagreement_between_arms_is_rejected():
    ev = _mutated(_small_evidence(), lambda e: e["arms"]["candidate"]["rows"][4].update(label="bird"))
    _reject(ev, "disagree on its label", "11/4")


def test_a_row_role_that_differs_from_its_arm_is_rejected():
    ev = _mutated(_small_evidence(), lambda e: e["arms"]["candidate"]["rows"][2].update(role="baseline"))
    _reject(ev, "row role 'baseline' in an arm with role 'candidate'")


def test_several_clusters_without_cluster_by_are_rejected():
    plan = fixtures.accuracy_plan(cluster_by=None)
    assert plan["cluster_by"] is None
    _reject(_small_evidence(plan), "3 training seeds", "no cluster_by")


def test_an_arm_without_rows_is_rejected():
    ev = _mutated(_small_evidence(), lambda e: e["arms"].pop("candidate"))
    _reject(ev, "no classification rows for role 'candidate'")


def test_an_arm_outside_the_plan_or_with_the_wrong_role_is_rejected():
    _reject(
        _mutated(_small_evidence(), lambda e: e["arms"].update(ghost=e["arms"]["candidate"])),
        "arm 'ghost' is not in the frozen plan",
    )
    _reject(
        _mutated(_small_evidence(), lambda e: e["arms"]["candidate"].update(role="baseline")),
        "evidence role 'baseline' != planned role 'candidate'",
    )


def test_a_duplicate_example_in_one_arm_and_cluster_is_rejected():
    ev = _mutated(
        _small_evidence(), lambda e: e["arms"]["candidate"]["rows"].append(dict(e["arms"]["candidate"]["rows"][0]))
    )
    _reject(ev, "duplicate unit '0' in cluster '11' for role 'candidate'")


def test_the_same_example_id_in_different_clusters_is_not_a_duplicate():
    result = _evaluate(_small_evidence())
    assert result["effect"]["units"] == 18


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda r: r.update(label=None), "label is missing or not a class label"),
        (lambda r: r.update(prediction=True), "prediction is missing or not a class label"),
        (lambda r: r.update(prediction=1.5), "non-finite or not an integer class"),
        (lambda r: r.update(prediction=float("nan")), "non-finite or not an integer class"),
        (lambda r: r.update(prediction="NaN"), "is not a class label"),
        (lambda r: r.update(label="  "), "is not a class label"),
        (lambda r: r.pop("prediction"), "prediction is missing"),
        (lambda r: r.update(example_id=None), "without an example_id"),
        (lambda r: r.update(example_id=True), "without an example_id"),
        (lambda r: r.update(cluster=None), "need a cluster"),
        (lambda r: r.update(cluster=" "), "need a cluster"),
    ],
    ids=[
        "null-label",
        "bool-prediction",
        "non-integral",
        "nan",
        "nan-string",
        "blank-label",
        "missing-prediction",
        "null-id",
        "bool-id",
        "null-cluster",
        "blank-cluster",
    ],
)
def test_unreadable_rows_are_rejected(edit, needle):
    ev = _mutated(_small_evidence(), lambda e: edit(e["arms"]["baseline"]["rows"][1]))
    _reject(ev, needle)


def test_mixed_row_families_in_one_rule_are_rejected():
    plan = fixtures.accuracy_plan()
    plan["success_rule"]["guardrails"] = [{"metric": "reference_accuracy", "max_regression": 0.05}]
    plan["metric_definitions"]["reference_accuracy"] = {
        "identity": "paired-report/1:reference-accuracy",
        "direction": "increase",
        "denominator": "pairs",
    }
    _reject(
        _small_evidence(plan),
        "need different row families",
        "['classification', 'paired-report']",
        "computable from the same rows",
    )


def test_an_unknown_metric_is_rejected():
    plan = fixtures.accuracy_plan()
    plan["success_rule"]["metric"] = "macro_f1"
    _reject(_small_evidence(plan), "cannot compute metric 'macro_f1'", "accuracy")


def test_paired_report_metrics_still_work():
    evidence = json.loads((COMPONENTS / "eidos.assessors.paired-ci" / "fixtures" / "planted-effect.json").read_text())[
        "inputs"
    ]["evidence"]["json"]
    result = _evaluate(evidence)
    assert result["outcome"] == "protocol-passed"
    assert result["effect"]["units"] == 36 and result["effect"]["independent_units"] == 3
    assert result["metric"] == "both_correct_rate"
    assert [(g["metric"], g["status"]) for g in result["guardrails"]] == [("reference_accuracy", "ok")]
    null = json.loads((COMPONENTS / "eidos.assessors.paired-ci" / "fixtures" / "null-effect.json").read_text())
    assert _evaluate(null["inputs"]["evidence"]["json"])["outcome"] == "protocol-failed"


def test_every_assessor_fixture_runs_to_its_expectation(lab_run):
    """The accuracy fixtures through the real worker protocol, next to the original paired-report ones."""
    fixture_dir = COMPONENTS / "eidos.assessors.paired-ci" / "fixtures"
    names = sorted(p.stem for p in fixture_dir.glob("*.json"))
    assert {"accuracy-planted", "accuracy-null", "accuracy-id-mismatch", "accuracy-one-seed", "planted-effect"} <= set(
        names
    )
    for name in names:
        fx = json.loads((fixture_dir / f"{name}.json").read_text())
        res = lab_run(*ASSESSOR, inputs={"evidence": fx["inputs"]["evidence"]["json"]}, config=fx["config"])
        expect = fx["expect"]
        assert res.manifest.status == expect["status"], (name, res.manifest.error)
        for key, value in expect.get("receipt", {}).items():
            assert res.receipt[key] == value, (name, key, res.receipt)
        if expect["status"] == "failed":
            assert res.manifest.error["category"] == expect["error_category"], name


def test_assessor_run_reports_the_classification_evaluation(lab_run):
    res = lab_run(*ASSESSOR, inputs={"evidence": _evidence(fixtures.accuracy_plan(), PLANTED)}, config={})
    assert res.ok, res.manifest.error
    se = _se_of_clusters([-2, 0, 2], 300)
    assert res.receipt["outcome"] == "protocol-passed" and res.receipt["units"] == 300
    assert res.receipt["clusters"] == 3
    assert res.receipt["metrics"]["estimate"] == pytest.approx(0.2)
    assert res.receipt["metrics"]["ci_low"] == pytest.approx(0.2 - Z95 * se, rel=1e-6)
    assert res.manifest.outputs[0].schema == "protocol-evaluation/1"
    evaluation = res.json("evaluation")
    assert evaluation["assessor"] == "eidos.assessors.paired-ci@1.1.0"
    assert evaluation["analysis_unit"] == "example" and evaluation["cluster_by"] == "seed"
    assert evaluation["clusters"] == ["11", "12", "13"]
    assert evaluation["units_by_role"] == {"baseline": 300, "candidate": 300}
    assert evaluation["success_rule_ref"] == "charter:accuracy-study@1#practical-accuracy-gain"
    assert evaluation["unbounded_fields"] == []


def test_assessor_run_flags_the_unbounded_interval_of_a_single_cluster(lab_run):
    plan = fixtures.accuracy_plan(seeds=(11,), cluster_by="seed")
    res = lab_run(*ASSESSOR, inputs={"evidence": _evidence(plan, {11: (2000, 1200, 1400)})}, config={})
    assert res.ok, res.manifest.error
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["clusters"] == 1
    assert res.receipt["metrics"]["ci_low"] is None and res.receipt["metrics"]["ci_high"] is None
    assert set(res.json("evaluation")["unbounded_fields"]) == {"effect.ci_low", "effect.ci_high", "effect.se"}
    plain = fixtures.accuracy_plan(seeds=(11,), cluster_by=None)
    ok = lab_run(*ASSESSOR, inputs={"evidence": _evidence(plain, {11: (2000, 1200, 1400)})}, config={})
    assert ok.receipt["outcome"] == "protocol-passed" and ok.json("evaluation")["unbounded_fields"] == []


def test_assessor_run_refuses_mismatched_arms(lab_run):
    ev = _mutated(_small_evidence(), lambda e: e["arms"]["candidate"]["rows"][0].update(example_id="zzz"))
    res = lab_run(*ASSESSOR, inputs={"evidence": ev}, config={})
    assert res.manifest.status == "failed" and res.manifest.error["category"] == "contract"
    assert "do not match" in res.manifest.error["message"]


# --------------------------------------------------------------------------------------------------
# End to end inside the components: report (both roles, every seed) -> assessor
# --------------------------------------------------------------------------------------------------


def _seed_artifacts(seed, n, base_ok, cand_ok):
    """Held-out predictions of one training seed in three different formats, ids in different orders."""
    labels = [CLASSES[i % 3] for i in range(n)]

    def pred(i, ok):
        return labels[i] if i < ok else CLASSES[(i + 1) % 3]

    baseline = [{"id": str(i), "prediction": pred(i, base_ok)} for i in range(n)]
    order = list(reversed(range(n)))
    candidate = {"ids": order, "predictions": [pred(i, cand_ok) for i in order]}
    label_rows = {"rows": [{"id": float(i), "label": labels[i]} for i in range(n)]}
    return baseline, candidate, label_rows


def _report_rows(lab_run, per_seed):
    arms = {"baseline": {"role": "baseline", "rows": []}, "candidate": {"role": "candidate", "rows": []}}
    for seed, (n, base_ok, cand_ok) in per_seed.items():
        baseline, candidate, labels = _seed_artifacts(seed, n, base_ok, cand_ok)
        for role in arms:
            res = _run_report(lab_run, baseline, candidate, labels, role=role, cluster=str(seed))
            assert res.ok, res.manifest.error
            assert res.receipt["examples"] == n and res.receipt["cluster"] == str(seed)
            arms[role]["rows"] += _emitted(res)
    return arms


def test_report_rows_feed_the_assessor_to_a_verdict(lab_run):
    arms = _report_rows(lab_run, PLANTED)
    assert [len(a["rows"]) for a in arms.values()] == [300, 300]
    # what the components emit is what the in-process assessor computes from synthetic rows
    assert arms == _evidence(fixtures.accuracy_plan(), PLANTED)["arms"]
    res = lab_run(*ASSESSOR, inputs={"evidence": {"plan": fixtures.accuracy_plan(), "arms": arms}}, config={})
    assert res.ok, res.manifest.error
    se = _se_of_clusters([-2, 0, 2], 300)
    assert res.receipt["outcome"] == "protocol-passed" and res.receipt["units"] == 300
    assert res.receipt["metrics"]["estimate"] == pytest.approx(0.2)
    assert res.receipt["metrics"]["ci_low"] == pytest.approx(0.2 - Z95 * se, rel=1e-6)
    assert res.receipt["metrics"]["ci_low"] >= 0.02


def test_csv_float_text_through_the_report_gives_the_same_verdict_as_integers(lab_run):
    """Predictions exported as CSV with float text ("1.0") and JSON integers are the same classes."""
    n = 50
    labels = [i % 4 for i in range(n)]

    def preds(ok):
        return [labels[i] if i < ok else (labels[i] + 1) % 4 for i in range(n)]

    def csv_float(values):
        body = "".join(f"{i}.0,{v}.0\n" for i, v in enumerate(values))
        return ("id,prediction\n" + body).encode(), "text/csv"

    arms = {"baseline": {"role": "baseline", "rows": []}, "candidate": {"role": "candidate", "rows": []}}
    for seed, (base_ok, cand_ok) in {11: (25, 40), 12: (25, 42), 13: (25, 38)}.items():
        label_rows = [{"id": i, "label": v} for i, v in enumerate(labels)]
        for role in arms:
            res = _run_report(
                lab_run,
                csv_float(preds(base_ok)),
                _preds(enumerate(preds(cand_ok))),
                label_rows,
                role=role,
                cluster=seed,
            )
            assert res.ok, res.manifest.error
            arms[role]["rows"] += _emitted(res)
    assert all(isinstance(r["label"], str) and "." not in r["prediction"] for a in arms.values() for r in a["rows"])
    res = lab_run(*ASSESSOR, inputs={"evidence": {"plan": fixtures.accuracy_plan(), "arms": arms}}, config={})
    assert res.ok, res.manifest.error
    assert res.receipt["units"] == 150 and res.receipt["metrics"]["estimate"] == pytest.approx(0.3)
    assert res.receipt["outcome"] == "protocol-passed"


def test_report_rows_of_identical_predictions_fail_the_rule(lab_run):
    arms = _report_rows(lab_run, {11: (100, 60, 60), 12: (100, 70, 70), 13: (100, 65, 65)})
    res = lab_run(*ASSESSOR, inputs={"evidence": {"plan": fixtures.accuracy_plan(), "arms": arms}}, config={})
    assert res.ok, res.manifest.error
    assert res.receipt["outcome"] == "protocol-failed" and res.receipt["metrics"]["estimate"] == 0


def test_report_rows_of_noisy_small_samples_are_inconclusive(lab_run):
    arms = _report_rows(lab_run, {11: (10, 6, 8), 12: (10, 6, 6), 13: (10, 6, 7)})
    res = lab_run(*ASSESSOR, inputs={"evidence": {"plan": fixtures.accuracy_plan(), "arms": arms}}, config={})
    assert res.ok, res.manifest.error
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["units"] == 30


def test_a_seed_missing_from_one_arm_cannot_be_assessed(lab_run):
    """Both arms' rows must cover the same seeds: dropping one seed from the candidate is refused."""
    arms = _report_rows(lab_run, PLANTED)
    arms["candidate"]["rows"] = [r for r in arms["candidate"]["rows"] if r["cluster"] != "12"]
    res = lab_run(*ASSESSOR, inputs={"evidence": {"plan": fixtures.accuracy_plan(), "arms": arms}}, config={})
    assert res.manifest.status == "failed" and "do not match" in res.manifest.error["message"]


# --------------------------------------------------------------------------------------------------
# Template: eidos.templates.improves-under-budget 1.2.0, "classification" requests
# --------------------------------------------------------------------------------------------------


def _classification_request(seeds=("11",), **over):
    req = {
        "claim": {
            "kind": "improves",
            "statement": "candidate recipe raises accuracy",
            "scope": {"task": "multiclass classification"},
        },
        "success_rule_id": "practical-accuracy-gain",
        "assessor": {"ref": "eidos.assessors.paired-ci@1.1.0"},
        "classification": {
            "labels": "art-labels",
            "predictions": {
                "baseline": {s: f"art-base-{s}" for s in seeds},
                "candidate": {s: f"art-cand-{s}" for s in seeds},
            },
        },
        "required_isolation": {"profile": "dev:same-user", "controls": []},
    }
    req.update(over)
    return req


def _expand(lab_run, request):
    return lab_run(*TEMPLATE, inputs={"request": request}, config={})


def _jobs(arm):
    return [job for run in arm["runs"] for job in run["jobs"]]


def test_one_seed_request_plans_one_report_job_per_arm(lab_run):
    res = _expand(lab_run, _classification_request())
    assert res.ok, res.manifest.error
    draft = res.json("draft")
    reject_threshold_overrides("draft", draft)  # the server's own check
    assert res.manifest.outputs[0].schema == "evidence-plan/1"
    assert [a["id"] for a in draft["arms"]] == ["baseline", "candidate"]
    assert [a["role"] for a in draft["arms"]] == ["baseline", "candidate"]
    for arm in draft["arms"]:
        assert len(arm["runs"]) == 1 and len(_jobs(arm)) == 1
        run = arm["runs"][0]
        assert run["seed"] == 11 and run["cluster"] == "11"
        assert _jobs(arm) == [
            {
                "id": f"{arm['role']}.s11.report",
                "component": REPORT_REF,
                "inputs": {"baseline": "art-base-11", "candidate": "art-cand-11", "labels": "art-labels"},
                "config": {"role": arm["role"], "cluster": "11"},
            }
        ]
    assert draft["cluster_by"] is None and draft["analysis_unit"] == "example"
    assert draft["seed_policy"]["seeds"] == [11] and draft["seed_policy"]["kind"] == "independent-training-seeds"
    assert draft["disclosures"]["seed_variance"] == "not estimated (one seed per arm)"
    assert any(
        "one training seed per arm" in a and "variance is not estimated" in a for a in draft["alternatives_unresolved"]
    )
    assert len(draft["alternatives_unresolved"]) == 3  # the two defaults plus the single-seed disclosure
    assert draft["obligations"] == []
    assert draft["assessor"] == {"ref": "eidos.assessors.paired-ci@1.1.0"}
    assert draft["splits"]["evaluation"] == {"labels": "art-labels", "population": "held-out examples"}
    assert res.receipt["jobs_planned"] == 2 and res.receipt["seeds"] == [11]
    assert res.receipt["open_obligations"] == []


def test_three_seed_request_clusters_by_seed_and_ignores_key_order(lab_run):
    seeds_a, seeds_b = ["13", "11", "12"], ["12", "13", "11"]
    req = _classification_request()
    req["classification"]["predictions"] = {
        "baseline": {s: f"art-base-{s}" for s in seeds_a},
        "candidate": {s: f"art-cand-{s}" for s in seeds_b},
    }
    res = _expand(lab_run, req)
    assert res.ok, res.manifest.error
    draft = res.json("draft")
    assert draft["cluster_by"] == "seed" and draft["seed_policy"]["seeds"] == [11, 12, 13]
    assert draft["disclosures"]["seed_variance"] == "clustered by training seed"
    assert not any("variance is not estimated" in a for a in draft["alternatives_unresolved"])
    assert res.receipt["jobs_planned"] == 6 and res.receipt["seeds"] == [11, 12, 13]
    for arm in draft["arms"]:
        assert [r["cluster"] for r in arm["runs"]] == ["11", "12", "13"]
        for run in arm["runs"]:
            (job,) = run["jobs"]
            seed = run["cluster"]
            assert job["inputs"] == {
                "baseline": f"art-base-{seed}",
                "candidate": f"art-cand-{seed}",
                "labels": "art-labels",
            }
            assert job["config"] == {"role": arm["role"], "cluster": seed}


def test_seed_keys_sort_numerically_and_optional_report_config_is_passed_through(lab_run):
    req = _classification_request(seeds=("10", "9"))
    req["classification"].update(classes=["cat", "dog", "bird"], id_field="uid")
    res = _expand(lab_run, req)
    assert res.ok, res.manifest.error
    draft = res.json("draft")
    assert draft["seed_policy"]["seeds"] == [9, 10]
    for arm in draft["arms"]:
        assert [j["config"] for j in _jobs(arm)] == [
            {"role": arm["role"], "cluster": s, "classes": ["cat", "dog", "bird"], "id_field": "uid"}
            for s in ("9", "10")
        ]


def _accuracy_charter():
    return validate_charter(
        {
            "study_id": "accuracy-study",
            "question": "Does the candidate recipe raise held-out multiclass accuracy by at least 0.02?",
            "metrics": {
                "accuracy": {
                    "identity": "classification-report/1:accuracy",
                    "direction": "increase",
                    "denominator": "held-out examples",
                }
            },
            "population": {"split": "held-out examples"},
            "comparator": {"arm_role": "baseline"},
            "success_rules": [
                {
                    "id": "practical-accuracy-gain",
                    "metric": "accuracy",
                    "min_effect": 0.02,
                    "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
                    "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                    "guardrails": [],
                }
            ],
        }
    )


@pytest.mark.parametrize(("seeds", "cluster_by"), [(("11",), None), (("11", "12", "13"), "seed")])
def test_template_draft_compiles_against_the_accuracy_charter(lab_run, seeds, cluster_by):
    draft = _expand(lab_run, _classification_request(seeds=seeds)).json("draft")
    charter = _accuracy_charter()
    plan = compile_plan(
        draft=draft,
        claim={"kind": "improves", "scope": {"task": "multiclass classification"}},
        claim_ref={"id": "claim:accuracy", "revision": 1},
        charter=charter,
        charter_ref={"id": "charter:accuracy-study", "revision": 1, "digest": charter_digest(charter)},
        template_ref={"ref": "eidos.templates.improves-under-budget@1.2.0"},
    )
    assert plan["success_rule"]["min_effect"] == 0.02 and plan["success_rule_source"] == "immutable-charter"
    assert plan["cluster_by"] == cluster_by and plan["analysis_unit"] == "example"
    assert plan["obligations"] == []


def test_unpaired_seeds_are_rejected(lab_run):
    req = _classification_request()
    req["classification"]["predictions"] = {"baseline": {"11": "a", "12": "b"}, "candidate": {"11": "c", "13": "d"}}
    _assert_refused(
        _expand(lab_run, req),
        "baseline seeds ['11', '12'] != candidate seeds ['11', '13']",
        "same training seeds in both arms",
    )


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda c: c.pop("labels"), "classification.labels must be the labels artifact id"),
        (lambda c: c.update(labels=""), "classification.labels must be the labels artifact id"),
        (lambda c: c.update(labels=7), "classification.labels must be the labels artifact id"),
        (lambda c: c["predictions"].pop("candidate"), "classification.predictions.candidate must map each"),
        (lambda c: c["predictions"].update(baseline={}), "classification.predictions.baseline must map each"),
        (lambda c: c["predictions"]["baseline"].update({"11": ""}), "must be an artifact id"),
        (lambda c: c["predictions"]["baseline"].update({"11": ["x"]}), "must be an artifact id"),
        (lambda c: c["predictions"]["baseline"].update({" ": "art"}), "must be an integer or a non-empty string"),
        (lambda c: c.pop("predictions"), "classification.predictions.baseline must map each"),
    ],
    ids=[
        "no-labels",
        "empty-labels",
        "non-string-labels",
        "no-candidate-predictions",
        "empty-baseline-seeds",
        "empty-artifact",
        "list-artifact",
        "blank-seed",
        "no-predictions",
    ],
)
def test_malformed_classification_requests_are_rejected(lab_run, edit, needle):
    req = _classification_request()
    edit(req["classification"])
    _assert_refused(_expand(lab_run, req), needle)


def test_classification_must_be_an_object(lab_run):
    _assert_refused(
        _expand(lab_run, _classification_request(classification=["labels"])), "request.classification must be an object"
    )


@pytest.mark.parametrize("place", ["top", "baseline", "classification", "nested"])
def test_a_request_containing_a_success_bar_is_rejected(lab_run, place):
    req = _classification_request()
    if place == "top":
        req["min_effect"] = 0.01
    elif place == "baseline":
        req["baseline"] = {"description": "x", "notes": {"min_effect": 0.0}}
    elif place == "classification":
        req["classification"]["min_effect"] = 0.5
    else:
        req["tuning"] = {"search": [{"threshold": 0.9}]}
    res = _expand(lab_run, req)
    _assert_refused(res, "must not define success bars", "the charter rule owns them")
    assert res.manifest.outputs[0].slot == "log"


def test_missing_assessor_and_isolation_become_obligations(lab_run):
    res = _expand(lab_run, _classification_request(assessor=None))
    assert res.ok, res.manifest.error
    assert res.json("draft")["obligations"] == ["assessor"] and res.receipt["open_obligations"] == ["assessor"]
    both = _classification_request(assessor=None, required_isolation=None)
    res = _expand(lab_run, both)
    assert res.ok, res.manifest.error
    assert res.json("draft")["obligations"] == ["required_isolation", "assessor"]
    assert res.json("draft")["assessor"] is None and res.json("draft")["required_isolation"] is None


def test_classification_requests_need_a_claim_of_kind_improves_and_a_rule_id(lab_run):
    claim = {"kind": "decodable", "statement": "s", "scope": {"x": 1}}
    _assert_refused(_expand(lab_run, _classification_request(claim=claim)), "only to claims of kind 'improves'")
    _assert_refused(_expand(lab_run, _classification_request(success_rule_id=None)), "success_rule_id")


def test_requests_without_classification_still_take_the_training_path(lab_run):
    req = {
        "claim": {"kind": "improves", "statement": "s", "scope": {"builder": "OrderedSwitchNet"}},
        "success_rule_id": "early-repair",
        "assessor": {"ref": "eidos.assessors.paired-ci@1.1.0"},
        "seeds": [1, 2],
        "evaluation": {"split": "test", "data_seed": 9, "pairs": 100},
        "baseline": {},
        "candidate": {"post": [{"component": "eidos.pytorch.weight-edit", "config": {"edits": []}}]},
        "required_isolation": {"profile": "dev:same-user", "controls": []},
    }
    res = _expand(lab_run, req)
    assert res.ok, res.manifest.error
    draft = res.json("draft")
    assert draft["analysis_unit"] == "pair" and draft["cluster_by"] == "seed"
    assert REPORT_REF not in json.dumps(draft)
