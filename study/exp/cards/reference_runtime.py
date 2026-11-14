# ruff: noqa: E402
# Thread caps must be installed before numpy/sklearn imports in exported candidates.
"""Standalone sklearn candidate and CV worker; copied verbatim with an embedded spec.

Checkpoints are local self-trained sklearn objects, never downloaded source data.
"""

import argparse
import gzip
import json
import os
import pickle
import time
import warnings
from pathlib import Path

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
              "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
    os.environ[_name] = "1"

import numpy as np
from sklearn.compose import TransformedTargetRegressor
from sklearn.ensemble import (
    ExtraTreesClassifier,
    ExtraTreesRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
    RandomForestClassifier,
    RandomForestRegressor,
)
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import RepeatedKFold, RepeatedStratifiedKFold
from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, SVR

SPEC = None


def estimator(config, task_type, seed):
    classification = task_type == "classification"
    params = dict(config["params"])
    family = config["family"]
    if family == "linear":
        model = (LogisticRegression(max_iter=1000, random_state=seed, **params) if classification else
                 Ridge(**params))
    elif family == "knn":
        model = (KNeighborsClassifier if classification else KNeighborsRegressor)(n_jobs=1, **params)
    elif family == "rbf":
        model = SVC(probability=True, random_state=seed, **params) if classification else SVR(**params)
    elif family == "extra":
        model = (ExtraTreesClassifier if classification else ExtraTreesRegressor)(
            n_jobs=1, random_state=seed, **{**dict(n_estimators=100, min_samples_leaf=2, max_depth=20), **params})
    elif family == "forest":
        model = (RandomForestClassifier if classification else RandomForestRegressor)(
            n_jobs=1, random_state=seed, **{**dict(n_estimators=100, min_samples_leaf=2, max_depth=20), **params})
    elif family == "hist":
        model = (HistGradientBoostingClassifier if classification else HistGradientBoostingRegressor)(
            early_stopping=True, validation_fraction=0.15, random_state=seed, **params)
    elif family == "mlp":
        model = (MLPClassifier if classification else MLPRegressor)(
            hidden_layer_sizes=(64, 32), max_iter=200, early_stopping=True, random_state=seed, **params)
    else:
        raise ValueError("unknown reference family")
    if family in {"linear", "knn", "rbf", "mlp"}:
        model = make_pipeline(StandardScaler(), model)
        if not classification:
            model = TransformedTargetRegressor(regressor=model, transformer=StandardScaler())
    return model


def fit_configuration(config, x, y, task_type, seed):
    offsets = (0, 1009, 2017) if config["family"] == "mlp" else (0,)
    models = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        for offset in offsets:
            model = estimator(config, task_type, seed + offset)
            model.fit(x, y)
            models.append(model)
    return models


def predict_configuration(models, x, task_type):
    if task_type == "classification":
        return np.mean([model.predict_proba(x) for model in models], axis=0)
    return np.mean([model.predict(x) for model in models], axis=0)


def cross_validate(data_path, config_path, output, task_type, seed):
    with np.load(data_path, allow_pickle=False) as data:
        x, y = data["x"], data["y"]
    config = json.loads(Path(config_path).read_text())
    repeats = 2 if len(y) < 1000 else 1
    splitter = (RepeatedStratifiedKFold if task_type == "classification" else RepeatedKFold)(
        n_splits=5, n_repeats=repeats, random_state=seed)
    shape = (len(y), len(np.unique(y))) if task_type == "classification" else (len(y),)
    oof = np.zeros(shape, dtype=float)
    counts = np.zeros(len(y), dtype=int)
    started = time.monotonic()
    estimated_bytes = 0
    for fold, (train, held) in enumerate(splitter.split(x, y)):
        models = fit_configuration(config, x[train], y[train], task_type, seed + fold)
        if fold == 0:
            # Fit once on the CV training fold; conservatively extrapolate to full train.
            estimated_bytes = int(len(gzip.compress(pickle.dumps(models, protocol=5), compresslevel=3)) *
                                  len(y) / len(train) * 1.5)
            if estimated_bytes > config.get("size_limit_bytes", 48 * 1024 * 1024):
                np.savez(output, estimated_bytes=estimated_bytes)
                return
        oof[held] += predict_configuration(models, x[held], task_type)
        counts[held] += 1
    oof /= counts[:, None] if oof.ndim == 2 else counts
    np.savez(output, predictions=oof, estimated_bytes=estimated_bytes, seconds=time.monotonic() - started)


def train(inputs, out, seed):
    inputs, out = Path(inputs), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    spec = SPEC
    if spec is None:
        raise ValueError("reference candidate has no embedded spec")
    task = json.loads((inputs / "task.json").read_text())
    if task["task_type"] != spec["task_type"]:
        raise ValueError("reference task type mismatch")
    with np.load(inputs / "train.npz", allow_pickle=False) as data:
        x, y = data["x"], data["y"]
    started = time.monotonic()
    members = [fit_configuration(member["config"], x, y, spec["task_type"], seed) for member in spec["members"]]
    with gzip.open(out / "model.pt", "wb", compresslevel=3) as stream:
        pickle.dump(dict(spec=spec, members=members), stream, protocol=5)
    infer(out / "model.pt", inputs / "validation.npz", out / "validation_predictions.npz")
    (out / "receipt.json").write_text(json.dumps(dict(seed=seed, threads=1,
                                      elapsed_seconds=time.monotonic() - started)) + "\n")


def infer(checkpoint, inputs, out):
    with gzip.open(checkpoint, "rb") as stream:
        saved = pickle.load(stream)  # Locally trained candidate artifact, never an intake source.
    spec = saved["spec"]
    with np.load(inputs, allow_pickle=False) as data:
        x, ids = data["x"], data["ids"]
    predictions = sum(member["weight"] * predict_configuration(models, x, spec["task_type"])
                      for member, models in zip(spec["members"], saved["members"], strict=True))
    if spec["task_type"] == "classification":
        predictions = predictions.argmax(axis=1).astype(np.int64)
    np.savez(out, ids=ids, predictions=predictions)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("train")
    fit.add_argument("--inputs", required=True)
    fit.add_argument("--out", required=True)
    fit.add_argument("--seed", type=int, default=11)
    pred = commands.add_parser("infer")
    pred.add_argument("--model", required=True)
    pred.add_argument("--inputs", required=True)
    pred.add_argument("--out", required=True)
    cv = commands.add_parser("cv")
    cv.add_argument("--data", required=True)
    cv.add_argument("--config", required=True)
    cv.add_argument("--out", required=True)
    cv.add_argument("--task-type", required=True)
    cv.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    if args.command == "train":
        train(args.inputs, args.out, args.seed)
    elif args.command == "infer":
        infer(args.model, args.inputs, args.out)
    else:
        cross_validate(args.data, args.config, args.out, args.task_type, args.seed)


if __name__ == "__main__":
    main()
