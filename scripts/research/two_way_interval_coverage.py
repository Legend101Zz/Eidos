"""Monte Carlo coverage of the 95% paired-difference interval: seed-only (paired-ci 1.0.0/1.1.0) versus two-way
(training seed x held-out example, paired-ci 1.2.0). ADR 0018 section 6.

Run with the repository sources on PYTHONPATH::

    python scripts/research/two_way_interval_coverage.py
"""

from __future__ import annotations

import random
from collections.abc import Callable

from eidos.domain.stats import paired_difference, two_way_paired_difference

Design = Callable[[random.Random, int, int], tuple[list[float], list[str], list[str], float]]


def simulate(name: str, design: Design, n_seeds: int, n_examples: int, reps: int = 400, seed: int = 20261111) -> None:
    rng = random.Random(seed)
    covered = {"seed-only": 0, "two-way": 0}
    width = {"seed-only": 0.0, "two-way": 0.0}
    for _ in range(reps):
        d, seeds, examples, mu = design(rng, n_seeds, n_examples)
        for key, r in (("seed-only", paired_difference(d, seeds)), ("two-way", two_way_paired_difference(d, seeds, examples))):
            covered[key] += r["ci_low"] <= mu <= r["ci_high"]
            width[key] += (r["ci_high"] - r["ci_low"]) / reps
    print(
        f"{name:54s} S={n_seeds} N={n_examples}: coverage seed-only {covered['seed-only'] / reps:.3f} "
        f"two-way {covered['two-way'] / reps:.3f} | mean width {width['seed-only']:.4f} vs {width['two-way']:.4f}"
    )


def additive(seed_sd: float, example_sd: float, noise_sd: float) -> Design:
    """d = 0.1 + a_seed + b_example + e: every seed scores the same examples (crossed design)."""

    def design(rng: random.Random, n_seeds: int, n_examples: int):
        a = [rng.gauss(0, seed_sd) for _ in range(n_seeds)]
        b = [rng.gauss(0, example_sd) for _ in range(n_examples)]
        d, seeds, examples = [], [], []
        for i in range(n_seeds):
            for j in range(n_examples):
                d.append(0.1 + a[i] + b[j] + rng.gauss(0, noise_sd))
                seeds.append(str(i))
                examples.append(str(j))
        return d, seeds, examples, 0.1

    return design


def binary(p_base: float, gain: float, fixable: float, seed_sd: float) -> Design:
    """Binary correctness. A fraction ``fixable`` of examples (drawn once, shared by all seeds) is where the
    candidate can gain; each seed's candidate gets a seed-specific shift there. Target: the expected difference."""

    def design(rng: random.Random, n_seeds: int, n_examples: int):
        can_fix = [rng.random() < fixable for _ in range(n_examples)]
        d, seeds, examples = [], [], []
        for i in range(n_seeds):
            shift = rng.gauss(0, seed_sd)
            for j in range(n_examples):
                p_cand = min(1.0, max(0.0, p_base + (gain / fixable + shift if can_fix[j] else 0.0)))
                d.append(float(rng.random() < p_cand) - float(rng.random() < p_base))
                seeds.append(str(i))
                examples.append(str(j))
        return d, seeds, examples, fixable * (min(1.0, p_base + gain / fixable) - p_base)

    return design


def discrete_seed(p_fav: float) -> Design:
    """F5 (review B1): a discrete seed effect. Each seed is +1 on every example with probability ``p_fav`` and -1
    otherwise (population effect 2 p_fav - 1). All seeds favourable is a sample with zero variance."""

    def design(rng: random.Random, n_seeds: int, n_examples: int):
        d, seeds, examples = [], [], []
        for i in range(n_seeds):
            a = 1.0 if rng.random() < p_fav else -1.0
            for j in range(n_examples):
                d.append(a)
                seeds.append(str(i))
                examples.append(str(j))
        return d, seeds, examples, 2 * p_fav - 1

    return design


def near_discrete_seed(p_fav: float, frac: float) -> Design:
    """F5 (review B1): a seed is favourable with probability ``p_fav`` (the candidate gains on a random ``frac`` of
    the examples, drawn per seed) and otherwise loses on a random ``frac``. Binary correctness differences, so the
    seed variance is small but not zero when every seed is favourable."""

    def design(rng: random.Random, n_seeds: int, n_examples: int):
        d, seeds, examples = [], [], []
        for i in range(n_seeds):
            sign = 1.0 if rng.random() < p_fav else -1.0
            for j in range(n_examples):
                d.append(sign if rng.random() < frac else 0.0)
                seeds.append(str(i))
                examples.append(str(j))
        return d, seeds, examples, (2 * p_fav - 1) * frac

    return design


def false_pass(name: str, design: Design, n_seeds: int, n_examples: int, min_effect: float, reps: int = 4000,
               seed: int = 20261111) -> None:
    """Coverage of the two-way interval and its false-pass rate: ci_low >= min_effect while the true effect is
    below min_effect (the assessor would report protocol-passed)."""
    rng = random.Random(seed)
    covered = passes = unbounded = 0
    for _ in range(reps):
        d, seeds, examples, mu = design(rng, n_seeds, n_examples)
        r = two_way_paired_difference(d, seeds, examples, require_seed_variance=True)
        covered += r["ci_low"] <= mu <= r["ci_high"]
        passes += r["ci_low"] >= min_effect
        unbounded += "unbounded_reason" in r
    print(f"{name:54s} S={n_seeds} N={n_examples}: coverage {covered / reps:.3f}, false pass "
          f"{passes / reps:.4f} (min_effect {min_effect}, true effect {mu:+.3f}), fail-closed {unbounded / reps:.3f}")


def main() -> None:
    for s, n in ((3, 100), (5, 200), (10, 200)):
        simulate("additive (seed sd .02, example sd .3, interaction .3)", additive(0.02, 0.3, 0.3), s, n)
    for s, n in ((3, 100), (5, 200)):
        simulate("additive, no example effect (seed .05, interaction .3)", additive(0.05, 0.0, 0.3), s, n)
    for s, n in ((3, 100), (3, 400), (5, 400)):
        simulate("binary: gain on a fixed 5% of shared examples", binary(0.3, 0.03, 0.05, 0.02), s, n)
    # F5 (review B1): difficult discrete, few-seed designs (population effect below min_effect: any pass is false)
    for s, n in ((3, 100), (5, 100), (10, 100)):
        false_pass("discrete seed effect +-1 (p_fav 0.4, effect -0.2)", discrete_seed(0.4), s, n, 0.02)
    for s, n in ((3, 100), (5, 100), (10, 100)):
        false_pass("near-discrete: +-1 on 10% of examples (p_fav 0.4)", near_discrete_seed(0.4, 0.1), s, n, 0.02)
    for s, n in ((3, 400), (5, 400)):
        false_pass("near-discrete: +-1 on 10% of examples (p_fav 0.45)", near_discrete_seed(0.45, 0.1), s, n, 0.0)


if __name__ == "__main__":
    main()
