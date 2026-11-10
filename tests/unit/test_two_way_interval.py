"""Two-way (training seed x held-out example) interval regression tests (ADR 0018, review finding F3b).

Every training seed is scored on the SAME held-out examples, so clustering an interval by seed alone ignores
example variation: a candidate that gains on the same few examples in every seed looks perfectly consistent
across seeds. These tests pin the Student t quantile, the two-way estimator, the opt-in
``interval=INTERVAL_TWO_WAY`` of ``evaluate_success_rule`` (the default keeps the original behaviour), and
the paired-ci 1.2.0 assessor that uses it for both row families.

Author tests. Expected values are re-derived here from textbook formulas and known t table values, not read
back from the code under test.
"""

from __future__ import annotations

import copy
import importlib.util
import math
import random
import statistics
import sys
from pathlib import Path

import pytest

from eidos.domain.errors import Invalid
from eidos.domain.evidence import INTERVAL_SEED_CLUSTERED, INTERVAL_TWO_WAY, evaluate_success_rule
from eidos.domain.stats import (
    normal_quantile,
    paired_difference,
    student_t_cdf,
    student_t_quantile,
    two_way_paired_difference,
)

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "components"
ASSESSOR = ("eidos.assessors.paired-ci", "paired_ci.assess:run")
ASSESSOR_REF = "eidos.assessors.paired-ci@1.2.0"
CLASSES = ("cat", "dog", "bird")


def _load_helpers():
    """The assessor module and the fixture builder (plain directories, not installed packages)."""
    for path in (COMPONENTS / "eidos.assessors.paired-ci", COMPONENTS):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import build_assessor_fixtures as fixtures
    from paired_ci import assess

    return assess, fixtures


assess, fixtures = _load_helpers()


def _component_runner():
    """The component test harness of tests/components (the real worker protocol), loaded by path."""
    spec = importlib.util.spec_from_file_location(
        "_eidos_component_test_helpers", ROOT / "tests" / "components" / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(module)
    return module.run_component


run_component = _component_runner()


@pytest.fixture()
def run_assessor(tmp_path):
    counter = {"n": 0}

    def go(evidence):
        counter["n"] += 1
        res = run_component(tmp_path, *ASSESSOR, inputs={"evidence": evidence}, config={}, label=f"a{counter['n']}")
        assert res.ok, res.manifest.error
        return res

    return go


# --------------------------------------------------------------------------------------------------
# Shared constructions
# --------------------------------------------------------------------------------------------------

SEEDS3 = ("0", "1", "2")


def _sample_sd(values):
    return statistics.stdev(values)


def _shared_gain_diffs(n_seeds=3, n_examples=100, gain_on=3):
    """The reviewer's example: the candidate gains on the same ``gain_on`` examples in every seed."""
    seeds, examples, diffs = [], [], []
    for s in range(n_seeds):
        for i in range(n_examples):
            seeds.append(str(s))
            examples.append(str(i))
            diffs.append(1.0 if i < gain_on else 0.0)
    return diffs, seeds, examples


def _classification_rows(role, per_cell_correct, seeds=SEEDS3, n=100):
    """Classification rows for ``role``: ``per_cell_correct(seed_index, example_index) -> bool``."""
    rows = []
    for s, seed in enumerate(seeds):
        for i in range(n):
            label = CLASSES[i % 3]
            ok = per_cell_correct(s, i)
            rows.append(
                {
                    "example_id": str(i),
                    "cluster": seed,
                    "role": role,
                    "label": label,
                    "prediction": label if ok else CLASSES[(i + 1) % 3],
                }
            )
    return rows


def _classification_evidence(plan, baseline_ok, candidate_ok, seeds=SEEDS3, n=100):
    return {
        "plan": plan,
        "arms": {
            "baseline": {"role": "baseline", "rows": _classification_rows("baseline", baseline_ok, seeds, n)},
            "candidate": {"role": "candidate", "rows": _classification_rows("candidate", candidate_ok, seeds, n)},
        },
    }


def _paired_report_rows(role, ref_ok, var_ok, seeds=SEEDS3, n=100):
    """paired-report rows: ``ref_ok(seed_index, pair_index)`` / ``var_ok`` say whether each side is right."""
    rows = []
    for s, seed in enumerate(seeds):
        for i in range(n):
            rows.append(
                {
                    "pair_id": str(i),
                    "cluster": seed,
                    "reference_logit": 1.0 if ref_ok(s, i) else -1.0,
                    "variant_logit": 1.0 if var_ok(s, i) else -1.0,
                    "reference_label": 1,
                    "variant_label": 1,
                }
            )
    return rows


def _paired_report_evidence(plan, baseline, candidate, **kw):
    return {
        "plan": plan,
        "arms": {
            "baseline": {"role": "baseline", "rows": _paired_report_rows("baseline", *baseline, **kw)},
            "candidate": {"role": "candidate", "rows": _paired_report_rows("candidate", *candidate, **kw)},
        },
    }


def _reference_accuracy_plan(min_effect=0.02):
    """A paired-report plan whose rule metric is reference_accuracy (cluster_by seed, no guardrails)."""
    plan = copy.deepcopy(fixtures.plan_body())
    plan["success_rule"]["metric"] = "reference_accuracy"
    plan["success_rule"]["min_effect"] = min_effect
    plan["success_rule"]["guardrails"] = []
    assert plan["cluster_by"] == "seed" and "reference_accuracy" in plan["metric_definitions"]
    return plan


def _per_unit(evidence):
    return assess.per_unit_by_role(evidence["plan"], evidence["arms"])


def _textbook_t_interval(diffs):
    n = len(diffs)
    mean = sum(diffs) / n
    half = student_t_quantile(0.975, n - 1) * _sample_sd(diffs) / math.sqrt(n)
    return mean - half, mean + half


# --------------------------------------------------------------------------------------------------
# 1. Student t quantile
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p", "df", "want"),
    [
        (0.975, 1, 12.7062047),
        (0.975, 2, 4.3026527),
        (0.975, 4, 2.7764451),
        (0.975, 99, 1.9842169),
        (0.975, 199, 1.9719565),
        (0.95, 10, 1.8124611),
    ],
)
def test_student_t_quantile_matches_published_values(p, df, want):
    assert student_t_quantile(p, df) == pytest.approx(want, abs=1e-6)


@pytest.mark.parametrize("df", [1, 2, 3.5, 10, 99, 1e4])
@pytest.mark.parametrize("p", [0.6, 0.9, 0.975, 0.999])
def test_student_t_quantile_is_antisymmetric_and_inverts_the_cdf(p, df):
    q = student_t_quantile(p, df)
    assert q > 0
    assert student_t_quantile(1 - p, df) == pytest.approx(-q, abs=1e-9)
    assert student_t_cdf(q, df) == pytest.approx(p, abs=1e-9)
    assert student_t_cdf(-q, df) == pytest.approx(1 - p, abs=1e-9)
    assert student_t_quantile(0.5, df) == pytest.approx(0.0, abs=1e-9)


def test_student_t_cdf_matches_closed_forms_for_one_and_two_degrees_of_freedom():
    for t in (-7.0, -1.3, 0.0, 0.4, 3.0, 25.0):
        assert student_t_cdf(t, 1) == pytest.approx(0.5 + math.atan(t) / math.pi, abs=1e-12)
        assert student_t_cdf(t, 2) == pytest.approx(0.5 + t / (2 * math.sqrt(2 + t * t)), abs=1e-12)


def test_student_t_quantile_approaches_the_normal_for_large_df_and_decreases_with_df():
    z = normal_quantile(0.975)
    assert student_t_quantile(0.975, 1e5) == pytest.approx(z, abs=1e-4)
    assert student_t_quantile(0.975, math.inf) == pytest.approx(z, abs=1e-12)
    qs = [student_t_quantile(0.975, df) for df in (1, 2, 5, 20, 100, 1000, 1e5)]
    assert qs == sorted(qs, reverse=True) and qs[-1] > z


def test_student_t_quantile_with_fractional_degrees_of_freedom():
    q = student_t_quantile(0.975, 2.5)
    assert student_t_cdf(q, 2.5) == pytest.approx(0.975, abs=1e-9)
    # between the integer neighbours, as the quantile is monotone in df
    assert student_t_quantile(0.975, 3) < q < student_t_quantile(0.975, 2)


@pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.5])
def test_student_t_quantile_rejects_probabilities_outside_the_open_unit_interval(p):
    with pytest.raises(ValueError, match="p must be"):
        student_t_quantile(p, 5)


@pytest.mark.parametrize("df", [0, -2.0])
def test_student_t_quantile_rejects_non_positive_degrees_of_freedom(df):
    with pytest.raises(ValueError, match="df must be"):
        student_t_quantile(0.975, df)


# --------------------------------------------------------------------------------------------------
# 2. The reviewer's shared-error example at the statistics level
# --------------------------------------------------------------------------------------------------


def test_reviewers_shared_gain_seed_only_interval_is_overconfident_two_way_is_not():
    diffs, seeds, examples = _shared_gain_diffs()
    assert len(diffs) == 300
    old = paired_difference(diffs, seeds)
    # documents the flaw: every seed has the identical mean, so the seed-only interval collapses to a point
    assert old["estimate"] == pytest.approx(0.03, abs=1e-12)
    assert old["ci_low"] == pytest.approx(0.03, abs=1e-9) and old["ci_high"] == pytest.approx(0.03, abs=1e-9)
    assert old["independent_units"] == 3

    new = two_way_paired_difference(diffs, seeds, examples)
    per_example = [1.0] * 3 + [0.0] * 97  # the 100 per-example differences (identical in every seed)
    sd = _sample_sd(per_example)
    half = student_t_quantile(0.975, 99) * sd / 10
    assert new["estimate"] == pytest.approx(0.03, abs=1e-12)
    assert new["ci_low"] < 0.02 < new["ci_high"]
    assert new["ci_low"] == pytest.approx(0.03 - half, rel=1e-9)
    assert new["ci_high"] == pytest.approx(0.03 + half, rel=1e-9)
    assert new["ci_low"] == pytest.approx(-0.0040, abs=1e-4)
    assert new["se"] == pytest.approx(sd / 10, rel=1e-9)
    assert new["df"] == pytest.approx(99.0, rel=1e-9)
    assert new["variance_components"]["seed"] == pytest.approx(0.0, abs=1e-12)
    assert new["variance_components"]["example"] == pytest.approx(sd * sd / 100, rel=1e-9)
    assert new["seeds"] == 3 and new["examples"] == 100 and new["units"] == 300
    assert new["seed_variance_estimated"] is True and new["independent_units"] == 3
    assert new["method"] == "two-way-seed-example-t/1" and new["confidence"] == 0.95


# --------------------------------------------------------------------------------------------------
# 3. The same example through evaluate_success_rule
# --------------------------------------------------------------------------------------------------


def _reviewer_per_unit():
    base = [{"unit": str(i), "cluster": seed, "accuracy": 0.0 if i < 3 else 1.0} for seed in SEEDS3 for i in range(100)]
    cand = [{"unit": str(i), "cluster": seed, "accuracy": 1.0} for seed in SEEDS3 for i in range(100)]
    return {"baseline": base, "candidate": cand}


def test_reviewers_example_is_inconclusive_under_the_two_way_interval():
    plan = fixtures.accuracy_plan()
    assert plan["cluster_by"] == "seed" and plan["success_rule"]["min_effect"] == 0.02
    assert plan["success_rule"]["decision_standard"]["kind"] == "paired-difference-ci"
    result = evaluate_success_rule(plan, _reviewer_per_unit(), interval=INTERVAL_TWO_WAY)
    eff = result["effect"]
    assert result["outcome"] == "inconclusive"
    assert eff["estimate"] == pytest.approx(0.03) and eff["ci_low"] == pytest.approx(-0.0040, abs=1e-4)
    assert eff["ci_high"] > 0.02 and eff["method"] == "two-way-seed-example-t/1"


def test_the_default_interval_keeps_the_original_behaviour():
    plan = fixtures.accuracy_plan()
    default = evaluate_success_rule(plan, _reviewer_per_unit())
    assert default["outcome"] == "protocol-passed"  # the flaw, preserved for assessor releases 1.0.0 / 1.1.0
    assert default["effect"]["ci_low"] == pytest.approx(0.03, abs=1e-9)
    assert "method" not in default["effect"]
    explicit = evaluate_success_rule(plan, _reviewer_per_unit(), interval=INTERVAL_SEED_CLUSTERED)
    assert explicit == default
    # the default is exactly paired_difference clustered by seed
    diffs, seeds, _ = _shared_gain_diffs()
    assert default["effect"] == paired_difference(diffs, seeds, 0.95)


def test_an_unknown_interval_method_is_refused():
    with pytest.raises(Invalid, match="unknown interval method"):
        evaluate_success_rule(fixtures.accuracy_plan(), _reviewer_per_unit(), interval="bootstrap")


# --------------------------------------------------------------------------------------------------
# 4. The reviewer's example through the paired-ci 1.2.0 assessor, both row families
# --------------------------------------------------------------------------------------------------


def test_assessor_classification_rows_reviewers_example_is_not_a_pass(run_assessor):
    evidence = _classification_evidence(
        fixtures.accuracy_plan(), baseline_ok=lambda s, i: i >= 3, candidate_ok=lambda s, i: True
    )
    in_process = evaluate_success_rule(
        evidence["plan"], _per_unit(evidence), interval=INTERVAL_TWO_WAY
    )
    assert in_process["outcome"] == "inconclusive"
    # what 1.1.0 computed from the same rows: a pass, because the seed-only interval collapses to a point
    assert evaluate_success_rule(evidence["plan"], _per_unit(evidence))["outcome"] == "protocol-passed"

    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["units"] == 300 and res.receipt["clusters"] == 3
    assert res.receipt["metrics"]["estimate"] == pytest.approx(0.03)
    assert res.receipt["metrics"]["ci_low"] == pytest.approx(-0.0040, abs=1e-4)
    evaluation = res.json("evaluation")
    assert evaluation["outcome"] == "inconclusive"
    assert evaluation["interval"] == "two-way-seed-example-t"
    assert evaluation["assessor"] == ASSESSOR_REF == "eidos.assessors.paired-ci@1.2.0"
    assert evaluation["metric"] == "accuracy" and evaluation["cluster_by"] == "seed"
    assert evaluation["effect"]["method"] == "two-way-seed-example-t/1"
    assert evaluation["effect"]["df"] == pytest.approx(99.0, rel=1e-6)
    assert evaluation["effect"]["seeds"] == 3 and evaluation["effect"]["examples"] == 100
    assert evaluation["effect"]["ci_low"] == pytest.approx(in_process["effect"]["ci_low"], rel=1e-12)
    assert evaluation["unbounded_fields"] == []


def test_assessor_paired_report_rows_reviewers_example_is_not_a_pass(run_assessor):
    plan = _reference_accuracy_plan()
    # the baseline reference side is wrong on the same 3 pairs in every seed; the candidate is right on all
    evidence = _paired_report_evidence(
        plan, baseline=(lambda s, i: i >= 3, lambda s, i: True), candidate=(lambda s, i: True, lambda s, i: True)
    )
    assert evaluate_success_rule(plan, _per_unit(evidence))["outcome"] == "protocol-passed"  # seed-only: a pass
    two_way = evaluate_success_rule(plan, _per_unit(evidence), interval=INTERVAL_TWO_WAY)
    assert two_way["outcome"] == "inconclusive" and two_way["metric"] == "reference_accuracy"
    assert two_way["effect"]["ci_low"] == pytest.approx(-0.0040, abs=1e-4)

    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["units"] == 300
    assert res.receipt["metrics"]["estimate"] == pytest.approx(0.03)
    assert res.receipt["metrics"]["ci_low"] == pytest.approx(two_way["effect"]["ci_low"], rel=1e-12)
    evaluation = res.json("evaluation")
    assert evaluation["interval"] == "two-way-seed-example-t" and evaluation["assessor"] == ASSESSOR_REF
    assert evaluation["metric"] == "reference_accuracy" and evaluation["effect"]["df"] == pytest.approx(99.0, rel=1e-6)


# --------------------------------------------------------------------------------------------------
# 5. A real, broad improvement still passes
# --------------------------------------------------------------------------------------------------


def test_a_broad_improvement_passes_through_the_assessor(run_assessor):
    rng = random.Random(20240611)
    n_seeds, n = 3, 400
    base = [[rng.random() < 0.60 for _ in range(n)] for _ in range(n_seeds)]
    cand = [[rng.random() < 0.80 for _ in range(n)] for _ in range(n_seeds)]
    evidence = _classification_evidence(
        fixtures.accuracy_plan(), lambda s, i: base[s][i], lambda s, i: cand[s][i], n=n
    )
    diffs = [float(cand[s][i]) - float(base[s][i]) for s in range(n_seeds) for i in range(n)]
    gain = sum(diffs) / len(diffs)
    assert gain == pytest.approx(0.20, abs=0.05)

    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "protocol-passed" and res.receipt["units"] == 1200
    assert res.receipt["metrics"]["estimate"] == pytest.approx(gain, abs=1e-12)
    assert res.receipt["metrics"]["ci_low"] >= 0.02
    effect = res.json("evaluation")["effect"]
    assert effect["ci_low"] > 0.10 and effect["ci_low"] < gain < effect["ci_high"]
    assert effect["seeds"] == 3 and effect["examples"] == 400


def test_per_seed_gains_that_differ_a_little_still_pass_at_the_statistics_level():
    n = 400
    gains = {"0": range(0, 60), "1": range(100, 172), "2": range(200, 280)}  # 0.15, 0.18, 0.20
    diffs, seeds, examples = [], [], []
    for seed, gained in gains.items():
        gained = set(gained)
        for i in range(n):
            diffs.append(1.0 if i in gained else 0.0)
            seeds.append(seed)
            examples.append(str(i))
    result = two_way_paired_difference(diffs, seeds, examples)
    assert result["estimate"] == pytest.approx((60 + 72 + 80) / 1200)
    assert result["ci_low"] >= 0.02
    assert result["ci_low"] < result["estimate"] < result["ci_high"]
    # the few seeds widen the interval: Student t with few degrees of freedom, not the normal 1.96
    assert result["df"] < 10
    assert (result["ci_high"] - result["estimate"]) / result["se"] > 2.5


# --------------------------------------------------------------------------------------------------
# 6. One seed: the plain paired t-interval over examples
# --------------------------------------------------------------------------------------------------


def test_one_seed_is_the_textbook_paired_t_interval_over_examples():
    rng = random.Random(7)
    n = 200
    diffs = [float(rng.choice((-1, 0, 0, 1, 1))) for _ in range(n)]
    got = two_way_paired_difference(diffs, ["11"] * n, [str(i) for i in range(n)])
    lo, hi = _textbook_t_interval(diffs)
    assert student_t_quantile(0.975, n - 1) == pytest.approx(1.9719565, abs=1e-6)
    assert got["ci_low"] == pytest.approx(lo, rel=1e-12) and got["ci_high"] == pytest.approx(hi, rel=1e-12)
    assert got["estimate"] == pytest.approx(sum(diffs) / n, rel=1e-12)
    assert got["se"] == pytest.approx(_sample_sd(diffs) / math.sqrt(n), rel=1e-12)
    assert got["seed_variance_estimated"] is False and got["seeds"] == 1 and got["examples"] == n
    assert got["df"] == pytest.approx(n - 1, rel=1e-12) and got["independent_units"] == n
    assert got["variance_components"] == {"seed": None, "example": pytest.approx(got["se"] ** 2, rel=1e-12)}


def test_one_seed_that_must_be_clustered_by_seed_is_unbounded():
    rng = random.Random(7)
    n = 200
    diffs = [float(rng.choice((-1, 0, 1))) for _ in range(n)]
    seeds, examples = ["11"] * n, [str(i) for i in range(n)]
    got = two_way_paired_difference(diffs, seeds, examples, require_seed_variance=True)
    assert got["ci_low"] == -math.inf and got["ci_high"] == math.inf and math.isnan(got["se"])
    assert got["seed_variance_estimated"] is False and got["df"] is None
    # several seeds: the flag changes nothing
    many = two_way_paired_difference(diffs, ["a", "b"] * (n // 2), examples)
    assert two_way_paired_difference(diffs, ["a", "b"] * (n // 2), examples, require_seed_variance=True) == many


def test_one_example_is_unbounded_whatever_the_seeds():
    got = two_way_paired_difference([0.0, 1.0, 1.0], ["a", "b", "c"], ["x", "x", "x"])
    assert got["ci_low"] == -math.inf and got["ci_high"] == math.inf and got["examples"] == 1


def test_one_seed_through_the_assessor_matches_the_textbook_interval(run_assessor):
    rng = random.Random(99)
    n = 200
    base = [rng.random() < 0.55 for _ in range(n)]
    cand = [rng.random() < 0.70 for _ in range(n)]
    diffs = [float(c) - float(b) for b, c in zip(base, cand, strict=True)]
    lo, hi = _textbook_t_interval(diffs)

    plan = fixtures.accuracy_plan(seeds=(11,), cluster_by=None)
    evidence = _classification_evidence(plan, lambda s, i: base[i], lambda s, i: cand[i], seeds=("11",), n=n)
    res = run_assessor(evidence)
    effect = res.json("evaluation")["effect"]
    assert effect["ci_low"] == pytest.approx(lo, rel=1e-12) and effect["ci_high"] == pytest.approx(hi, rel=1e-12)
    assert effect["estimate"] == pytest.approx(sum(diffs) / n, rel=1e-12)
    assert effect["seed_variance_estimated"] is False and effect["df"] == pytest.approx(n - 1, rel=1e-12)
    assert res.receipt["metrics"]["ci_low"] == pytest.approx(lo, rel=1e-12)
    assert res.receipt["metrics"]["ci_high"] == pytest.approx(hi, rel=1e-12)
    expected = "protocol-passed" if lo >= 0.02 else ("protocol-failed" if hi < 0.02 else "inconclusive")
    assert res.receipt["outcome"] == expected
    assert res.json("evaluation")["unbounded_fields"] == []

    # the same rows under a plan that declares cluster_by "seed": one seed cannot support that conclusion
    clustered = fixtures.accuracy_plan(seeds=(11,), cluster_by="seed")
    again = run_assessor(_classification_evidence(clustered, lambda s, i: base[i], lambda s, i: cand[i],
                                                  seeds=("11",), n=n))
    assert again.receipt["outcome"] == "inconclusive"
    assert again.receipt["metrics"]["ci_low"] is None and again.receipt["metrics"]["ci_high"] is None
    assert {"effect.ci_low", "effect.ci_high"} <= set(again.json("evaluation")["unbounded_fields"])


# --------------------------------------------------------------------------------------------------
# 7. Conservativeness properties
# --------------------------------------------------------------------------------------------------


def _crossed(rng, n_seeds, n_examples, sd_seed=0.2, sd_example=0.3, sd_error=0.4, keep=1.0):
    """d = mu + a_seed + b_example + e over the crossed cells; ``keep`` < 1 drops cells (unbalanced)."""
    a = [rng.gauss(0, sd_seed) for _ in range(n_seeds)]
    b = [rng.gauss(0, sd_example) for _ in range(n_examples)]
    diffs, seeds, examples = [], [], []
    for s in range(n_seeds):
        for i in range(n_examples):
            if keep < 1.0 and rng.random() > keep:
                continue
            diffs.append(0.1 + a[s] + b[i] + rng.gauss(0, sd_error))
            seeds.append(f"s{s}")
            examples.append(f"e{i}")
    return diffs, seeds, examples


@pytest.mark.parametrize(("n_seeds", "n_examples"), [(2, 15), (3, 40), (5, 25), (8, 60)])
@pytest.mark.parametrize("keep", [1.0, 0.7])
def test_two_way_variance_is_at_least_each_one_way_cluster_variance(n_seeds, n_examples, keep):
    for replicate in range(5):
        rng = random.Random(1000 * n_seeds + 10 * n_examples + replicate)
        diffs, seeds, examples = _crossed(rng, n_seeds, n_examples, keep=keep)
        got = two_way_paired_difference(diffs, seeds, examples)
        by_seed = paired_difference(diffs, seeds)["se"] ** 2
        by_example = paired_difference(diffs, examples)["se"] ** 2
        assert got["se"] ** 2 >= by_seed and got["se"] ** 2 >= by_example
        # in fact it is exactly their sum (each term carries its own G/(G-1) factor)
        assert got["se"] ** 2 == pytest.approx(by_seed + by_example, rel=1e-12)
        assert got["variance_components"]["seed"] == pytest.approx(by_seed, rel=1e-12)
        assert got["variance_components"]["example"] == pytest.approx(by_example, rel=1e-12)
        assert got["estimate"] == pytest.approx(sum(diffs) / len(diffs), rel=1e-12)
        assert got["ci_low"] < got["estimate"] < got["ci_high"]
        # never narrower than the seed-only normal interval it replaces
        old = paired_difference(diffs, seeds)
        assert got["ci_high"] - got["ci_low"] > old["ci_high"] - old["ci_low"]


@pytest.mark.parametrize(("n_seeds", "n_examples"), [(2, 10), (3, 30), (6, 12), (10, 50)])
def test_balanced_design_variance_is_the_sum_of_the_two_marginal_mean_variances(n_seeds, n_examples):
    rng = random.Random(n_seeds * 100 + n_examples)
    diffs, seeds, examples = _crossed(rng, n_seeds, n_examples)
    grid = [diffs[s * n_examples:(s + 1) * n_examples] for s in range(n_seeds)]
    seed_means = [sum(row) / n_examples for row in grid]
    example_means = [sum(grid[s][i] for s in range(n_seeds)) / n_seeds for i in range(n_examples)]
    want = statistics.variance(seed_means) / n_seeds + statistics.variance(example_means) / n_examples
    got = two_way_paired_difference(diffs, seeds, examples)
    assert got["se"] ** 2 == pytest.approx(want, rel=1e-12)
    # Welch-Satterthwaite degrees of freedom from the two components
    v_seed, v_example = statistics.variance(seed_means) / n_seeds, statistics.variance(example_means) / n_examples
    df = (v_seed + v_example) ** 2 / (v_seed**2 / (n_seeds - 1) + v_example**2 / (n_examples - 1))
    assert got["df"] == pytest.approx(df, rel=1e-9)
    mean = sum(diffs) / len(diffs)
    half = student_t_quantile(0.975, df) * math.sqrt(want)
    assert got["ci_low"] == pytest.approx(mean - half, rel=1e-9) and got["ci_high"] == pytest.approx(mean + half, rel=1e-9)


def test_the_interval_does_not_depend_on_row_order_or_label_spelling():
    rng = random.Random(5)
    diffs, seeds, examples = _crossed(rng, 4, 20)
    base = two_way_paired_difference(diffs, seeds, examples)
    order = list(range(len(diffs)))
    random.Random(6).shuffle(order)
    shuffled = two_way_paired_difference([diffs[k] for k in order], [seeds[k] for k in order],
                                         [examples[k] for k in order])
    for key in ("estimate", "ci_low", "ci_high", "se", "df"):
        assert shuffled[key] == pytest.approx(base[key], rel=1e-12)


# --------------------------------------------------------------------------------------------------
# 8. Monte Carlo coverage
# --------------------------------------------------------------------------------------------------


def test_two_way_interval_covers_the_true_mean_while_the_seed_only_interval_does_not():
    rng = random.Random(1234)
    mu, n_seeds, n_examples, replicates = 0.10, 5, 60, 300
    seeds = [f"s{s}" for s in range(n_seeds) for _ in range(n_examples)]
    examples = [f"e{i}" for _ in range(n_seeds) for i in range(n_examples)]
    covered_two_way = covered_seed_only = 0
    for _ in range(replicates):
        a = [rng.gauss(0, 0.05) for _ in range(n_seeds)]
        b = [rng.gauss(0, 0.30) for _ in range(n_examples)]
        diffs = [mu + a[s] + b[i] + rng.gauss(0, 0.30) for s in range(n_seeds) for i in range(n_examples)]
        two_way = two_way_paired_difference(diffs, seeds, examples)
        seed_only = paired_difference(diffs, seeds)
        covered_two_way += two_way["ci_low"] <= mu <= two_way["ci_high"]
        covered_seed_only += seed_only["ci_low"] <= mu <= seed_only["ci_high"]
    two_way_rate, seed_only_rate = covered_two_way / replicates, covered_seed_only / replicates
    print(f"coverage over {replicates} replicates: two-way {two_way_rate:.4f}, seed-only {seed_only_rate:.4f}")
    assert two_way_rate >= 0.93
    assert seed_only_rate < two_way_rate
    assert seed_only_rate < 0.90  # clearly anti-conservative: it ignores the example component


# --------------------------------------------------------------------------------------------------
# 9. Guardrails use the same interval
# --------------------------------------------------------------------------------------------------


def _guardrail_evidence():
    """paired-report rows. Target both_correct_rate: baseline's variant side is wrong on pairs 0-49, the
    candidate's is right on all, so the candidate gains on pairs 3-49 in every seed (a large effect).
    Guardrail reference_accuracy: the candidate's reference side is wrong on the SAME pairs 0-2 in every
    seed, a regression of 0.03 that is perfectly consistent across seeds."""
    plan = copy.deepcopy(fixtures.plan_body())
    assert plan["success_rule"]["metric"] == "both_correct_rate"
    assert plan["success_rule"]["guardrails"] == [{"metric": "reference_accuracy", "max_regression": 0.05}]
    return _paired_report_evidence(
        plan,
        baseline=(lambda s, i: True, lambda s, i: i >= 50),
        candidate=(lambda s, i: i >= 3, lambda s, i: True),
    )


def test_a_guardrail_regression_on_shared_examples_is_violated_only_under_the_two_way_interval():
    evidence = _guardrail_evidence()
    plan = evidence["plan"]
    per_unit = _per_unit(evidence)

    default = evaluate_success_rule(plan, per_unit)
    assert default["outcome"] == "protocol-passed"
    (guard,) = default["guardrails"]
    assert guard["metric"] == "reference_accuracy" and guard["status"] == "ok"
    assert guard["estimate"] == pytest.approx(-0.03) and guard["ci_low"] == pytest.approx(-0.03, abs=1e-9)

    two_way = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    assert two_way["effect"]["estimate"] == pytest.approx(0.47)
    assert two_way["effect"]["ci_low"] >= 0.05  # the target itself is a clear pass, so only the guardrail decides
    (guard,) = two_way["guardrails"]
    assert guard["metric"] == "reference_accuracy" and guard["status"] == "violated"
    assert guard["estimate"] == pytest.approx(-0.03)
    assert guard["ci_low"] < -0.05 < guard["ci_high"] and guard["method"] == "two-way-seed-example-t/1"
    assert guard["ci_low"] == pytest.approx(-0.0640, abs=1e-3)
    assert two_way["outcome"] == "inconclusive"


def test_a_guardrail_violation_through_the_assessor_withholds_the_pass(run_assessor):
    res = run_assessor(_guardrail_evidence())
    assert res.receipt["outcome"] == "inconclusive"
    evaluation = res.json("evaluation")
    assert evaluation["interval"] == "two-way-seed-example-t"
    assert [(g["metric"], g["status"]) for g in evaluation["guardrails"]] == [("reference_accuracy", "violated")]
