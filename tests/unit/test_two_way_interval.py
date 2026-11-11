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
    MIN_SEED_CLUSTERS,
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
    # F5 (B1): every seed has the same mean, so the seed variance is estimated as zero. That is never taken at face
    # value: the lower bound is unbounded (no pass); the upper bound is the two-way computation.
    assert new["ci_low"] == -math.inf and new["unbounded_reason"] == "seed-variance-collapsed"
    assert new["ci_high"] == pytest.approx(0.03 + half, rel=1e-9)
    assert 0.03 - half == pytest.approx(-0.0040, abs=1e-4)  # what the bounded R4 interval was
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
    assert eff["estimate"] == pytest.approx(0.03) and eff["ci_low"] == -math.inf  # F5 (B1): seed variance collapsed
    assert eff["unbounded_reason"] == "seed-variance-collapsed"
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
    assert res.receipt["metrics"]["ci_low"] is None  # F5 (B1): unbounded below (seed variance collapsed)
    evaluation = res.json("evaluation")
    assert evaluation["outcome"] == "inconclusive"
    assert evaluation["interval"] == "two-way-seed-example-t"
    assert evaluation["assessor"] == ASSESSOR_REF == "eidos.assessors.paired-ci@1.2.0"
    assert evaluation["metric"] == "accuracy" and evaluation["cluster_by"] == "seed"
    assert evaluation["effect"]["method"] == "two-way-seed-example-t/1"
    assert evaluation["effect"]["df"] == pytest.approx(99.0, rel=1e-6)
    assert evaluation["effect"]["seeds"] == 3 and evaluation["effect"]["examples"] == 100
    assert in_process["effect"]["ci_low"] == -math.inf and evaluation["effect"]["ci_low"] is None
    assert evaluation["effect"]["unbounded_reason"] == "seed-variance-collapsed"
    assert evaluation["unbounded_fields"] == ["effect.ci_low"]


def test_assessor_paired_report_rows_reviewers_example_is_not_a_pass(run_assessor):
    plan = _reference_accuracy_plan()
    # the baseline reference side is wrong on the same 3 pairs in every seed; the candidate is right on all
    evidence = _paired_report_evidence(
        plan, baseline=(lambda s, i: i >= 3, lambda s, i: True), candidate=(lambda s, i: True, lambda s, i: True)
    )
    assert evaluate_success_rule(plan, _per_unit(evidence))["outcome"] == "protocol-passed"  # seed-only: a pass
    two_way = evaluate_success_rule(plan, _per_unit(evidence), interval=INTERVAL_TWO_WAY)
    assert two_way["outcome"] == "inconclusive" and two_way["metric"] == "reference_accuracy"
    assert two_way["effect"]["ci_low"] == -math.inf  # F5 (B1): seed variance collapsed

    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["units"] == 300
    assert res.receipt["metrics"]["estimate"] == pytest.approx(0.03)
    assert res.receipt["metrics"]["ci_low"] is None
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
    assert got["ci_high"] == pytest.approx(mean + half, rel=1e-9)
    if n_seeds < MIN_SEED_CLUSTERS:  # F5 (B1): too few seed clusters: estimates reported, never a pass
        assert got["ci_low"] == -math.inf and got["unbounded_reason"] == "too-few-seed-clusters"
    else:
        assert got["ci_low"] == pytest.approx(mean - half, rel=1e-9) and "unbounded_reason" not in got


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
        # the candidate loses 0, 1 or 2 pairs by seed: seed variation, so the target's seed variance is estimated
        candidate=(lambda s, i: i >= 3, lambda s, i: not 50 <= i < 50 + s),
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
    assert two_way["effect"]["estimate"] == pytest.approx(0.46)
    assert two_way["effect"]["ci_low"] >= 0.05  # the target itself is a clear pass, so only the guardrail decides
    (guard,) = two_way["guardrails"]
    assert guard["metric"] == "reference_accuracy" and guard["status"] == "violated"
    assert guard["estimate"] == pytest.approx(-0.03)
    # the guardrail regression is on the same 3 pairs in every seed: its seed variance collapses (F5, B1), so its
    # lower bound is unbounded and the guardrail cannot be shown to hold
    assert guard["ci_low"] == -math.inf < -0.05 < guard["ci_high"] and guard["method"] == "two-way-seed-example-t/1"
    assert guard["unbounded_reason"] == "seed-variance-collapsed"
    assert two_way["outcome"] == "inconclusive"


def test_a_guardrail_violation_through_the_assessor_withholds_the_pass(run_assessor):
    res = run_assessor(_guardrail_evidence())
    assert res.receipt["outcome"] == "inconclusive"
    evaluation = res.json("evaluation")
    assert evaluation["interval"] == "two-way-seed-example-t"
    assert [(g["metric"], g["status"]) for g in evaluation["guardrails"]] == [("reference_accuracy", "violated")]


# --------------------------------------------------------------------------------------------------
# 10. F5 (review B1): collapsed or under-replicated seed variance never supports a pass
# --------------------------------------------------------------------------------------------------
#
# The reviewer's discrete few-seed design: every training seed gives +1 on EVERY example with probability 0.4 and -1
# with probability 0.6 (population effect 0.4 - 0.6 = -0.2). Three seeds are all +1 with probability 0.064, and then
# every cell is identical, the seed-variance estimate is exactly zero, and a bounded interval would be the point
# [1, 1]. The fix keeps the estimates but makes the lower bound -inf when there are fewer than MIN_SEED_CLUSTERS seed
# clusters or when the seed (or total) variance estimate collapses. The upper bound is kept, so that identical arms
# still fail a rule that needs an improvement.

MC_SLACK = 0.01  # Monte Carlo slack on a rate near 0.025 over <= 2000 replicates (3 standard errors ~ 0.011)


def _accuracy_per_unit(ok, seeds=SEEDS3, n=100):
    """Domain-level per-unit rows (metric ``accuracy``): ``ok(seed_index, example_index) -> bool``."""
    return [
        {"unit": str(i), "cluster": seed, "accuracy": float(ok(s, i))} for s, seed in enumerate(seeds) for i in range(n)
    ]


def _discrete_cells(signs, n_examples):
    """``signs[s]`` is seed s's difference on every example: the reviewer's discrete design."""
    diffs, seeds, examples = [], [], []
    for s, sign in enumerate(signs):
        for i in range(n_examples):
            diffs.append(float(sign))
            seeds.append(f"s{s}")
            examples.append(f"e{i}")
    return diffs, seeds, examples


@pytest.mark.parametrize("n_seeds", [3, 4, 5, 8])
def test_discrete_all_positive_seeds_collapse_to_an_unbounded_lower_bound(n_seeds):
    diffs, seeds, examples = _discrete_cells([1] * n_seeds, 100)
    got = two_way_paired_difference(diffs, seeds, examples)
    assert got["estimate"] == 1.0 and got["seeds"] == n_seeds and got["examples"] == 100
    assert got["ci_low"] == -math.inf  # never a point interval [1, 1]
    # With >= 3 seeds the seed variance is tested before the total variance, and the total is V_seed + V_example, so
    # a zero total always has a zero seed component: "seed-variance-collapsed" is what fires. "variance-collapsed"
    # is reachable only when the seed variance is not estimated (one seed), see the one-seed test below.
    assert got["unbounded_reason"] == "seed-variance-collapsed"
    # the estimates are still reported, and the upper bound is the (degenerate) computed one
    assert got["se"] == 0.0 and got["ci_high"] == 1.0 and got["df"] is None
    assert got["variance_components"] == {"seed": 0.0, "example": 0.0}
    assert got["seed_variance_estimated"] is True and got["method"] == "two-way-seed-example-t/1"
    assert MIN_SEED_CLUSTERS == 3


def test_discrete_all_positive_seeds_are_not_a_pass_in_the_domain_rule():
    plan = fixtures.accuracy_plan()
    assert plan["cluster_by"] == "seed" and plan["success_rule"]["min_effect"] == 0.02
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: False),
        "candidate": _accuracy_per_unit(lambda s, i: True),
    }
    result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    assert result["outcome"] == "inconclusive"  # ci_low is -inf, ci_high (= 1) is not below min_effect
    eff = result["effect"]
    assert eff["estimate"] == 1.0 and eff["ci_low"] == -math.inf and eff["ci_high"] == 1.0
    assert eff["unbounded_reason"] == "seed-variance-collapsed"
    assert result["outcome"] != "protocol-passed"
    # the seed-only default (kept for assessor releases 1.0.0 / 1.1.0) is exactly the false pass this guards against
    assert evaluate_success_rule(plan, per_unit)["outcome"] == "protocol-passed"


def test_discrete_all_positive_seeds_through_the_assessor_are_not_a_pass(run_assessor):
    # classification rows: the candidate is right on every example in every seed, the baseline wrong on every one
    evidence = _classification_evidence(
        fixtures.accuracy_plan(), baseline_ok=lambda s, i: False, candidate_ok=lambda s, i: True
    )
    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["outcome"] != "protocol-passed"
    assert res.receipt["units"] == 300 and res.receipt["clusters"] == 3
    assert res.receipt["metrics"]["estimate"] == 1.0
    assert res.receipt["metrics"]["ci_low"] is None and res.receipt["metrics"]["ci_high"] == 1.0
    evaluation = res.json("evaluation")
    assert evaluation["outcome"] == "inconclusive" and evaluation["interval"] == "two-way-seed-example-t"
    assert evaluation["effect"]["unbounded_reason"] == "seed-variance-collapsed"
    assert evaluation["effect"]["ci_low"] is None and evaluation["unbounded_fields"] == ["effect.ci_low"]


@pytest.mark.parametrize("n_seeds", [2, 3, 4, 5, 6])
def test_no_discrete_seed_pattern_passes_exactly(n_seeds):
    """Exhaustive over the 2**S outcomes of the discrete design (each seed +1 or -1 on every example): none of them
    passes a min_effect of 0.02, whatever the sign pattern, and the all-same patterns are unbounded below."""
    for pattern in range(2**n_seeds):
        signs = [1 if pattern >> s & 1 else -1 for s in range(n_seeds)]
        got = two_way_paired_difference(*_discrete_cells(signs, 20))
        assert not got["ci_low"] >= 0.02, (signs, got)
        if len(set(signs)) == 1 or n_seeds < MIN_SEED_CLUSTERS:
            assert got["ci_low"] == -math.inf and "unbounded_reason" in got, (signs, got)


def _discrete_replicate(rng, n_seeds, n_examples, p_favourable=0.4):
    """One draw of the discrete design: seed s is +1 on every example with p_favourable, else -1."""
    signs = [1 if rng.random() < p_favourable else -1 for _ in range(n_seeds)]
    return _discrete_cells(signs, n_examples)


def _near_discrete_replicate(rng, n_seeds, n_examples, noise=0.03, p_favourable=0.4, hit_rate=0.10):
    """A seed is favourable with p_favourable: the candidate then gains on a random ``hit_rate`` share of the examples
    (drawn independently per seed), else it loses on one. Every cell also carries symmetric extra noise: +1 or -1
    with probability ``noise`` each. The true effect is (2 p_favourable - 1) * hit_rate = -0.02 for the defaults."""
    diffs, seeds, examples = [], [], []
    hits = round(hit_rate * n_examples)
    for s in range(n_seeds):
        sign = 1.0 if rng.random() < p_favourable else -1.0
        hit = set(rng.sample(range(n_examples), hits))
        for i in range(n_examples):
            extra = rng.choices((-1.0, 0.0, 1.0), (noise, 1 - 2 * noise, noise))[0]
            diffs.append(extra + (sign if i in hit else 0.0))
            seeds.append(f"s{s}")
            examples.append(f"e{i}")
    return diffs, seeds, examples


def _monte_carlo(replicate, true_effect, rng_seed, replicates=2000, min_effect=0.02, **design):
    """(false-pass rate, coverage of true_effect by [ci_low, ci_high], share unbounded below) over the replicates."""
    assert true_effect < min_effect  # every pass is a false pass
    rng = random.Random(rng_seed)
    false_pass = covered = unbounded = 0
    for _ in range(replicates):
        got = two_way_paired_difference(*replicate(rng, **design))
        false_pass += got["ci_low"] >= min_effect
        covered += got["ci_low"] <= true_effect <= got["ci_high"]
        unbounded += got["ci_low"] == -math.inf
    return false_pass / replicates, covered / replicates, unbounded / replicates


def _exact_discrete_coverage(n_seeds, true_effect=-0.2, p_favourable=0.4):
    """Coverage of the true effect in the discrete design by enumerating the 2**S sign patterns (probability-weighted)."""
    total = 0.0
    for pattern in range(2**n_seeds):
        signs = [1 if pattern >> s & 1 else -1 for s in range(n_seeds)]
        weight = math.prod(p_favourable if sign == 1 else 1 - p_favourable for sign in signs)
        got = two_way_paired_difference(*_discrete_cells(signs, 10))
        total += weight * (got["ci_low"] <= true_effect <= got["ci_high"])
    return total


@pytest.mark.parametrize(("n_seeds", "min_coverage"), [(3, 0.70), (5, 0.85)])
def test_monte_carlo_discrete_few_seed_design_has_no_false_passes(n_seeds, min_coverage):
    false_pass, coverage, unbounded = _monte_carlo(
        _discrete_replicate, true_effect=-0.2, rng_seed=20260611, n_seeds=n_seeds, n_examples=50
    )
    exact = _exact_discrete_coverage(n_seeds)
    print(
        f"discrete design S={n_seeds} N=50: false-pass {false_pass:.4f}, coverage of -0.2 {coverage:.4f} "
        f"(exact {exact:.4f}), unbounded below {unbounded:.4f}"
    )
    assert false_pass <= 0.025 + MC_SLACK  # in fact exactly 0: see test_no_discrete_seed_pattern_passes_exactly
    # Coverage is well below 0.95 because a collapsed estimate keeps its (degenerate) upper bound: the all -1 draw
    # (probability 0.6**S) gives [-inf, -1], which excludes -0.2. Generous bounds, not a nominal-coverage claim.
    assert coverage >= min_coverage
    assert coverage == pytest.approx(exact, abs=0.04)  # the Monte Carlo agrees with the exact enumeration
    # all-same draws (all +1: 0.4**S, all -1: 0.6**S) are unbounded below
    assert unbounded == pytest.approx(0.4**n_seeds + 0.6**n_seeds, abs=0.04)


@pytest.mark.parametrize(("n_examples", "noise"), [(50, 0.03), (100, 0.01)])
def test_monte_carlo_near_discrete_design_five_seeds_false_pass_rate_and_coverage(n_examples, noise):
    false_pass, coverage, unbounded = _monte_carlo(
        _near_discrete_replicate, true_effect=-0.02, rng_seed=20260611, n_seeds=5, n_examples=n_examples, noise=noise
    )
    print(
        f"near-discrete design S=5 N={n_examples} noise={noise}: false-pass {false_pass:.4f}, "
        f"coverage of -0.02 {coverage:.4f}, unbounded below {unbounded:.4f}"
    )
    assert false_pass <= 0.025  # measured about 0.01 (the 0.4**5 = 1% all-favourable draws)
    assert coverage >= 0.85  # measured about 0.91 to 0.92


@pytest.mark.parametrize(("n_examples", "noise"), [(50, 0.03), (100, 0.01)])
def test_monte_carlo_near_discrete_design_three_seeds_documented_residual_false_pass_rate(n_examples, noise):
    """DOCUMENTED RESIDUAL LIMITATION, not nominal. With three seeds the B1 guard only catches an exactly collapsed
    seed variance. In a near-discrete design the extra per-example noise makes the seed means differ slightly, so the
    0.4 ** 3 = 6.4% of draws in which all three seeds are favourable still give a bounded interval above min_effect
    (the effect is -0.02, so every one of those is a false pass). Measured with this design and fixed RNG
    (2000 replicates): N=50, noise 0.03: about 0.028; N=100, noise 0.01: about 0.055, i.e. ABOVE 0.05 (the nominal
    one-sided rate is 0.025). The assertion below is an honest upper bound on the measurement, deliberately not the
    nominal rate; coverage of the true effect is likewise only bounded generously."""
    false_pass, coverage, unbounded = _monte_carlo(
        _near_discrete_replicate, true_effect=-0.02, rng_seed=20260611, n_seeds=3, n_examples=n_examples, noise=noise
    )
    print(
        f"near-discrete design S=3 N={n_examples} noise={noise}: false-pass {false_pass:.4f}, "
        f"coverage of -0.02 {coverage:.4f}, unbounded below {unbounded:.4f}"
    )
    assert false_pass <= 0.08  # measured 0.028 (N=50) and 0.055 (N=100); the all-favourable share is 0.064
    assert coverage >= 0.65  # measured about 0.84 (N=50) and 0.74 (N=100)


# --------------------------------------------------------------------------------------------------
# 11. Two seed clusters: never a pass
# --------------------------------------------------------------------------------------------------


def _planted_effect_cells(rng, seed_labels, n_examples, base_rate=0.5, gains=(0.30, 0.35, 0.40)):
    """Per-seed candidate rates base_rate + gain[s] over baseline rate base_rate, a large, seed-varying planted effect."""
    base = [[rng.random() < base_rate for _ in range(n_examples)] for _ in seed_labels]
    cand = [[rng.random() < base_rate + gains[s] for _ in range(n_examples)] for s, _ in enumerate(seed_labels)]
    return base, cand


def _two_seed_datasets():
    rng = random.Random(31)
    datasets = {"all-positive": _discrete_cells([1, 1], 60), "all-negative": _discrete_cells([-1, -1], 60)}
    datasets["identical-arms"] = ([0.0] * 120, ["s0"] * 60 + ["s1"] * 60, [f"e{i % 60}" for i in range(120)])
    big = [float(rng.choice((0, 1, 1, 1))) for _ in range(120)]  # a large planted effect (mean about 0.75)
    datasets["large-effect"] = (big, ["s0"] * 60 + ["s1"] * 60, [f"e{i % 60}" for i in range(120)])
    huge = [1.0 if k % 7 else 0.0 for k in range(120)]
    datasets["huge-seed-varying-effect"] = (huge, ["s0"] * 60 + ["s1"] * 60, [f"e{i % 60}" for i in range(120)])
    for k in range(5):
        noisy = [rng.gauss(0.4 * (s == "s1") + 0.2, 0.5) for s in ["s0"] * 60 + ["s1"] * 60]
        datasets[f"gaussian-{k}"] = (noisy, ["s0"] * 60 + ["s1"] * 60, [f"e{i % 60}" for i in range(120)])
    return datasets


@pytest.mark.parametrize("name", sorted(_two_seed_datasets()))
def test_two_seeds_are_unbounded_below_whatever_the_data(name):
    diffs, seeds, examples = _two_seed_datasets()[name]
    got = two_way_paired_difference(diffs, seeds, examples)
    assert got["seeds"] == 2 and got["seed_variance_estimated"] is True
    assert got["ci_low"] == -math.inf and got["unbounded_reason"] == "too-few-seed-clusters"  # also over "collapsed"
    assert got["se"] == got["se"] and got["ci_high"] < math.inf  # estimates and the upper bound are still reported
    assert got["df"] is None or got["df"] > 0
    # require_seed_variance changes nothing with two seeds
    assert two_way_paired_difference(diffs, seeds, examples, require_seed_variance=True) == got


def test_two_seeds_with_a_large_planted_effect_are_never_protocol_passed():
    plan = fixtures.accuracy_plan(seeds=(11, 12))
    seeds2 = ("0", "1")
    rng = random.Random(8)
    n = 400
    base, cand = _planted_effect_cells(rng, seeds2, n, gains=(0.30, 0.40))
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: base[s][i], seeds2, n),
        "candidate": _accuracy_per_unit(lambda s, i: cand[s][i], seeds2, n),
    }
    result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    eff = result["effect"]
    assert eff["estimate"] == pytest.approx(0.35, abs=0.06)  # the effect really is large
    assert eff["seeds"] == 2 and eff["ci_low"] == -math.inf and eff["unbounded_reason"] == "too-few-seed-clusters"
    assert result["outcome"] == "inconclusive" and eff["ci_high"] > 0.02


def test_two_seeds_with_a_large_planted_effect_through_the_assessor_are_not_a_pass(run_assessor):
    seeds2 = ("0", "1")
    n = 400
    base, cand = _planted_effect_cells(random.Random(8), seeds2, n, gains=(0.30, 0.40))
    evidence = _classification_evidence(
        fixtures.accuracy_plan(seeds=(11, 12)), lambda s, i: base[s][i], lambda s, i: cand[s][i], seeds=seeds2, n=n
    )
    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["clusters"] == 2 and res.receipt["units"] == 800
    assert res.receipt["metrics"]["estimate"] > 0.25 and res.receipt["metrics"]["ci_low"] is None
    evaluation = res.json("evaluation")
    assert evaluation["effect"]["unbounded_reason"] == "too-few-seed-clusters"
    assert evaluation["unbounded_fields"] == ["effect.ci_low"]


def test_three_seeds_with_a_large_seed_varying_planted_effect_still_pass(run_assessor):
    seeds3 = SEEDS3
    n = 400
    base, cand = _planted_effect_cells(random.Random(8), seeds3, n, gains=(0.30, 0.35, 0.40))
    plan = fixtures.accuracy_plan()
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: base[s][i], seeds3, n),
        "candidate": _accuracy_per_unit(lambda s, i: cand[s][i], seeds3, n),
    }
    result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    eff = result["effect"]
    assert eff["seeds"] == 3 and eff["seed_variance_estimated"] is True and "unbounded_reason" not in eff
    assert eff["variance_components"]["seed"] > 0  # the seed effect really varies
    assert result["outcome"] == "protocol-passed" and eff["ci_low"] >= 0.02
    assert eff["estimate"] == pytest.approx(0.35, abs=0.05)

    evidence = _classification_evidence(plan, lambda s, i: base[s][i], lambda s, i: cand[s][i], seeds=seeds3, n=n)
    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "protocol-passed" and res.receipt["clusters"] == 3
    assert res.receipt["metrics"]["ci_low"] >= 0.02
    assert "unbounded_reason" not in res.json("evaluation")["effect"]
    assert res.json("evaluation")["unbounded_fields"] == []


# --------------------------------------------------------------------------------------------------
# 12. Identical arms
# --------------------------------------------------------------------------------------------------


def test_identical_arms_with_three_seeds_are_protocol_failed():
    plan = fixtures.accuracy_plan()
    rng = random.Random(4)
    table = [[rng.random() < 0.6 for _ in range(100)] for _ in SEEDS3]
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: table[s][i]),
        "candidate": _accuracy_per_unit(lambda s, i: table[s][i]),
    }
    result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    eff = result["effect"]
    assert result["outcome"] == "protocol-failed"  # ci_high = 0 < min_effect 0.02: collapse never turns this into a pass
    assert eff["estimate"] == 0.0 and eff["ci_high"] == 0.0 and eff["ci_low"] == -math.inf
    assert eff["se"] == 0.0 and eff["seeds"] == 3
    # The lab asked for "variance-collapsed". With >= 2 seeds the seed variance is checked first, and a zero total
    # variance (V_seed + V_example, both non-negative) implies a zero seed component, so the code reports
    # "seed-variance-collapsed". "variance-collapsed" alone is reachable only for a single seed (next test).
    assert eff["unbounded_reason"] == "seed-variance-collapsed"


def test_identical_arms_through_the_assessor_are_protocol_failed(run_assessor):
    evidence = _classification_evidence(
        fixtures.accuracy_plan(), baseline_ok=lambda s, i: i % 2 == 0, candidate_ok=lambda s, i: i % 2 == 0
    )
    res = run_assessor(evidence)
    assert res.receipt["outcome"] == "protocol-failed"
    assert res.receipt["metrics"]["estimate"] == 0.0 and res.receipt["metrics"]["ci_high"] == 0.0
    assert res.receipt["metrics"]["ci_low"] is None
    evaluation = res.json("evaluation")
    assert evaluation["outcome"] == "protocol-failed"
    assert evaluation["effect"]["unbounded_reason"] == "seed-variance-collapsed"
    assert evaluation["unbounded_fields"] == ["effect.ci_low"]


def test_identical_arms_with_one_seed_report_variance_collapsed():
    # One seed (no cluster_by): the seed variance is not estimated, so the total variance is checked, and it is zero.
    diffs, seeds, examples = [0.0] * 100, ["11"] * 100, [str(i) for i in range(100)]
    got = two_way_paired_difference(diffs, seeds, examples)
    assert got["seed_variance_estimated"] is False and got["unbounded_reason"] == "variance-collapsed"
    assert got["ci_low"] == -math.inf and got["ci_high"] == 0.0 and got["se"] == 0.0

    plan = fixtures.accuracy_plan(seeds=(11,), cluster_by=None)
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: i % 2 == 0, ("11",)),
        "candidate": _accuracy_per_unit(lambda s, i: i % 2 == 0, ("11",)),
    }
    result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    assert result["outcome"] == "protocol-failed" and result["effect"]["unbounded_reason"] == "variance-collapsed"
    # a single seed that is +1 everywhere is not a pass either
    per_unit["candidate"] = _accuracy_per_unit(lambda s, i: True, ("11",))
    per_unit["baseline"] = _accuracy_per_unit(lambda s, i: False, ("11",))
    result = evaluate_success_rule(plan, per_unit, interval=INTERVAL_TWO_WAY)
    assert result["outcome"] == "inconclusive" and result["effect"]["unbounded_reason"] == "variance-collapsed"


# --------------------------------------------------------------------------------------------------
# 13. F5 (review B2): the arms must cover the same units
# --------------------------------------------------------------------------------------------------
#
# Silently intersecting the arms let a candidate that lost a subset of units (for instance the ones it got wrong)
# be judged on the units it kept. A mismatch is now inconclusive whatever the interval method says.

INTERVAL_KWARGS = pytest.mark.parametrize(
    "interval_kwargs",
    [{}, {"interval": INTERVAL_SEED_CLUSTERED}, {"interval": INTERVAL_TWO_WAY}],
    ids=["default", "seed-clustered", "two-way"],
)
MISMATCH_REASON = "baseline and candidate units do not match"


def _example_labels(cells):
    """The first five 'cluster/unit' labels in (cluster, unit) string order, as the domain reports them."""
    return [f"{c}/{u}" for c, u in sorted(cells, key=lambda k: (str(k[0]), str(k[1])))[:5]]


def _selective_loss_per_unit(n=100):
    """The candidate is WORSE than the baseline (right on 40% of cells, the baseline on 60%) but its report keeps only
    the units it got right. Returns (full per-unit rows, rows with the candidate's failures dropped, dropped cells)."""
    rng = random.Random(2025)
    base = [[rng.random() < 0.6 for _ in range(n)] for _ in SEEDS3]
    cand = [[rng.random() < 0.4 for _ in range(n)] for _ in SEEDS3]
    full = {
        "baseline": _accuracy_per_unit(lambda s, i: base[s][i], n=n),
        "candidate": _accuracy_per_unit(lambda s, i: cand[s][i], n=n),
    }
    kept = [row for row in full["candidate"] if row["accuracy"] == 1.0]
    dropped = {(row["cluster"], row["unit"]) for row in full["candidate"] if row["accuracy"] == 0.0}
    return full, {"baseline": full["baseline"], "candidate": kept}, dropped


@INTERVAL_KWARGS
def test_a_candidate_that_lost_its_failed_units_is_inconclusive(interval_kwargs):
    plan = fixtures.accuracy_plan()
    full, lossy, dropped = _selective_loss_per_unit()
    assert 100 < len(dropped) < 200 and len(lossy["candidate"]) + len(dropped) == 300

    # the honest comparison: the candidate is worse, so it is not a pass
    assert evaluate_success_rule(plan, full, **interval_kwargs)["outcome"] in ("protocol-failed", "inconclusive")
    # what main computed from the lossy report: the arms intersected, the candidate right on every unit it kept
    kept = {(r["cluster"], r["unit"]) for r in lossy["candidate"]}
    intersected = {"baseline": [r for r in lossy["baseline"] if (r["cluster"], r["unit"]) in kept],
                   "candidate": lossy["candidate"]}
    assert evaluate_success_rule(plan, intersected, **interval_kwargs)["outcome"] == "protocol-passed"

    result = evaluate_success_rule(plan, lossy, **interval_kwargs)
    assert result["outcome"] == "inconclusive"
    assert result["reason"] == MISMATCH_REASON
    assert result["unit_mismatch"] == {
        "only_baseline": len(dropped),
        "only_candidate": 0,
        "examples": _example_labels(dropped),
    }
    assert len(result["unit_mismatch"]["examples"]) == 5
    assert result["effect"]["units"] == 300 - len(dropped)  # the effect is still reported, from the matched units


@INTERVAL_KWARGS
def test_matched_units_report_no_unit_mismatch(interval_kwargs):
    plan = fixtures.accuracy_plan()
    full, lossy, _ = _selective_loss_per_unit()
    matched = evaluate_success_rule(plan, full, **interval_kwargs)
    assert "unit_mismatch" not in matched and "reason" not in matched
    # the row order of an arm does not matter for matching
    shuffled = {role: random.Random(3).sample(rows, len(rows)) for role, rows in full.items()}
    again = evaluate_success_rule(plan, shuffled, **interval_kwargs)
    assert again["outcome"] == matched["outcome"] and "unit_mismatch" not in again
    # a clear pass on matched units stays a pass
    sure = {"baseline": _accuracy_per_unit(lambda s, i: (i + s) % 3 == 0), "candidate": _accuracy_per_unit(lambda s, i: True)}
    result = evaluate_success_rule(plan, sure, **interval_kwargs)
    assert "unit_mismatch" not in result and "reason" not in result
    assert result["outcome"] == "protocol-passed"


@INTERVAL_KWARGS
def test_a_mismatch_overrides_a_protocol_failed_intersection_too(interval_kwargs):
    plan = fixtures.accuracy_plan()
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: True),
        "candidate": _accuracy_per_unit(lambda s, i: False),
    }
    assert evaluate_success_rule(plan, per_unit, **interval_kwargs)["outcome"] == "protocol-failed"
    per_unit["candidate"] = [r for r in per_unit["candidate"] if int(r["unit"]) % 10]  # drops 10 units per seed
    result = evaluate_success_rule(plan, per_unit, **interval_kwargs)
    assert result["outcome"] == "inconclusive" and result["reason"] == MISMATCH_REASON
    assert result["unit_mismatch"]["only_baseline"] == 30 and result["unit_mismatch"]["only_candidate"] == 0
    assert result["unit_mismatch"]["examples"] == ["0/0", "0/10", "0/20", "0/30", "0/40"]


@INTERVAL_KWARGS
def test_unit_mismatch_counts_extra_candidate_units_and_a_missing_seed(interval_kwargs):
    plan = fixtures.accuracy_plan()
    good = {
        "baseline": _accuracy_per_unit(lambda s, i: (i + s) % 3 == 0),
        "candidate": _accuracy_per_unit(lambda s, i: True),
    }
    # the candidate reports units the baseline lacks
    extra = [{"unit": f"x{k}", "cluster": "1", "accuracy": 1.0} for k in range(7)]
    result = evaluate_success_rule(plan, {**good, "candidate": good["candidate"] + extra}, **interval_kwargs)
    assert result["outcome"] == "inconclusive" and result["reason"] == MISMATCH_REASON
    assert result["unit_mismatch"] == {
        "only_baseline": 0,
        "only_candidate": 7,
        "examples": ["1/x0", "1/x1", "1/x2", "1/x3", "1/x4"],
    }
    # one whole seed missing from the candidate
    no_seed = [r for r in good["candidate"] if r["cluster"] != "2"]
    result = evaluate_success_rule(plan, {**good, "candidate": no_seed}, **interval_kwargs)
    assert result["outcome"] == "inconclusive"
    assert result["unit_mismatch"]["only_baseline"] == 100 and result["unit_mismatch"]["only_candidate"] == 0
    assert result["unit_mismatch"]["examples"] == ["2/0", "2/1", "2/10", "2/11", "2/12"]
    # the same unit id under another seed is a different unit: one on each side
    moved = [dict(r, cluster="9") if (r["cluster"], r["unit"]) == ("0", "0") else r for r in good["candidate"]]
    result = evaluate_success_rule(plan, {**good, "candidate": moved}, **interval_kwargs)
    assert result["outcome"] == "inconclusive"
    assert result["unit_mismatch"] == {"only_baseline": 1, "only_candidate": 1, "examples": ["0/0", "9/0"]}


@INTERVAL_KWARGS
def test_arms_with_no_common_unit_are_inconclusive(interval_kwargs):
    plan = fixtures.accuracy_plan()
    per_unit = {
        "baseline": _accuracy_per_unit(lambda s, i: False, ("0", "1", "2")),
        "candidate": _accuracy_per_unit(lambda s, i: True, ("3", "4", "5")),
    }
    result = evaluate_success_rule(plan, per_unit, **interval_kwargs)
    assert result["outcome"] == "inconclusive" and result["effect"] is None


# the assessor, paired-report rows (the classification family refuses a mismatch earlier, with a ContractError)


def _selective_loss_paired_report(plan, lose="candidate"):
    """paired-report rows where the candidate's reference side is wrong on 60% of the pairs and the baseline's on 40%
    (so the candidate is the worse arm). ``lose`` names the arm whose rows for the pairs it got wrong are removed."""
    rng = random.Random(77)
    base = [[rng.random() < 0.6 for _ in range(100)] for _ in SEEDS3]
    cand = [[rng.random() < 0.4 for _ in range(100)] for _ in SEEDS3]
    full = _paired_report_evidence(
        plan, baseline=(lambda s, i: base[s][i], lambda s, i: True), candidate=(lambda s, i: cand[s][i], lambda s, i: True)
    )
    lossy = copy.deepcopy(full)
    wrong = {(r["cluster"], r["pair_id"]) for r in lossy["arms"][lose]["rows"] if r["reference_logit"] < 0}
    lossy["arms"][lose]["rows"] = [r for r in lossy["arms"][lose]["rows"] if (r["cluster"], r["pair_id"]) not in wrong]
    assert 100 < len(wrong) < 200
    return full, lossy, wrong


def test_assessor_paired_report_selective_loss_of_candidate_units_is_inconclusive(run_assessor):
    plan = _reference_accuracy_plan()
    full, lossy, wrong = _selective_loss_paired_report(plan, lose="candidate")

    # the arms covering the same pairs: the candidate is the worse arm, so not a pass
    honest = run_assessor(full)
    assert honest.receipt["outcome"] != "protocol-passed" and honest.receipt["units"] == 300
    assert "unit_mismatch" not in honest.json("evaluation")

    # on the pairs it kept, the candidate is right on every one: what the old code intersected into a pass
    kept = {(r["cluster"], r["pair_id"]) for r in lossy["arms"]["candidate"]["rows"]}
    per_unit = _per_unit(lossy)
    intersected = {role: [r for r in rows if (r["cluster"], r["unit"]) in kept] for role, rows in per_unit.items()}
    assert evaluate_success_rule(plan, intersected, interval=INTERVAL_TWO_WAY)["outcome"] == "protocol-passed"

    res = run_assessor(lossy)  # the assessor run itself succeeds (run_assessor asserts res.ok)
    assert res.receipt["outcome"] == "inconclusive" and res.receipt["outcome"] != "protocol-passed"
    assert res.receipt["units"] == 300 - len(wrong) and res.receipt["clusters"] == 3
    evaluation = res.json("evaluation")
    assert evaluation["outcome"] == "inconclusive" and evaluation["reason"] == MISMATCH_REASON
    assert evaluation["unit_mismatch"] == {
        "only_baseline": len(wrong),
        "only_candidate": 0,
        "examples": _example_labels(wrong),
    }
    assert evaluation["interval"] == "two-way-seed-example-t" and evaluation["metric"] == "reference_accuracy"
    assert evaluation["units_by_role"] == {"baseline": 300, "candidate": 300 - len(wrong)}


def test_assessor_paired_report_loss_of_baseline_units_is_inconclusive_too(run_assessor):
    plan = _reference_accuracy_plan()
    # here the BASELINE is the arm that loses the pairs it got wrong, which would flatter nobody but is still a mismatch
    _, lossy, wrong = _selective_loss_paired_report(plan, lose="baseline")
    res = run_assessor(lossy)
    assert res.receipt["outcome"] == "inconclusive"
    evaluation = res.json("evaluation")
    assert evaluation["unit_mismatch"]["only_baseline"] == 0
    assert evaluation["unit_mismatch"]["only_candidate"] == len(wrong)
    assert evaluation["unit_mismatch"]["examples"] == _example_labels(wrong)


def test_assessor_paired_report_matched_pairs_have_no_unit_mismatch(run_assessor):
    evidence = _paired_report_evidence(
        _reference_accuracy_plan(),
        baseline=(lambda s, i: (i + s) % 3 == 0, lambda s, i: True),
        candidate=(lambda s, i: True, lambda s, i: True),
    )
    res = run_assessor(evidence)
    evaluation = res.json("evaluation")
    assert "unit_mismatch" not in evaluation and "reason" not in evaluation
    assert res.receipt["outcome"] == "protocol-passed" and res.receipt["units"] == 300
