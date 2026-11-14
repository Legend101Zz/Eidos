import itertools
import math

import numpy as np
import pytest

from study.exp.stats import (
    _signflip_mc,
    geomean,
    mean_lower_bound,
    mean_se,
    paired_rows_delta,
    signflip_p,
)


@pytest.mark.parametrize("data,p", [([1, 2, 3], 1 / 8), ([1, 1, 1, 1], 1 / 16),
                                    ([1, -1, 0], 3 / 4), ([0, 0, 0], 1)])
def test_exp_signflip_hand(data, p):
    assert signflip_p(data) == p


def test_exp_signflip_symmetry_monotonicity_and_zero_denominator():
    data = np.array([0.2, -0.7, 1.2, 0.4])
    assert signflip_p(data) == signflip_p(-data, "less")
    assert signflip_p(abs(data)) <= signflip_p(data)
    assert mean_se([1, 1, 0])[0] == 2 / 3
    assert signflip_p([1, 1, 0]) == signflip_p([1, 1])
    expected = sum(abs(sum(s * d for s, d in zip(signs, data, strict=True))) >= abs(data.sum()) - 1e-12
                   for signs in itertools.product([-1, 1], repeat=4)) / 16
    assert signflip_p(data, "two-sided") == expected
    assert signflip_p(data * 1e-20) == signflip_p(data)


def test_exp_signflip_exact_mc_and_seed():
    data = np.random.default_rng(17).normal(0.3, 1, 12)
    assert abs(signflip_p(data) - _signflip_mc(data)) < 0.006
    large = np.tile(data, 2)
    assert signflip_p(large) == signflip_p(large)


def test_exp_signflip_aa_rejection_property():
    rng = np.random.default_rng(99)
    rejected = sum(signflip_p(rng.normal(size=12)) <= 0.10 for _ in range(2000))
    assert 0.07 <= rejected / 2000 <= 0.13


def test_exp_mean_se_t_bounds_and_geomean():
    mean, se = mean_se([1, 2, 3])
    assert mean == 2
    assert se == pytest.approx(1 / math.sqrt(3))
    # df=2, one-sided 90% critical value, independently tabulated.
    assert mean_lower_bound([1, 2, 3]) == pytest.approx(2 - 1.885618083164 * se)
    # df=1 is Cauchy, whose inverse has a closed form.
    assert mean_lower_bound([0, 2]) == pytest.approx(1 - math.tan(0.4 * math.pi))
    assert mean_lower_bound([1]) is None
    assert mean_lower_bound([4, 4]) == 4
    assert geomean([1, 4]) == 2


def test_exp_paired_accuracy_and_rmse():
    delta, se = paired_rows_delta([1, 1, 0, 1], [0, 1, 1, 1], [1, 1, 0, 0], "accuracy")
    assert delta == 0.5
    assert se == pytest.approx(math.sqrt(1 / 12))
    a, b, y = [0, 2, 1], [1, 3, 2], [0, 1, 1]
    delta, se = paired_rows_delta(a, b, y, "rmse", seed=3)
    assert delta == pytest.approx(math.sqrt(2) - math.sqrt(1 / 3))
    reverse, reverse_se = paired_rows_delta(b, a, y, "rmse", seed=3)
    assert reverse == -delta
    assert reverse_se == se
    assert paired_rows_delta(a, a, y, "rmse") == (0, 0)


@pytest.mark.parametrize("data", [[], [math.nan], [math.inf]])
def test_exp_stats_refuses_invalid(data):
    with pytest.raises(ValueError):
        signflip_p(data)
    with pytest.raises(ValueError):
        mean_se(data)


def test_exp_stats_bad_shapes_and_arguments():
    with pytest.raises(ValueError):
        paired_rows_delta([1], [1, 2], [1], "accuracy")
    with pytest.raises(ValueError):
        geomean([0, 1])
    with pytest.raises(ValueError):
        signflip_p([1], "bad")


def test_exp_signflip_exact_boundary_and_float_ties():
    assert signflip_p([1] * 20) == 1 / 2**20
    assert signflip_p([0.1] * 8) == 1 / 256
    assert signflip_p([0.3, 0.1, 0.2, -0.1]) == 3 / 16
