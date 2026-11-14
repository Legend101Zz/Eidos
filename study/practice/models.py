"""Offline comparator estimators; this file can also run inside an exported project.

No generator, card, oracle or scoring dependency. Fitted transforms live inside each fit.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin, TransformerMixin
from sklearn.decomposition import PCA
from sklearn.ensemble import (
    ExtraTreesClassifier,
    ExtraTreesRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
    VotingClassifier,
)
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.mixture import GaussianMixture
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler
from sklearn.svm import SVC, SVR


def legacy_module():
    name = '_practice_legacy_recipe'
    if name not in sys.modules:
        path = Path(__file__).with_name('legacy.py')
        if not path.exists():
            path = Path(__file__).resolve().parents[2] / 'components/eidos.search.recipe-sweep/templates/fit_recipe.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class Legacy:
    def __init__(self, task_type, spec, seed):
        self.task_type, self.spec, self.seed = task_type, spec, seed

    def fit(self, x, y):
        module = legacy_module()
        self.pre = module.Preprocess(self.spec['preprocess']).fit(x)
        self.model = module.make_model(self.task_type, self.spec, np.random.default_rng(self.seed))
        self.model.fit(self.pre.transform(x), y)
        return self

    def predict(self, x):
        return self.model.predict(self.pre.transform(x))


class CorrelationScreen(TransformerMixin, BaseEstimator):
    def fit(self, x, y=None):
        correlation = np.nan_to_num(np.corrcoef(x, rowvar=False), nan=0.0)
        np.fill_diagonal(correlation, 0)
        support = np.max(abs(correlation), axis=0) >= 0.1
        self.columns_ = np.flatnonzero(support) if support.any() else np.arange(x.shape[1])
        return self

    def transform(self, x):
        return x[:, self.columns_]


class ClassGMM(ClassifierMixin, BaseEstimator):
    def __init__(self, seed=11):
        self.seed = seed

    def fit(self, x, y):
        self.classes_, counts = np.unique(y, return_counts=True)
        self.priors_ = np.log(counts / len(y))
        self.models_ = []
        for label in self.classes_:
            rows = x[y == label]
            options = [GaussianMixture(n_components=k, covariance_type='full', reg_covar=1e-4,
                                       max_iter=150, random_state=self.seed).fit(rows)
                       for k in (1, 2, 3, 4, 5) if len(rows) >= max(k * 3, 2)]
            if not options:
                raise ValueError('not enough rows for class conditional density')
            self.models_.append(min(options, key=lambda m: m.bic(rows)))
        return self

    def predict(self, x):
        scores = np.column_stack([model.score_samples(x) + prior for model, prior in zip(self.models_, self.priors_, strict=False)])
        return self.classes_[scores.argmax(axis=1)]


def row_center(x):
    return x - x.mean(axis=1, keepdims=True)


class Recipe:
    def __init__(self, task_type, spec, seed):
        self.task_type, self.spec, self.seed = task_type, spec, seed

    def fit(self, x, y, row_ids=None):
        self.fit_ids_ = np.asarray(row_ids if row_ids is not None else np.arange(len(x)))
        family = self.spec['family']
        classification = self.task_type == 'classification'
        if self.spec.get('v1'):
            self.estimator_ = Legacy(self.task_type, self.spec['v1'], self.seed).fit(x, y)
            self.transform_fit_ids_ = [self.fit_ids_.tolist()]
            return self
        if family == 'linear':
            estimator = LogisticRegression(C=self.spec.get('C', 1.0), max_iter=1500, random_state=self.seed) if classification else Ridge(alpha=self.spec.get('alpha', 1.0))
        elif family == 'kernel':
            estimator = SVC(C=self.spec.get('C', 10.0), gamma=self.spec.get('gamma', 'scale'), random_state=self.seed) if classification else SVR(C=self.spec.get('C', 100.0), gamma=self.spec.get('gamma', 'scale'))
        elif family == 'trees':
            cls = ExtraTreesClassifier if classification else ExtraTreesRegressor
            estimator = cls(n_estimators=100, max_features=0.8, n_jobs=1, random_state=self.seed)
        elif family == 'boosting':
            cls = HistGradientBoostingClassifier if classification else HistGradientBoostingRegressor
            estimator = cls(max_iter=150, max_leaf_nodes=15, early_stopping=False, random_state=self.seed)
        elif family == 'generative':
            estimator = ClassGMM(self.seed)
        elif family == 'neural':
            if classification:
                members = [(str(i), MLPClassifier(hidden_layer_sizes=(64, 64), max_iter=300, batch_size=256,
                                                 random_state=self.seed + i, early_stopping=False)) for i in range(5)]
                estimator = VotingClassifier(members, voting='soft', n_jobs=1)
            else:
                estimator = MLPRegressor(hidden_layer_sizes=(64, 64), max_iter=300, batch_size=256, random_state=self.seed)
        else:
            raise ValueError('unknown comparator family')
        transforms = []
        if self.spec.get('center'):
            transforms.append(FunctionTransformer(row_center))
        if self.spec.get('screen'):
            transforms.append(CorrelationScreen())
        transforms.append(StandardScaler())
        if self.spec.get('pca'):
            transforms.append(PCA(n_components=0.99, whiten=True, svd_solver='full'))
        self.estimator_ = make_pipeline(*transforms, estimator)
        # Explicit, frozen resource cap, applied inside every training fold, never to evaluation rows.
        cap = self.spec.get('max_rows', 5000 if family in ('kernel', 'neural', 'generative') else len(x))
        rows = np.arange(len(x))
        if len(x) > cap:
            from sklearn.model_selection import train_test_split
            rows, _ = train_test_split(rows, train_size=cap, random_state=self.seed,
                                       stratify=y if classification else None)
        self.estimator_.fit(x[rows], y[rows])
        self.transform_fit_ids_ = [self.fit_ids_[rows].tolist() for _ in transforms]
        return self

    def predict(self, x):
        return self.estimator_.predict(x)
