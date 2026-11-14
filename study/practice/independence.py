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
    """Conservative rank-mixture lint, including jittered/rotated dev copies.

    Gaussian ranks remove strictly monotone marginal transforms. Independent
    rank shadows identify nuisance columns without requiring exact redundancy.
    A second all-feature projection detects mixtures hidden by rotations. Neither
    path assumes a particular number of redundant columns or exact matrix rank.
    """
    from scipy.stats import kurtosis, norm, rankdata, skew
    from sklearn.decomposition import PCA
    from sklearn.mixture import GaussianMixture
    from threadpoolctl import threadpool_limits

    # Lint uses visible rows only; factory review must not expose held labels.
    visible = [data.splits[name] for name in ('train', 'validation') if name in data.splits]
    x = np.concatenate([s['x'] for s in visible]).astype(float)
    y = np.concatenate([s['y'] for s in visible])
    if data.task_type != 'classification' or len(np.unique(y)) != 2 or x.shape[1] < 16 or len(x) < 200:
        return False
    x = x[:, x.std(axis=0) > 1e-8]
    standardized = (x - x.mean(axis=0)) / x.std(axis=0)
    correlation = np.corrcoef(standardized.T)
    rank_deficit = int(np.sum(np.linalg.eigvalsh(correlation) < 1e-7))
    np.fill_diagonal(correlation, 0.)
    gaussian_shadow = ((np.max(abs(correlation), axis=0) < .18)
                       & (abs(skew(standardized)) < .35) & (abs(kurtosis(standardized)) < .7))
    moments = [abs(standardized[y == label].mean(axis=0)) + abs(standardized[y == label].var(axis=0) - 1.)
               + abs(kurtosis(standardized[y == label])) for label in np.unique(y)]
    signal_moment = np.median(np.mean(moments, axis=0)[~gaussian_shadow]) if np.any(~gaussian_shadow) else 0.
    # Retain the earlier exact-redundancy signature too, including its one-cluster
    # sensitivity. The transformed-copy paths below do not depend on redundancy.
    if rank_deficit >= 3 and gaussian_shadow.sum() >= 4 and signal_moment > .25:
        return True
    # One-hot/grouped generators are not this continuous Gaussian-mixture recipe.
    if np.mean([len(np.unique(c)) > .5 * len(x) for c in x.T]) < .9:
        return False
    # Bounded deterministic workload, stratified by observed class, no holdout score.
    rng = np.random.default_rng(19834)
    rows = np.concatenate([rng.choice(np.flatnonzero(y == label), min(2000, int(np.sum(y == label))), replace=False)
                           for label in np.unique(y)])
    x, y = x[rows], y[rows]
    z = norm.ppf((rankdata(x, axis=0) - .5) / len(x))
    correlation = np.corrcoef(z.T)
    np.fill_diagonal(correlation, 0.)
    shadow = np.max(abs(correlation), axis=0) < max(.15, 3.5 / np.sqrt(len(x)))

    def mixture_gain(features):
        if features.shape[1] < 2:
            return -np.inf
        projected = PCA(n_components=min(8, features.shape[1]), whiten=True).fit_transform(features)
        gains = []
        for label in np.unique(y):
            values = projected[y == label]
            bic = [GaussianMixture(k, random_state=34, n_init=2, reg_covar=1e-4).fit(values).bic(values) for k in (1, 3)]
            gains.append((bic[0] - bic[1]) / len(values))
        # Shared nonlinear nuisance can itself be multimodal (squares/sines).
        # Require *class-conditional* extra structure beyond that shared density.
        bic = [GaussianMixture(k, random_state=34, n_init=2, reg_covar=1e-4).fit(projected).bic(projected) for k in (1, 3)]
        common_gain = (bic[0] - bic[1]) / len(projected)
        return min(min(gains), min(gains) - common_gain + .3)

    with threadpool_limits(limits=1):
        # Noise-isolated signatures survive marginal quantiles and jitter; the
        # dense projection branch survives orthogonal mixing of every column.
        sparse_mixture = shadow.sum() >= 4 and mixture_gain(z[:, ~shadow]) > .65
        dense_mixture = mixture_gain(z) > .65
    return bool(sparse_mixture or dense_mixture)


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
