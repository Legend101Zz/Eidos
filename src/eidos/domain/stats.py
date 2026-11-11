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


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _beta_cf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (modified Lentz)."""
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def regularized_beta(x: float, a: float, b: float) -> float:
    """I_x(a, b) for a, b > 0 and 0 <= x <= 1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(a, b, x) / a
    return 1.0 - math.exp(b * math.log1p(-x) + a * math.log(x) - _log_beta(a, b)) * _beta_cf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    if math.isinf(df):
        return 0.5 * math.erfc(-t / math.sqrt(2.0))
    tail = 0.5 * regularized_beta(df / (df + t * t), df / 2.0, 0.5)
    return 1.0 - tail if t >= 0 else tail


def student_t_quantile(p: float, df: float) -> float:
    """Inverse Student t CDF for real df > 0 (df = inf gives the normal quantile); bisection to 1e-12."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    if not df > 0:
        raise ValueError("df must be > 0")
    if math.isinf(df) or df > 1e7:
        return normal_quantile(p)
    if p < 0.5:
        return -student_t_quantile(1.0 - p, df)
    lo, hi = 0.0, max(1.0, normal_quantile(p))
    while student_t_cdf(hi, df) < p:
        lo, hi = hi, hi * 2.0
        if hi > 1e12:
            return math.inf
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * max(1.0, hi):
            break
    return (lo + hi) / 2.0


def _cluster_sum_variance(resid: Sequence[float], groups: Sequence[str]) -> tuple[float, int]:
    """One-way cluster-robust variance of the grand mean, with the G/(G-1) small-sample factor."""
    n = len(resid)
    sums: dict[str, float] = defaultdict(float)
    for r, g in zip(resid, groups, strict=True):
        sums[g] += r
    count = len(sums)
    if count < 2:
        return float("nan"), count
    return (count / (count - 1)) * sum(s * s for s in sums.values()) / (n * n), count


TWO_WAY_METHOD = "two-way-seed-example-t/1"
MIN_SEED_CLUSTERS = 3  # F5 (B1): fewest training seeds for an interval that includes seed variation


def two_way_paired_difference(
    diffs: Sequence[float], seeds: Sequence[str], examples: Sequence[str], confidence: float = 0.95,
    *, require_seed_variance: bool = False,
) -> dict:
    """Mean paired difference with an interval for a crossed (training seed x held-out example) design.

    ``diffs[i]`` is candidate minus baseline for the unit scored by training seed ``seeds[i]`` on held-out
    example ``examples[i]``. Every seed scores the same held-out examples, so seed clusters are correlated
    through the examples and a seed-only clustered interval understates the variance (ADR 0018, F3b).

    Variance: the two-way cluster-robust estimator of Cameron, Gelbach and Miller (2011) *without*
    subtracting the seed-by-example intersection term, i.e. ``V = V_seed + V_example`` where each term is
    the one-way cluster-robust variance of the grand mean (with the G/(G-1) factor). For a balanced design
    these are ``var(seed means)/S`` and ``var(example means over seeds)/N``. Under the additive model
    ``d = mu + a_seed + b_example + e`` their expectation is ``sigma_a^2/S + sigma_b^2/N + 2 sigma_e^2/(SN)``,
    the true variance plus ``sigma_e^2/(SN) >= 0``: biased upward (not a coverage guarantee: ADR 0018 s. 7), never
    negative, and never smaller than
    either one-way variance. Critical value: Student t with Welch-Satterthwaite degrees of freedom from the
    components (S-1 and N-1), so few seeds widen the interval.

    One seed: training-seed variance cannot be estimated (``seed_variance_estimated`` is false) and the
    interval reduces to the plain paired t-interval over examples, unless ``require_seed_variance`` (the
    plan declares ``cluster_by``, i.e. a conclusion across training seeds), when it is unbounded. Fewer than
    two examples: unbounded.
    """
    n = len(diffs)
    if n == 0:
        raise ValueError("no paired units")
    mean = sum(diffs) / n
    resid = [d - mean for d in diffs]
    v_seed, n_seeds = _cluster_sum_variance(resid, seeds)
    v_example, n_examples = _cluster_sum_variance(resid, examples)
    seed_estimated = n_seeds >= 2
    terms: list[tuple[float, int]] = []
    if seed_estimated:
        terms.append((v_seed, n_seeds - 1))
    if n_examples >= 2:
        terms.append((v_example, n_examples - 1))
    else:
        v_example = float("nan")
    var = float("nan") if n_examples < 2 or (require_seed_variance and not seed_estimated) else sum(
        v for v, _ in terms)
    # F5 (review B1): fail closed (unbounded, so the assessor is inconclusive) when the variance estimate collapses
    # or there are too few seed clusters. With bounded outcomes a seed effect can be discrete: if each seed is +1 on
    # every example with p = 0.4 and -1 with p = 0.6 (population effect -0.2), three seeds are all +1 with
    # probability 0.064 and the variance estimate is exactly zero; a point interval [1, 1] would pass any min_effect.
    # No variance-based interval can tell that sample from a population with no seed variation, so zero is never
    # taken at face value. Two seed clusters give the seed component one degree of freedom, so it is zero or
    # near zero far too often; at least MIN_SEED_CLUSTERS are needed when seed variation is part of the interval.
    # The estimates (se, df, components) are still reported; only the interval's lower bound is unbounded.
    tol = (1e-12 * max(1.0, abs(mean))) ** 2
    collapsed = None
    if var == var:
        if seed_estimated and n_seeds < MIN_SEED_CLUSTERS:
            collapsed = "too-few-seed-clusters"
        elif seed_estimated and v_seed <= tol:
            collapsed = "seed-variance-collapsed"
        elif var <= tol:
            collapsed = "variance-collapsed"
    df: float = float("nan")
    if var == var:
        denom = sum(v * v / k for v, k in terms)
        df = var * var / denom if denom > 0 else math.inf
    if var != var or var < 0:
        se, lo, hi = float("nan"), float("-inf"), float("inf")
    else:
        se = math.sqrt(var)
        q = student_t_quantile(0.5 + confidence / 2, df) if se > 0 else 0.0
        # A collapsed estimate never supports a pass: the lower bound is unbounded. The upper bound is kept, so
        # identical arms (every difference zero) still fail a rule that needs an improvement (fail closed: the
        # reviewer's case is a false pass; a collapsed estimate may still fail, never pass).
        lo, hi = (float("-inf") if collapsed else mean - q * se), mean + q * se
    return {
        "estimate": mean, "ci_low": lo, "ci_high": hi, "units": n,
        "independent_units": min(n_seeds, n_examples) if seed_estimated else n_examples,
        "confidence": confidence, "se": se, "method": TWO_WAY_METHOD,
        "seeds": n_seeds, "examples": n_examples, "seed_variance_estimated": seed_estimated,
        "variance_components": {"seed": v_seed if seed_estimated else None,
                                "example": v_example if v_example == v_example else None},
        "df": None if df != df or math.isinf(df) else df,
        **({"unbounded_reason": collapsed} if collapsed else {}),
    }


def proportion(values: Iterable[int]) -> tuple[int, int]:
    vals = list(values)
    return sum(vals), len(vals)
