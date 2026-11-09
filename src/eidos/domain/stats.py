"""Small, dependency-free statistics used by decision standards and qualification gates."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence


def normal_quantile(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |err| < 1.2e-9)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02,
         -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01,
         -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00,
         4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        return -normal_quantile(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    z = normal_quantile(0.5 + confidence / 2)
    phat = successes / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def paired_difference(
    diffs: Sequence[float], clusters: Sequence[str] | None = None, confidence: float = 0.95
) -> dict:
    """Mean paired difference with a (cluster-robust when clusters given) normal-approximation CI.

    ``diffs[i]`` is candidate minus baseline for analysis unit i. When units are nested in
    clusters (e.g. pairs within an independently trained model), the variance uses cluster sums
    so that thousands of pairs from one model are not treated as independent replications.
    """
    n = len(diffs)
    if n == 0:
        raise ValueError("no paired units")
    mean = sum(diffs) / n
    z = normal_quantile(0.5 + confidence / 2)
    if clusters is not None:
        groups: dict[str, list[float]] = defaultdict(list)
        for d, c in zip(diffs, clusters, strict=True):
            groups[c].append(d)
        g = len(groups)
        if g >= 2:
            resid_sums = [sum(x - mean for x in vals) for vals in groups.values()]
            var = (g / (g - 1)) * sum(s * s for s in resid_sums) / (n * n)
        else:
            var = float("nan")
        effective = g
    else:
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1) / n if n > 1 else float("nan")
        effective = n
    se = math.sqrt(var) if var == var and var >= 0 else float("nan")
    if se != se:
        lo, hi = float("-inf"), float("inf")
    else:
        lo, hi = mean - z * se, mean + z * se
    return {"estimate": mean, "ci_low": lo, "ci_high": hi, "units": n, "independent_units": effective,
            "confidence": confidence, "se": se}


def proportion(values: Iterable[int]) -> tuple[int, int]:
    vals = list(values)
    return sum(vals), len(vals)
