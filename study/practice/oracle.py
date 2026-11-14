"""Controller-only scoring. No oracle value participates in recipe selection."""
import numpy as np


def score(task_type, truth, predictions):
    truth, predictions = np.asarray(truth), np.asarray(predictions)
    if truth.shape != predictions.shape or truth.ndim != 1 or not np.isfinite(predictions).all():
        raise ValueError('predictions must be a finite vector matching truth')
    if task_type == 'classification':
        return float(np.mean(truth == predictions))
    return -float(np.sqrt(np.mean((truth.astype(float) - predictions.astype(float)) ** 2)))


def score_se(task_type, truth, predictions):
    n = len(truth)
    if n < 2:
        raise ValueError('at least two rows required')
    if task_type == 'classification':
        p = score(task_type, truth, predictions)
        return float(np.sqrt(p * (1 - p) / n))
    errors = (np.asarray(truth, dtype=float) - predictions) ** 2
    rmse = max(float(np.sqrt(errors.mean())), 1e-12)
    return float(errors.std(ddof=1) / np.sqrt(n) / (2 * rmse))


def paired_interval(task_type, truth, candidate, baseline):
    if task_type == 'classification':
        differences = (np.asarray(candidate) == truth).astype(float) - (np.asarray(baseline) == truth).astype(float)
        effect = float(differences.mean())
        se = float(differences.std(ddof=1) / np.sqrt(len(differences)))
    else:
        # Delta-method interval for the difference in RMSE, retaining paired covariance.
        a, b = (truth - candidate) ** 2, (truth - baseline) ** 2
        ra, rb = max(float(np.sqrt(a.mean())), 1e-12), max(float(np.sqrt(b.mean())), 1e-12)
        effect = rb - ra
        influence = (b - b.mean()) / (2 * rb) - (a - a.mean()) / (2 * ra)
        se = float(influence.std(ddof=1) / np.sqrt(len(truth)))
    return dict(effect=effect, se=se, lower95=effect - 1.959963984540054 * se,
                upper95=effect + 1.959963984540054 * se)
