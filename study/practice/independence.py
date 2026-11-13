"""Execute family factories and compare actual recipes with the seven public dev cards.

Sample count, feature padding and random seed never make a dev recipe independent.
Custom D04 has no sklearn factory: test its conditioning/linear-response fingerprint.
"""
from unittest.mock import patch

import numpy as np
from sklearn import datasets

from study.practice.generators import generate, registry

DEV_RECIPES = {
    'N01': ('load_breast_cancer', {}),
    'N02': ('load_diabetes', {}),
    'N03': ('make_friedman1', {}),
    'N04': ('make_classification', dict(n_informative=8, n_redundant=4,
                                     n_clusters_per_class=3, class_sep=.8, flip_y=.02)),
    'D02': ('load_digits', {'shift': 0}),
    'D03': ('load_digits', {'shift': .35}),
    'D04': ('custom-conditioned-linear', {'dimensions': 6, 'scale_ratio': 100000}),
}


def observed(name, original, calls):
    def call(*args, **settings):
        result = original(*args, **settings)
        calls.append((name, settings.copy(), result))
        return result
    return call


def n04_fingerprint(data):
    """Statistical screen for a redundant per-class mixture with Gaussian noise.

    Feature order, shifts, scaling, sample count and small changes in separation /
    label flips do not change this signature. This is a conservative dev-similarity
    lint, not a proof that arbitrary transformed data have an independent origin.
    """
    from scipy.stats import kurtosis, skew
    x = np.concatenate([s['x'] for s in data.splits.values()]).astype(float)
    y = np.concatenate([s['y'] for s in data.splits.values()])
    if data.task_type != 'classification' or len(np.unique(y)) != 2 or x.shape[1] < 16 or len(x) < 200:
        return False
    x = x[:, x.std(axis=0) > 1e-8]
    x = (x - x.mean(axis=0)) / x.std(axis=0)
    correlation = np.corrcoef(x.T)
    rank_deficit = int(np.sum(np.linalg.eigvalsh(correlation) < 1e-7))
    np.fill_diagonal(correlation, 0.)
    noise = (np.max(abs(correlation), axis=0) < .18) & (abs(skew(x)) < .35) & (abs(kurtosis(x)) < .7)
    # Class-conditional mean/variance/kurtosis distinguish the dependent signal
    # from the marginally Gaussian independent nuisance block.
    moments = []
    for label in np.unique(y):
        rows = x[y == label]
        moments.append(abs(rows.mean(axis=0)) + abs(rows.var(axis=0) - 1.) + abs(kurtosis(rows)))
    signal_moment = np.median(np.mean(moments, axis=0)[~noise]) if np.any(~noise) else 0.
    return bool(rank_deficit >= 3 and noise.sum() >= 4 and signal_moment > .25)


def recipe_violations():
    violations = []
    factories = [name for name in dir(datasets) if name.startswith(('make_', 'load_')) and callable(getattr(datasets, name))]
    for entry in registry():
        if entry['role'] == 'calibration-only':
            continue
        calls = []
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in factories:
                stack.enter_context(patch.object(datasets, name, observed(name, getattr(datasets, name), calls)))
            data = generate(entry, 0)
        if n04_fingerprint(data):
            violations.append((entry['id'], 'N04-statistical-fingerprint'))
        for card, (factory, parameters) in DEV_RECIPES.items():
            for name, kwargs, result in calls:
                if name != factory:
                    continue
                if card in ('N01', 'N02', 'N03') or (card == 'N04' and all(kwargs.get(k) == v for k, v in parameters.items())):
                    violations.append((entry['id'], card))
                if card in ('D02', 'D03'):
                    source = result[0] if isinstance(result, tuple) else result.data
                    rows = {tuple(row.astype(np.float32)) for row in source / 16 + parameters['shift']}
                    held = data.splits['validation']['x']
                    if held.shape[1] == 64 and sum(tuple(row) in rows for row in held) > .9 * len(held):
                        violations.append((entry['id'], card))
            if card == 'D04':
                x, y = data.splits['train']['x'], data.splits['train']['y']
                if x.shape[1] == 6:
                    sd = x.std(axis=0)
                    prediction = np.c_[x / sd, np.ones(len(x))] @ np.linalg.lstsq(np.c_[x / sd, np.ones(len(x))], y, rcond=None)[0]
                    if sd.max() / max(sd.min(), 1e-12) > 10000 and np.var(y - prediction) < .01 * np.var(y):
                        violations.append((entry['id'], card))
    return violations
