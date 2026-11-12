"""Trusted generator registry; oracle information never enters task bundles."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import rotate
from sklearn import datasets
from sklearn.model_selection import train_test_split


@dataclass
class Instance:
    splits: dict
    task_type: str
    oracle: dict


def registry():
    return json.loads(Path(__file__).with_name('registry.json').read_text())


def generate(entry, instance):
    if not 0 <= instance < entry['instances']:
        raise ValueError('instance outside frozen manifest')
    seed = entry['seed_stream'] + instance
    rng = np.random.default_rng(seed)
    sizes, mechanism = entry['sizes'], entry['mechanism']
    n = sum(sizes.values())
    kind = entry['task_type']
    groups = None
    informative = None
    if mechanism == 'moons-rotated':
        z, y = datasets.make_moons(n_samples=n, noise=0.12, random_state=seed)
        rotation, _ = np.linalg.qr(rng.normal(size=(2, 2)))
        x = np.c_[z @ rotation, rng.normal(size=(n, 10))]
        informative = [0, 1]
    elif mechanism == 'circles-quantiles':
        if instance % 2:
            x, y = datasets.make_gaussian_quantiles(n_samples=n, n_features=2, n_classes=2, random_state=seed)
        else:
            x, y = datasets.make_circles(n_samples=n, factor=0.25, noise=0.025, random_state=seed)
        informative = [0, 1]
    elif mechanism == 'hastie-radial':
        x, y = datasets.make_hastie_10_2(n_samples=n, random_state=seed)
        y = (y > 0).astype(int)
    elif mechanism == 'friedman-two-three':
        factory = datasets.make_friedman3 if instance % 2 else datasets.make_friedman2
        x, y = factory(n_samples=n, noise=0.05 if instance % 2 else 1.0, random_state=seed)
    elif mechanism == 'lowrank-sparse':
        if instance % 2:
            x, y = datasets.make_sparse_uncorrelated(n_samples=n, n_features=20, random_state=seed)
            informative = list(range(4))
        else:
            x, y = datasets.make_regression(n_samples=n, n_features=20, n_informative=8,
                                            effective_rank=5, tail_strength=0.2, noise=0.05, random_state=seed)
    elif mechanism == 'student-curved':
        y = rng.integers(0, 2, n)
        t = rng.uniform(-2, 2, n)
        x = np.c_[t, t * t + 1.8 * (2 * y - 1)] + 0.3 * rng.standard_t(3, (n, 2))
        x = np.c_[x, rng.standard_t(3, (n, 8))]
        informative = [0, 1]
    elif mechanism == 'periodic-heteroskedastic':
        x = rng.uniform(-2, 2, (n, 10))
        y = np.sin(3 * x[:, 0]) + np.cos(5 * x[:, 1]) + rng.normal(size=n) * (0.05 + 0.3 * abs(x[:, 2]))
        informative = [0, 1, 2]
    elif mechanism == 'wine-nuisance':
        z, y = datasets.load_wine(return_X_y=True)
        x = np.c_[z, rng.normal(size=(n, 12))]
        informative = list(range(z.shape[1]))
    elif mechanism in ('digits-rotation', 'offset-reenactment'):
        z, y = datasets.load_digits(return_X_y=True)
        x = z / 16.0
    elif mechanism == 'imbalanced-grouped':
        groups = np.repeat(np.arange(n // 10), 10)
        category = rng.integers(0, 48, n)
        latent = rng.normal(size=n)
        x = np.c_[latent, np.eye(48)[category], rng.normal(size=(n, 3))]
        y = (latent + rng.normal(0, 0.4, n) > 2).astype(int)
        informative = [0]
    elif mechanism in ('large-sample', 'mixture-reenactment'):
        x, y = datasets.make_classification(n_samples=n, n_features=30 if mechanism == 'large-sample' else 20,
                                            n_informative=8, n_redundant=4, n_clusters_per_class=3,
                                            class_sep=0.8, flip_y=0.02, random_state=seed)
    elif mechanism in ('noise-control', 'weak-control'):
        x = rng.normal(size=(n, 12))
        # The weak control's Bayes accuracy is .51: half the .02 practical bar above chance.
        probability = np.full(n, 0.5) if mechanism == 'noise-control' else 0.5 + 0.01 * np.sign(x[:, 0])
        y = (rng.random(n) < probability).astype(int)
        informative = [] if mechanism == 'noise-control' else [0]
    else:
        raise ValueError('unregistered mechanism')
    indices = np.arange(n)
    if groups is not None:
        order = rng.permutation(np.unique(groups))
        boundaries = [sizes['train'] // 10, (sizes['train'] + sizes['validation']) // 10]
        partitions = [indices[np.isin(groups, g)] for g in np.split(order, boundaries)]
    else:
        train, rest = train_test_split(indices, train_size=sizes['train'], random_state=seed,
                                       stratify=y if kind == 'classification' else None)
        validation, test = train_test_split(rest, train_size=sizes['validation'], random_state=seed + 1,
                                             stratify=y[rest] if kind == 'classification' else None)
        partitions = [train, validation, test]
    ids = np.array([int.from_bytes(hashlib.sha256(f'practice:{seed}:{i}'.encode()).digest()[:8], 'big')
                    & ((1 << 63) - 1) for i in indices], dtype=np.int64)
    splits = {}
    for name, rows in zip(('train', 'validation', 'test'), partitions, strict=False):
        values = np.asarray(x[rows], dtype=np.float32)
        if mechanism == 'digits-rotation' and name != 'train':
            values = rotate(values.reshape(-1, 8, 8), 18, axes=(1, 2), reshape=False, order=1).reshape(-1, 64)
        if mechanism == 'offset-reenactment' and name != 'train':
            values = values + 0.35
        splits[name] = dict(x=values, y=np.asarray(y[rows], dtype=np.int64 if kind == 'classification' else np.float32), ids=ids[rows])
        if groups is not None:
            splits[name]['groups'] = groups[rows].astype(np.int64)
    return Instance(splits, kind, dict(oracle_mechanism=mechanism, oracle_informative_columns=informative,
                                     oracle_true_gain=0.0 if mechanism == 'noise-control' else 0.01 if mechanism == 'weak-control' else None,
                                     oracle_reference_score=0.5 if mechanism == 'noise-control' else 0.51 if mechanism == 'weak-control' else None,
                                     reference_status='analytic Bayes accuracy' if mechanism.endswith('control') else 'not yet estimated; no large-sample reference claim'))
