"""Execute the committed prospective procedure once; failures are dropped, never tuned."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import sklearn
import torch
from threadpoolctl import threadpool_limits

from study.common import DEVELOPMENT, research_python, write_json
from study.packaging.p3_data import FROZEN, TASKS, dataset
from study.packaging.reference_p3 import recipe

sys.path.insert(0, str(DEVELOPMENT))
from grade import read_predictions, score


def array_hash(a):
    return hashlib.sha256(str((a.shape, a.dtype.str)).encode() + a.tobytes()).hexdigest()


def main(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    if FROZEN.exists():
        raise ValueError('qualification already frozen; do not rerun or overwrite')
    rows = {}
    for card in TASKS:
        data, kind, classes = dataset(card)
        inputs = root / card / 'baseline-inputs'
        inputs.mkdir(parents=True)
        write_json(inputs / 'task.json', dict(task_id=card, project=TASKS[card],
                                             nclasses=classes, task_type=kind))
        for split in ('train', 'validation'):
            x, y, ids = data[split]
            np.savez(inputs / (split + '.npz'), x=x, y=y, ids=ids)
        x, y, ids = data['test']
        test = root / card / 'test-inputs.npz'
        np.savez(test, x=x, ids=ids)
        baselines, references, elapsed = {}, {}, {}
        for seed in (11, 23, 37):
            out = root / card / ('baseline-' + str(seed))
            started = time.perf_counter()
            source = Path(__file__).with_name('baseline_project.py')
            subprocess.run([research_python(), str(source), 'train', '--inputs', str(inputs),
                            '--out', str(out), '--seed', str(seed)], check=True, timeout=60)
            elapsed[str(seed)] = time.perf_counter() - started
            subprocess.run([research_python(), str(source), 'infer', '--model', str(out / 'model.pt'),
                            '--inputs', str(test), '--out', str(out / 'test.npz')], check=True, timeout=60)
            baselines[str(seed)] = score(y, read_predictions(out / 'test.npz', ids, kind, classes), kind)
        comparator = baselines['11']
        bar = 0.02 if kind == 'classification' else 0.05 * comparator
        for seed in (11, 23, 37):
            started = time.perf_counter()
            model = recipe(card, seed)
            with threadpool_limits(limits=1):
                model.fit(data['train'][0], data['train'][1])
                value = score(y, model.predict(x), kind)
            gain = value - comparator if kind == 'classification' else comparator - value
            references[str(seed)] = dict(seed=seed, baseline=comparator,
                                         diagnostic_baseline_same_seed=baselines[str(seed)],
                                         reference=value, gain=gain, passes=gain >= bar,
                                         elapsed_seconds=time.perf_counter() - started)
        row = dict(metric='accuracy' if kind == 'classification' else 'rmse',
                   baseline=comparator, baseline_by_seed=baselines, bar=bar,
                   baseline_wall_seconds=elapsed, references=references,
                   qualified=all(r['passes'] for r in references.values()),
                   splits={s: dict(n=len(v[1]), hashes={name: array_hash(a) for name, a in
                            zip(('x', 'y', 'ids'), v)}) for s, v in data.items()})
        rows[card] = row
        write_json(root / 'results.json', rows)
        print(json.dumps(dict(card=card, **row)), flush=True)
    write_json(FROZEN, rows)
    write_json(root / 'runtime.json', dict(python=sys.version, numpy=np.__version__,
                                         torch=torch.__version__, sklearn=sklearn.__version__))
    print('FROZEN: ' + ', '.join(c + (' accepted' if r['qualified'] else ' DROPPED') for c, r in rows.items()))


if __name__ == '__main__':
    main(sys.argv[1])
