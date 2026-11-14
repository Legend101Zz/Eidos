"""Pure array transformers: no filesystem, subprocesses, or pre-CV learned state."""
import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.decomposition import PCA
from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.preprocessing import (
    Binarizer,
    Normalizer,
    OneHotEncoder,
    QuantileTransformer,
    RobustScaler,
    StandardScaler,
)


class RowTransform(TransformerMixin, BaseEstimator):
    def __init__(self, mode="log1p_signed"):
        self.mode = mode

    def fit(self, x, y=None):
        return self

    def transform(self, x):
        if self.mode == "log1p_signed":
            return np.sign(x) * np.log1p(np.abs(x))
        if self.mode == "tfidf":
            return self.tfidf_.transform(np.maximum(x, 0)).toarray()
        centered = x - x.mean(axis=1, keepdims=True)
        return centered / np.maximum(x.std(axis=1, keepdims=True), 1e-12) if self.mode == "per_example_standardize" else centered


class SignedTfidf(TransformerMixin, BaseEstimator):
    """TF-IDF uses nonnegative magnitudes; sign is restored after term weighting."""
    def fit(self, x, y=None):
        self.tfidf_ = TfidfTransformer().fit(np.abs(x))
        return self

    def transform(self, x):
        return self.tfidf_.transform(np.abs(x)).toarray() * np.sign(x)


class ColumnSelect(TransformerMixin, BaseEstimator):
    def transform(self, x):
        return x[:, self.columns_]

    def keep(self, values):
        self.columns_ = np.flatnonzero(values)
        if not len(self.columns_):
            self.columns_ = np.array([int(np.argmax(values))])
        return self


class VarianceScreen(ColumnSelect):
    def __init__(self, threshold=1e-12):
        self.threshold = threshold

    def fit(self, x, y=None):
        return self.keep(x.var(axis=0) > self.threshold)


class CorrDedupe(ColumnSelect):
    def __init__(self, threshold=.9):
        self.threshold = threshold

    def fit(self, x, y=None):
        corr = np.nan_to_num(np.atleast_2d(np.corrcoef(x.T)))
        kept = []
        for i in range(x.shape[1]):
            if not kept or np.all(np.abs(corr[i, kept]) < self.threshold):
                kept.append(i)
        self.columns_ = np.asarray(kept)
        return self


class ImportanceTopK(ColumnSelect):
    """Cheap marginal supervised screen; classification uses class-mean contrasts."""
    def __init__(self, k=8, task_type="classification"):
        self.k, self.task_type = k, task_type

    def fit(self, x, y):
        z = (x - x.mean(axis=0)) / np.maximum(x.std(axis=0), 1e-12)
        if self.task_type == "classification":
            strength = sum((z[y == c].mean(axis=0) ** 2) * np.mean(y == c) for c in np.unique(y))
        else:
            strength = np.abs(z.T @ (y - y.mean()))
        self.columns_ = np.sort(np.argsort(-strength, kind="stable")[:min(self.k, x.shape[1])])
        return self


class ShadowScreen(ColumnSelect):
    def __init__(self, n_estimators=64, seed=11, task_type="classification"):
        self.n_estimators, self.seed, self.task_type = n_estimators, seed, task_type

    def fit(self, x, y):
        rng = np.random.default_rng(self.seed)
        shadow = np.column_stack([rng.permutation(col) for col in x.T])
        cls = ExtraTreesClassifier if self.task_type == "classification" else ExtraTreesRegressor
        model = cls(n_estimators=self.n_estimators, min_samples_leaf=3, random_state=self.seed, n_jobs=1).fit(np.column_stack((x, shadow)), y)
        importance = model.feature_importances_
        mask = importance[:x.shape[1]] > np.quantile(importance[x.shape[1]:], .95)
        if not mask.any():
            mask[np.argmax(importance[:x.shape[1]])] = True
        return self.keep(mask)


class AdaptivePCA(TransformerMixin, BaseEstimator):
    def __init__(self, n=None, var=None, auto_rank=True, whiten=True):
        self.n, self.var, self.auto_rank, self.whiten = n, var, auto_rank, whiten

    def fit(self, x, y=None):
        if self.n is not None:
            count = min(self.n, *x.shape)
        elif self.var is not None:
            count = self.var
        else:
            eigen = np.linalg.eigvalsh(np.atleast_2d(np.cov(x, rowvar=False)))
            edge = (1 + np.sqrt(x.shape[1] / max(1, x.shape[0]))) ** 2
            count = max(1, int((eigen > edge).sum()))
        self.pca_ = PCA(n_components=count, whiten=self.whiten, svd_solver="full").fit(x)
        return self

    def transform(self, x):
        return self.pca_.transform(x)


class LowCardOneHot(TransformerMixin, BaseEstimator):
    def __init__(self, max_categories=16):
        self.max_categories = max_categories

    def fit(self, x, y=None):
        self.columns_ = [i for i in range(x.shape[1]) if len(np.unique(x[:, i])) <= self.max_categories]
        self.rest_ = [i for i in range(x.shape[1]) if i not in self.columns_]
        self.encoder_ = OneHotEncoder(handle_unknown="ignore", sparse_output=False).fit(x[:, self.columns_]) if self.columns_ else None
        return self

    def transform(self, x):
        return np.column_stack((x[:, self.rest_], self.encoder_.transform(x[:, self.columns_]))) if self.encoder_ is not None else x.copy()


def build(step, seed, task_type):
    name = step["op"]
    params = {k: v for k, v in step.items() if k != "op"}
    if name == "standardize":
        return StandardScaler(**params)
    if name == "robust_scale":
        return RobustScaler()
    if name == "quantile_normal":
        return QuantileTransformer(output_distribution="normal", random_state=seed, **params)
    if name in ("log1p_signed", "per_example_center", "per_example_standardize"):
        return RowTransform(name)
    if name == "tfidf":
        return SignedTfidf()
    if name == "row_l2":
        return Normalizer()
    if name == "variance_screen":
        return VarianceScreen(**params)
    if name == "corr_dedupe":
        return CorrDedupe(**params)
    if name == "importance_topk":
        return ImportanceTopK(task_type=task_type, **params)
    if name == "shadow_screen":
        return ShadowScreen(seed=seed, task_type=task_type, **params)
    if name == "pca":
        return AdaptivePCA(**params)
    if name == "onehot_lowcard":
        return LowCardOneHot(**params)
    if name == "binarize":
        return Binarizer(**params)
    raise ValueError("unknown operator " + name)
