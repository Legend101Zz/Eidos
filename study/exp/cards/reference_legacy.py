"""Measure train-only strong references against the actual public development baselines."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import tempfile
import time
from pathlib import Path

import numpy as np

from study.common import DEVELOPMENT, ROOT, dev_command
from study.exp.cards.common import run_cli, write_json
from study.exp.cards.reference import evaluate_spec, select_reference
from study.packaging.p3_data import FROZEN
from study.packaging.p3_data import dataset as prospective_dataset

CARDS = ('N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04')


def public_dataset(project):
    spec = importlib.util.spec_from_file_location('legacy_public_data', DEVELOPMENT / 'data.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.dataset(project)


def reference(card, root, budget_seconds):
    bundle = root / 'bundle'
    dev_command('package_task.py', '--task', card, '--out', bundle)
    task = json.loads((bundle / 'task.json').read_text())
    if card.startswith('N'):
        data, kind, _ = prospective_dataset(card)
        frozen = json.loads(FROZEN.read_text())[card]
        bar = frozen['bar']
    else:
        task_spec = json.loads((DEVELOPMENT / 'tasks' / (card + '.json')).read_text())
        data, kind, _ = public_dataset(task_spec['project'])
        bar = task_spec['min_reference_gain']
    x, y, _ = data['train']
    # Search is complete before the held-out arrays enter any selection function.
    spec = select_reference(x, y, kind, budget_seconds=budget_seconds, temp_root=root)
    xt, yt, ids = data['test']
    np.savez(root / 'test_inputs.npz', x=xt, ids=ids)
    baseline, baseline_ids, _, _ = run_cli(bundle / 'project.py', bundle, root / 'test_inputs.npz', root / 'baseline', 11)
    if not np.array_equal(ids, baseline_ids):
        raise ValueError('legacy baseline IDs disagree')
    if kind == 'classification':
        base = float(np.mean(baseline == yt))
    else:
        # Match development.grade's float64 scorer, including its baseline rounding.
        base = float(np.sqrt(np.mean((baseline.astype(float) - yt) ** 2)))
    if card.startswith('N') and not math.isclose(base, frozen['baseline'], rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError('legacy baseline differs from frozen grader baseline')
    result = evaluate_spec(spec, bundle, root / 'test_inputs.npz', yt, baseline, base, bar, root / 'reference')
    result['project'] = task['project']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--budget-seconds', type=float, default=480)
    args = parser.parse_args(argv)
    started = time.monotonic()
    scratch = ROOT / '.wp6b2-scratch'
    scratch.mkdir(exist_ok=True)
    references = {}
    with tempfile.TemporaryDirectory(prefix='legacy-', dir=scratch) as temporary:
        for card in CARDS:
            root = Path(temporary) / card
            root.mkdir()
            references[card] = reference(card, root, args.budget_seconds)
            print(f'{card}: baseline={references[card]["base_score"]:.6g} reference={references[card]["ref_score"]:.6g}', flush=True)
    write_json(args.out, references)
    print(f'Wall time: {time.monotonic() - started:.3f} s', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
