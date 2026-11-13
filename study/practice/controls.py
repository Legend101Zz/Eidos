"""Measured generic controls on actual family predictions and an end-to-end leak."""
import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.feature_selection import SelectKBest, f_classif

from study.practice.build_cards import write_json, write_npz
from study.practice.generators import registry
from study.practice.guard import evaluate
from study.practice.oracle import paired_interval
from study.practice.pipelines import compute_table


def produce(data_root, reference_root, out, *, registration):
    from study.practice.ledger import require_registration
    require_registration(registration, classes=[g["id"] for g in registry()])
    out = Path(out)
    index = json.loads((Path(data_root) / 'index.json').read_text())['cards']
    entries = {g['id']: g for g in registry()}
    roles = {g: e['role'] for g, e in entries.items()}
    effects = {name: {} for name in ('helpful', 'neutral', 'harmful', 'card-specific')}
    raw = {name: {} for name in effects}
    for card in index:
        g = card['generator']
        entry = entries[g]
        rng = np.random.default_rng(entry['seed_stream'] + card['instance'] + 49183)
        with np.load(Path(reference_root) / 'control-predictions' / f"{card['card']}.npz") as data:
            prediction, truth, train_y = data['predictions'], data['truth'], data['train_y']
        def corrupted(entry=entry, train_y=train_y, prediction=prediction, rng=rng):
            if entry['task_type'] == 'classification':
                labels = np.unique(train_y)
                changed = prediction.copy()
                mask = rng.random(len(changed)) < .12
                # Symmetric output-label corruption, without using held labels.
                changed[mask] = rng.choice(labels, mask.sum())
                return changed
            return prediction + rng.normal(0, .5 * np.std(train_y), len(prediction))
        broken, other_broken = corrupted(), corrupted()
        pairs = dict(helpful=(prediction, broken), neutral=(other_broken, broken), harmful=(broken, prediction),
                     **{'card-specific': (prediction if g == 'gen-002' else other_broken, broken)})
        for name, (candidate, baseline) in pairs.items():
            interval = paired_interval(entry['task_type'], truth, candidate, baseline)
            effects[name].setdefault(g, []).append(interval['effect'] / max(interval['se'], .001))
            raw[name].setdefault(g, []).append(interval)
    options = dict(registration=registration, in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True,
                   effective_instances={g: 1 for g in ('gen-008', 'gen-009', 'gen-015')})
    controls = {name: evaluate(values, roles, **options) for name, values in effects.items()}
    # Real pre-CV supervised screen on the compute_table data path, noise labels.
    rng = np.random.default_rng(991)
    bundle = out / 'leak-data'
    write_json(bundle / 'task.json', dict(task_type='classification'))
    for i, split in enumerate(('train', 'validation', 'test')):
        write_npz(bundle / f'{split}.npz', x=rng.normal(size=(200, 400)),
                  y=rng.integers(0, 2, 200), ids=np.arange(200) + i * 200)
    specs = [dict(id='linear', family='linear', complexity=0, pipelines=['A0', 'A1', 'A2', 'A3'])]
    args = bundle, bundle / 'test.npz', bundle / 'test.npz'
    clean = compute_table(*args, specs=specs, registration=registration)
    leaky = compute_table(*args, specs=specs, pre_cv_transform=SelectKBest(f_classif, k=5), registration=registration)
    leak = dict(clean_cv=clean['rows'][0]['cv_score'], leaky_cv=leaky['rows'][0]['cv_score'],
                clean_in_fold=clean['in_fold_ok'], leaky_in_fold=leaky['in_fold_ok'],
                actual_leaky_audits=leaky['in_fold_audits'],
                verdict=evaluate(effects['helpful'], roles, **dict(options, in_fold_ok=leaky['in_fold_ok']))['verdict'])
    passed = controls['helpful']['verdict'].startswith('accept') and all(
        controls[name]['verdict'].startswith('reject') for name in ('neutral', 'harmful', 'card-specific'))
    passed = passed and leak['clean_in_fold'] and not leak['leaky_in_fold'] and leak['leaky_cv'] > leak['clean_cv'] + .05
    report = dict(schema='practice-controls/2', controls=controls, effects=effects, raw_effects=raw, leak=leak,
                  all_met=bool(passed),
                  mechanism='Generic output corruption repair: remove 12% symmetric label jitter or Gaussian prediction noise (SD half training-target SD), without held labels. Neutral uses independent equal jitter; harm reverses repair; patch repairs gen-002 only. These are measured pipeline-output fault controls, not evidence for pipeline C.')
    write_json(out / 'measured-controls.json', report)
    print(json.dumps(dict(all_met=report['all_met'], controls={k: v['verdict'] for k, v in controls.items()}, leak=leak)))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--references', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--evaluation-id', required=True)
    parser.add_argument('--candidate-digest', required=True)
    args = parser.parse_args()
    from study.practice.ledger import register
    registration = register(evaluation_id=args.evaluation_id, candidate_digest=args.candidate_digest,
                            classes=[g['id'] for g in registry()], primary_verdict='measured construction controls', kind='construction')
    produce(args.data, args.references, args.out, registration=registration)


if __name__ == '__main__':
    main()
