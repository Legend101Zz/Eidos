"""Ordinary practice baseline CLI, deliberately independent of trusted practice code."""
import argparse
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits


def infer(model, inputs, out):
    with np.load(inputs, allow_pickle=False) as data:
        np.savez(out, ids=data['ids'], predictions=joblib.load(model).predict(data['x']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['train', 'infer'])
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--seed', type=int, default=11)
    parser.add_argument('--model')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        if args.command == 'infer':
            infer(args.model, args.inputs, args.out)
            return
        root, out = Path(args.inputs), Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        task = json.loads((root / 'task.json').read_text())
        estimator = LogisticRegression(C=1.0, max_iter=1000, random_state=args.seed) if task['task_type'] == 'classification' else Ridge(alpha=1.0)
        model = make_pipeline(StandardScaler(), estimator)
        with np.load(root / 'train.npz', allow_pickle=False) as data:
            model.fit(data['x'], data['y'])
        joblib.dump(model, out / 'model.pt')
        infer(out / 'model.pt', root / 'validation.npz', out / 'validation_predictions.npz')


if __name__ == '__main__':
    main()
