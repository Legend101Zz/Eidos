"""Train-only repeated folds. Every fit constructs a fresh complete pipeline."""
import hashlib
import time
import warnings

import numpy as np
from sklearn.model_selection import GroupKFold, KFold, StratifiedKFold

from .models import build
from .select import score
from .util import canonical


def partitions(x, y, kind, *, seed=81723, folds=5, repeats=None, groups=None):
    repeats = (2 if len(y) < 1000 else 1) if repeats is None else repeats
    if kind == "classification":
        folds = min(folds, int(np.unique(y, return_counts=True)[1].min()))
    if groups is not None:
        folds = min(folds, len(np.unique(groups)))
    if folds < 2:
        raise ValueError("at least two rows per class/group required for CV")
    result = []
    for repeat in range(repeats):
        if groups is not None:
            splitter = GroupKFold(n_splits=folds, shuffle=True, random_state=seed + repeat)
        else:
            splitter = (StratifiedKFold if kind == "classification" else KFold)(n_splits=folds, shuffle=True, random_state=seed + repeat)
        result.extend(splitter.split(x, y, groups) if groups is not None else splitter.split(x, y))
    return [(np.asarray(tr), np.asarray(va)) for tr, va in result]


def folds_digest(partitions):
    return hashlib.sha256(canonical([(tr.tolist(), va.tolist()) for tr, va in partitions]).encode()).hexdigest()


def evaluate(spec, x, y, *, partitions, seed=11, ids=None):
    scores, predictions, row_ids, fold_ids = [], [], [], []
    ids = np.arange(len(x)) if ids is None else ids
    started = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for i, (tr, va) in enumerate(partitions):
            model = fit_fold(spec, x, y, tr, seed=seed)
            p = model.predict(x[va])
            if not np.isfinite(p).all():
                raise ValueError("nonfinite predictions")
            scores.append(score(spec["task_type"], y[va], p))
            predictions.extend(p.tolist())
            row_ids.extend(ids[va].tolist())
            fold_ids.extend([i] * len(va))
    return dict(status="ok", scores=scores, mean=float(np.mean(scores)), fit_seconds=time.perf_counter() - started,
                oof=dict(predictions=predictions, row_ids=row_ids, fold_ids=fold_ids))


def fit_fold(spec, x, y, train, *, seed=11):
    """The actual fold-fit path, also exercised by the held-row poison audit."""
    return build(spec, seed).fit(x[train], y[train])
