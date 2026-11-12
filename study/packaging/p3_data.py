"""Trusted offline pilot-3 generators. Never copy this module into a task bundle."""
import json
from pathlib import Path

import numpy as np
from sklearn.datasets import load_breast_cancer, load_diabetes, make_classification, make_friedman1
from sklearn.model_selection import train_test_split

SEED = 20261111
TASKS = {'N01': 'breast_cancer', 'N02': 'diabetes', 'N03': 'friedman1', 'N04': 'classification'}
CLASSIFICATION = frozenset({'N01', 'N04'})
FROZEN = Path(__file__).with_name('p3_frozen.json')


def dataset(card):
    if card == 'N01':
        x, y = load_breast_cancer(return_X_y=True)
    elif card == 'N02':
        x, y = load_diabetes(return_X_y=True)
    elif card == 'N03':
        x, y = make_friedman1(n_samples=2000, noise=1.0, random_state=SEED)
    elif card == 'N04':
        x, y = make_classification(n_samples=3000, n_features=20, n_informative=8,
                                   n_redundant=4, n_clusters_per_class=3,
                                   class_sep=0.8, flip_y=0.02, random_state=SEED)
    else:
        raise ValueError(f'unknown prospective card: {card}')
    kind = 'classification' if card in CLASSIFICATION else 'regression'
    x = x.astype(np.float32)
    y = y.astype(np.int64 if kind == 'classification' else np.float32)
    ids = np.arange(len(y), dtype=np.int64)
    train, remainder = train_test_split(ids, test_size=0.4, random_state=SEED,
                                       stratify=y if kind == 'classification' else None)
    validation, test = train_test_split(remainder, test_size=0.5, random_state=SEED,
                                      stratify=y[remainder] if kind == 'classification' else None)
    return {s: (x[i], y[i], ids[i]) for s, i in
            [('train', train), ('validation', validation), ('test', test)]}, kind, 2 if kind == 'classification' else 1


def card_spec(card):
    frozen = json.loads(FROZEN.read_text())[card]
    if not frozen['qualified']:
        raise ValueError(f'{card} dropped by prospective qualification')
    return dict(id=card, project=TASKS[card], dataset_seed=SEED, seeds=[11, 23, 37],
                min_reference_gain=frozen['bar'], title='Improve a functioning ' +
                ('tabular classifier' if card in CLASSIFICATION else 'tabular regressor'),
                card_scope='prospective development improvement task',
                permitted_operations=['edit model/trainer', 'train-only preprocessing/augmentation', 'create instruments'])
