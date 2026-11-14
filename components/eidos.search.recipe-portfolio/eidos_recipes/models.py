"""One train-matched sklearn executor, including seeded MLP members and class GMMs."""
import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis, QuadraticDiscriminantAnalysis
from sklearn.ensemble import (
    ExtraTreesClassifier,
    ExtraTreesRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
    RandomForestClassifier,
    RandomForestRegressor,
)
from sklearn.kernel_ridge import KernelRidge
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.mixture import GaussianMixture
from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.svm import SVC, SVR, LinearSVC, LinearSVR

from . import ops
from .spec import validate


class ClassGMM(ClassifierMixin, BaseEstimator):
    def __init__(self, k=2, bic=False, covariance_type="diag", seed=11):
        self.k, self.bic, self.covariance_type, self.seed = k, bic, covariance_type, seed

    def fit(self, x, y):
        self.classes_, counts = np.unique(y, return_counts=True)
        self.priors_ = counts / len(y)
        self.models_ = []
        for cls in self.classes_:
            z = x[y == cls]
            choices = range(1, min(6, len(z) - 1) + 1) if self.bic else [min(self.k, len(z) - 1)]
            fitted = [GaussianMixture(n_components=k, covariance_type=self.covariance_type, reg_covar=1e-3, random_state=self.seed, n_init=1, max_iter=100).fit(z) for k in choices]
            self.models_.append(min(fitted, key=lambda m: m.bic(z)))
        return self

    def predict_proba(self, x):
        logp = np.column_stack([m.score_samples(x) + np.log(p) for m, p in zip(self.models_, self.priors_, strict=False)])
        logp -= logp.max(axis=1, keepdims=True)
        p = np.exp(logp)
        return p / p.sum(axis=1, keepdims=True)

    def predict(self, x):
        return self.classes_[self.predict_proba(x).argmax(axis=1)]


class SeededMLP(BaseEstimator):
    def __init__(self, width=64, depth=2, members=5, max_iter=300, alpha=.001, seed=11, task_type="classification"):
        self.width, self.depth, self.members, self.max_iter = width, depth, members, max_iter
        self.alpha, self.seed, self.task_type = alpha, seed, task_type

    def fit(self, x, y):
        self.models_ = []
        # Train-only target scaling is part of the shipped estimator.
        self.target_mean_ = float(np.mean(y)) if self.task_type == "regression" else 0.
        self.target_scale_ = max(float(np.std(y)), 1e-12) if self.task_type == "regression" else 1.
        target = (y - self.target_mean_) / self.target_scale_ if self.task_type == "regression" else y
        cls = MLPClassifier if self.task_type == "classification" else MLPRegressor
        for i in range(self.members):
            model = cls(hidden_layer_sizes=(self.width,) * self.depth, alpha=self.alpha, max_iter=self.max_iter,
                        batch_size=min(256, len(x)), early_stopping=False, random_state=self.seed + i * 104729)
            self.models_.append(model.fit(x, target))
        if self.task_type == "classification":
            self.classes_ = self.models_[0].classes_
        return self

    def predict(self, x):
        if self.task_type == "classification":
            return self.classes_[np.mean([m.predict_proba(x) for m in self.models_], axis=0).argmax(axis=1)]
        return np.mean([m.predict(x) for m in self.models_], axis=0) * self.target_scale_ + self.target_mean_


def build(spec, seed=11):
    validate(spec)
    kind = spec["task_type"]
    classification = kind == "classification"
    family, p = spec["model"]["family"], dict(spec["model"].get("params", {}))
    if family == "ridge":
        model = Ridge(**p)
    elif family == "logistic":
        model = LogisticRegression(max_iter=1000, random_state=seed, **p)
    elif family == "lda":
        model = LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto")
    elif family == "qda":
        model = QuadraticDiscriminantAnalysis(**({"reg_param": .1} | p))
    elif family == "gmm_class":
        model = ClassGMM(seed=seed, **p)
    elif family == "knn":
        model = (KNeighborsClassifier if classification else KNeighborsRegressor)(n_jobs=1, **({"n_neighbors": 5} | p))
    elif family == "svm_rbf":
        model = (SVC if classification else SVR)(kernel="rbf", **p)
    elif family == "svm_linear":
        model = (LinearSVC if classification else LinearSVR)(random_state=seed, max_iter=3000, **p)
    elif family == "kernel_ridge":
        model = KernelRidge(kernel="rbf", **p)
    elif family == "svr":
        model = SVR(**p)
    elif family in ("extra_trees", "random_forest"):
        cls = (ExtraTreesClassifier if classification else ExtraTreesRegressor) if family == "extra_trees" else (RandomForestClassifier if classification else RandomForestRegressor)
        model = cls(random_state=seed, n_jobs=1, **({"n_estimators": 64, "min_samples_leaf": 4} | p))
    elif family == "hgb":
        model = (HistGradientBoostingClassifier if classification else HistGradientBoostingRegressor)(random_state=seed, **({"max_iter": 100, "early_stopping": False} | p))
    elif family == "mlp":
        model = SeededMLP(seed=seed, task_type=kind, **p)
    else:
        raise ValueError("unknown model family")
    steps = [(f"step{i}", ops.build(step, seed, kind)) for i, step in enumerate(spec["steps"])]
    return Pipeline(steps + [("model", model)])
