"""Portable comparator export: train once on train, reload fitted state for inference."""
import argparse
import json
import time
from pathlib import Path

import joblib
import numpy as np
from models import Recipe, legacy_module
from threadpoolctl import threadpool_limits


def infer(model, inputs, out):
    legacy_module()
    estimator = joblib.load(model)
    with np.load(inputs, allow_pickle=False) as data:
        np.savez(out, ids=data['ids'], predictions=estimator.predict(data['x']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('train', 'infer'))
    parser.add_argument('--inputs', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--model')
    parser.add_argument('--seed', type=int, default=11)
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        if args.command == 'infer':
            infer(args.model, args.inputs, args.out)
            return
        root, out = Path(args.inputs), Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        spec = json.loads(Path(__file__).with_name('recipe.json').read_text())
        task = json.loads((root / 'task.json').read_text())
        started = time.perf_counter()
        with np.load(root / 'train.npz', allow_pickle=False) as data:
            model = Recipe(task['task_type'], spec, args.seed).fit(data['x'], data['y'], data['ids'])
        fit_seconds = time.perf_counter() - started
        joblib.dump(model, out / 'model.pt')
        infer(out / 'model.pt', root / 'validation.npz', out / 'validation_predictions.npz')
        (out / 'receipt.json').write_text(json.dumps(dict(seed=args.seed, threads=1, fit_seconds=fit_seconds,
                                                         elapsed_seconds=time.perf_counter() - started,
                                                         checkpoint_bytes=(out / 'model.pt').stat().st_size)))


if __name__ == '__main__':
    main()
