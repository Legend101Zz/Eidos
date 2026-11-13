"""Paired selection bands and intervals, always in task-score units (higher wins)."""
import math

import numpy as np


def score(kind, truth, prediction):
    if kind == "classification":
        return float(np.mean(truth == prediction))
    return -float(np.sqrt(np.mean((truth - prediction) ** 2)))


def score_se(kind, truth, prediction):
    if kind == "classification":
        p = score(kind, truth, prediction)
        return math.sqrt(p * (1 - p) / len(truth))
    sq = (truth - prediction) ** 2
    return float(np.std(sq, ddof=1) / math.sqrt(len(sq)) / (2 * max(math.sqrt(np.mean(sq)), 1e-12)))


def row_scores(kind, truth, prediction):
    if kind == "classification":
        return (truth == prediction).astype(float)
    # Delta-method influence plus the point score. Mean == -RMSE; paired sample
    # SE is the RMSE-difference delta-method SE, not a squared-error surrogate.
    sq = (truth - prediction) ** 2
    rmse = max(float(np.sqrt(sq.mean())), 1e-12)
    return -rmse - (sq - sq.mean()) / (2 * rmse)


def paired_interval(kind, truth, candidate, baseline):
    delta = row_scores(kind, truth, candidate) - row_scores(kind, truth, baseline)
    effect = score(kind, truth, candidate) - score(kind, truth, baseline)
    se = float(delta.std(ddof=1) / math.sqrt(len(delta)))
    return dict(effect=effect, se=se, lower95=effect - 1.959963984540054 * se, upper95=effect + 1.959963984540054 * se)


def choose(rows, *, multiple=1., complexity_order=None):
    good = [r for r in rows if r.get("status", "ok") == "ok"]
    if not good:
        raise ValueError("no completed recipes")
    best = min(good, key=lambda r: (-float(np.mean(r["scores"])), r["id"]))
    values = np.asarray(best["scores"])
    band = []
    for row in good:
        delta = values - row["scores"]
        se = float(delta.std(ddof=1) / math.sqrt(len(delta)))
        if float(delta.mean()) <= multiple * se + 1e-12:
            band.append(row)
    eligible = [r for r in band if r["export_eligible"]]
    if not eligible:
        raise ValueError("no export-eligible recipe in the paired band")
    ranks = complexity_order or {}
    chosen = min(eligible, key=lambda r: (ranks.get(r["id"], r["complexity"]), -float(np.mean(r["scores"])),
                                          float(np.std(r["scores"], ddof=1) / math.sqrt(len(r["scores"]))), r["id"]))
    best_se = float(values.std(ddof=1) / math.sqrt(len(values)))
    return dict(best_id=best["id"], band_ids=[r["id"] for r in band], chosen_id=chosen["id"],
                basis="one-se-choice", multiple=multiple, n_screened=len(good),
                reason="lowest frozen complexity in paired band among export-eligible recipes",
                winners_curse_scale_indep=best_se * math.sqrt(2 * math.log(len(good))),
                winners_curse_interpretation="approximate expected maximum of m independent draws under no true differences; neither an upper bound nor corrected for fold-sharing")


def class_contrasts(rows):
    good = [r for r in rows if r["status"] == "ok"]
    def best(candidates):
        return max(candidates, key=lambda r: np.mean(r["scores"])) if candidates else None
    pairs = [
        ("linear-vs-nonlinear", best([r for r in good if r["class"] == "linear"]), best([r for r in good if r["class"] != "linear"])),
        ("qda-vs-gmm", best([r for r in good if r["spec"]["model"]["family"] == "qda"]), best([r for r in good if r["spec"]["model"]["family"] == "gmm_class"])),
        ("unscreened-vs-screened", best([r for r in good if r["class"] not in ("screen", "lowrank")]), best([r for r in good if r["class"] == "screen"])),
        ("standardized-vs-lowrank", best([r for r in good if r["class"] not in ("screen", "lowrank") and r["spec"]["steps"] == [{"op": "standardize"}]]), best([r for r in good if r["class"] == "lowrank"])),
    ]
    result = []
    for name, a, b in pairs:
        if a and b:
            delta = np.asarray(b["scores"]) - a["scores"]
            mean, se = float(delta.mean()), float(delta.std(ddof=1) / math.sqrt(len(delta)))
            result.append(dict(contrast=name, reference_id=a["id"], candidate_id=b["id"], effect=mean, se=se,
                               lower95=mean - 1.959963984540054 * se, upper95=mean + 1.959963984540054 * se))
    return sorted(result, key=lambda r: (-r["effect"], r["contrast"]))


def bootstrap_interval(kind, truth, candidate, baseline, *, seed=11, draws=2000):
    """Paired percentile bootstrap for a visible baseline comparison only."""
    result = paired_interval(kind, truth, candidate, baseline)
    rng = np.random.default_rng(seed)
    effects = []
    for _ in range(draws):
        take = rng.integers(0, len(truth), len(truth))
        effects.append(score(kind, truth[take], candidate[take]) - score(kind, truth[take], baseline[take]))
    result.update(lower95=float(np.quantile(effects, .025)), upper95=float(np.quantile(effects, .975)),
                  method="paired percentile bootstrap; visible validation", seed=seed, draws=draws)
    return result
