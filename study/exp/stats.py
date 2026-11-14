"""Paired development statistics, using only numpy and the standard library."""

from __future__ import annotations

import math

import numpy as np


def _vector(values):
    data = np.asarray(list(values), dtype=float)
    if data.ndim != 1 or not len(data) or not np.isfinite(data).all():
        raise ValueError("expected a nonempty finite vector")
    return data


def mean_se(diffs):
    """Return (mean, sample standard error); one observation has unknown SE."""
    data = _vector(diffs)
    return float(data.mean()), float(data.std(ddof=1) / math.sqrt(len(data))) if len(data) > 1 else None


def _signflip_mc(data, alternative="greater", *, draws=100_000, seed=0):
    rng = np.random.default_rng(seed)
    observed = float(data.sum())
    hits = 0
    for start in range(0, draws, 2048):
        signs = rng.integers(0, 2, (min(2048, draws - start), len(data))) * 2 - 1
        sums = np.sum(signs * data, axis=1)
        hits += int(_tail(sums, observed, alternative).sum())
    return hits / draws


def _tail(sums, observed, alternative):
    # Relative tolerance only absorbs roundoff in different addition orders.
    tol = 16 * np.finfo(float).eps * max(float(np.max(np.abs(sums))), abs(observed))
    if alternative == "greater":
        return sums >= observed - tol
    if alternative == "less":
        return sums <= observed + tol
    if alternative == "two-sided":
        return np.abs(sums) >= abs(observed) - tol
    raise ValueError("alternative must be greater, less or two-sided")


def signflip_p(diffs, alternative="greater"):
    """Mean randomisation test, inclusive ties; exact through twenty pairs.

    Zero pairs remain in the mean's denominator. Their duplicate sign assignments
    can be collapsed without changing the randomisation probability.
    """
    data = _vector(diffs)
    _tail(np.array([0.0]), 0.0, alternative)
    if len(data) > 20:
        return _signflip_mc(data, alternative)
    sums = np.array([0.0])
    for value in data[data != 0]:
        sums = np.concatenate((sums + value, sums - value))
    return float(_tail(sums, float(data.sum()), alternative).mean())


def _beta_fraction(a, b, x):
    """Continued fraction for the regularised incomplete beta (Lentz)."""
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 501):
        for aa in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1 + aa * d
            c = 1 + aa / c
            d = 1 / (d if abs(d) > tiny else tiny)
            c = c if abs(c) > tiny else tiny
            step = d * c
            h *= step
        if abs(step - 1) < 2e-14:
            return h
    raise ArithmeticError("incomplete beta did not converge")


def _ibeta(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > (a + 1) / (a + b + 2):
        return 1 - _ibeta(b, a, 1 - x)
    scale = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    return scale * _beta_fraction(a, b, x) / a


def _t_cdf(t, df):
    tail = 0.5 * _ibeta(df / 2, 0.5, df / (df + t * t))
    return 1 - tail if t >= 0 else tail


def mean_lower_bound(diffs, level=0.90):
    """One-sided Student t bound. Fewer than two pairs returns None."""
    data = _vector(diffs)
    if not 0.5 < level < 1:
        raise ValueError("level must lie between 0.5 and 1")
    mean, se = mean_se(data)
    if se is None:
        return None
    lo, hi = 0.0, 1.0
    while _t_cdf(hi, len(data) - 1) < level:
        hi *= 2
    for _ in range(80):
        mid = (lo + hi) / 2
        if _t_cdf(mid, len(data) - 1) < level:
            lo = mid
        else:
            hi = mid
    return mean - (lo + hi) / 2 * se


def geomean(values):
    data = _vector(values)
    if np.any(data <= 0):
        raise ValueError("geometric mean requires positive values")
    return float(np.exp(np.log(data).mean()))


def paired_rows_delta(pred_a, pred_b, y, metric, *, draws=1000, seed=0):
    """Return (a's improvement over b, paired SE) on aligned test rows."""
    a, b, labels = (np.asarray(v) for v in (pred_a, pred_b, y))
    if a.ndim != 1 or a.shape != b.shape or a.shape != labels.shape or not a.size:
        raise ValueError("predictions and labels must be equal nonempty row vectors")
    if metric == "accuracy":
        return mean_se((a == labels).astype(float) - (b == labels).astype(float))
    if metric != "rmse":
        raise ValueError("metric must be accuracy or rmse")
    a, b, labels = (_vector(v) for v in (a, b, labels))
    if not isinstance(draws, int) or draws < 2:
        raise ValueError("at least two bootstrap draws required")
    ea, eb = (a - labels) ** 2, (b - labels) ** 2
    delta = float(np.sqrt(eb.mean()) - np.sqrt(ea.mean()))
    rng = np.random.default_rng(seed)
    boot = []
    for _ in range(draws):
        rows = rng.integers(0, len(a), len(a))
        boot.append(np.sqrt(eb[rows].mean()) - np.sqrt(ea[rows].mean()))
    return delta, float(np.std(boot, ddof=1)) if len(a) > 1 else None
