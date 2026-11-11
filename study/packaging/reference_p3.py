"""Fixed qualification recipes, stated and committed BEFORE their first execution.

Controller only: never included in an investigator bundle. No test-driven selection.
The ordinary CLI also permits submitting qualification controls to the hosted grader.
"""
import argparse
import json
from pathlib import Path
import time

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from threadpoolctl import threadpool_limits


def recipe(card, seed):
    if card in ('N01', 'N04'):
        return make_pipeline(StandardScaler(), SVC(C=10.0, kernel='rbf', gamma='scale', random_state=seed))
    if card == 'N02':
        return make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    if card == 'N03':
        return HistGradientBoostingRegressor(max_iter=200, max_leaf_nodes=15,
                                            learning_rate=0.1, l2_regularization=1.0,
                                            early_stopping=False, random_state=seed)
    raise ValueError(card)


def train(inputs, out, seed):
    started = time.perf_counter()
    root, out = Path(inputs), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    task = json.loads((root / 'task.json').read_text())
    model = recipe(task['task_id'], seed)
    with np.load(root / 'train.npz', allow_pickle=False) as d, threadpool_limits(limits=1):
        model.fit(d['x'], d['y'])
    joblib.dump(model, out / 'model.pt')
    infer(out / 'model.pt', root / 'validation.npz', out / 'validation_predictions.npz')
    (out / 'receipt.json').write_text(json.dumps(dict(seed=seed, threads=1, device='cpu',
                                                     elapsed_seconds=time.perf_counter() - started)))


def infer(model, inputs, out):
    # Candidate-process deserialization only; the trusted scorer never loads this.
    model = joblib.load(model)
    with np.load(inputs, allow_pickle=False) as d, threadpool_limits(limits=1):
        np.savez(out, ids=d['ids'], predictions=model.predict(d['x']))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    fit = sub.add_parser('train')
    fit.add_argument('--inputs', required=True); fit.add_argument('--out', required=True)
    fit.add_argument('--seed', type=int, default=11)
    pred = sub.add_parser('infer')
    pred.add_argument('--model', required=True); pred.add_argument('--inputs', required=True)
    pred.add_argument('--out', required=True)
    a = p.parse_args()
    if a.command == 'train':
        train(a.inputs, a.out, a.seed)
    else:
        infer(a.model, a.inputs, a.out)
