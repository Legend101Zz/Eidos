"""Independent large-sample attainable references, never used for recipe selection.

Synthetic references use fresh seed streams and independent 20k/40k training,
2k selection and 5k test rows. Finite-source references fit train+validation on
an actual resplit (maximum available data), with shared-source effective n=1.
These are empirical references, not Bayes upper bounds.
"""
import argparse
import json
import statistics
import warnings
from pathlib import Path

import numpy as np
from sklearn.ensemble import (
    ExtraTreesClassifier,
    ExtraTreesRegressor,
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, SVR
from threadpoolctl import threadpool_limits

from study.practice.build_cards import write_json, write_npz
from study.practice.generators import generate, registry
from study.practice.oracle import score, score_se


def estimators(kind, seed):
    classification = kind == 'classification'
    return [make_pipeline(StandardScaler(), LogisticRegression(max_iter=1500, random_state=seed) if classification else Ridge()),
            (HistGradientBoostingClassifier if classification else HistGradientBoostingRegressor)(max_iter=300, max_leaf_nodes=31, early_stopping=False, random_state=seed),
            (ExtraTreesClassifier if classification else ExtraTreesRegressor)(n_estimators=200, n_jobs=1, random_state=seed),
            make_pipeline(StandardScaler(), SVC(C=10) if classification else SVR(C=100))]


def produce(family, data_root, out, *, registration):
    from study.practice.ledger import require_bundle_registration, require_registration
    require_registration(registration, classes=[g["id"] for g in registry()])
    if registration['kind'] != 'harness-validation':
        raise ValueError('harness references/controls require the separate harness-validation budget')
    out = Path(out)
    by_generator = {}
    rows = []
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter('ignore', ConvergenceWarning)
        for entry in registry():
            cards = [r for r in family['cards'] if r['generator'] == entry['id']]
            for card in cards:
                require_bundle_registration(registration, Path(data_root) / 'bundles' / card['card'], purpose='references')
                i = card['instance']
                finite = entry['mechanism'] in ('wine-nuisance', 'digits-rotation', 'offset-reenactment')
                if finite:
                    original = generate(entry, i)
                    train = dict(x=np.concatenate([original.splits[s]['x'] for s in ('train', 'validation')]),
                                 y=np.concatenate([original.splits[s]['y'] for s in ('train', 'validation')]))
                    # Selection uses only the original train/validation data; final refit uses their union.
                    train_selection, validation, test = (original.splits[s] for s in ('train', 'validation', 'test'))
                else:
                    ref_entry = dict(entry, seed_stream=entry['seed_stream'] + 100000000,
                                     sizes=dict(train=40000 if entry['mechanism'] == 'large-hastie' else 20000, validation=2000, test=5000))
                    original = generate(ref_entry, i)
                    train, validation, test = (original.splits[s] for s in ('train', 'validation', 'test'))
                    train_selection = train
                models = estimators(entry['task_type'], 11)
                scores = []
                for model in models:
                    cap = 5000 if isinstance(model, type(models[-1])) and isinstance(model[-1], (SVC, SVR)) else len(train_selection['y'])
                    model.fit(train_selection['x'][:cap], train_selection['y'][:cap])
                    scores.append(score(entry['task_type'], validation['y'], model.predict(validation['x'])))
                chosen = int(np.argmax(scores))
                model = models[chosen]
                if finite:
                    model.fit(train['x'], train['y'])
                prediction = model.predict(test['x'])
                reference = score(entry['task_type'], test['y'], prediction)
                row = dict(card=card['card'], generator=entry['id'], instance=i, reference_score=reference,
                           reference_test_se=score_se(entry['task_type'], test['y'], prediction),
                           A1_score=card.get('pipelines', {}).get('A1', {}).get('test_score'),
                           headroom=None,
                           chosen_reference_model=chosen, reference_selection_scores=scores,
                           reference_train_rows=len(train['y']), reference_test_rows=len(test['y']),
                           reference_kind='finite-source train+validation' if finite else 'independent large-sample attainable',
                           effective_instance_n=1 if finite else 4)
                rows.append(row)
                by_generator.setdefault(entry['id'], []).append(row)
                write_json(out / 'reference-progress.json', dict(completed=len(rows), latest=row))
                print(json.dumps(dict(card=card['card'], reference=reference, headroom=row['headroom'])), flush=True)
                # Persist actual-card predictions for measured generic output-bug controls.
                actual = generate(entry, i)
                if not finite:
                    # An independent reference's target may vary with seed (e.g. random low-rank
                    # coefficients). Fit the chosen algorithm to actual visible rows for controls.
                    ax = np.concatenate([actual.splits[s]['x'] for s in ('train', 'validation')])
                    ay = np.concatenate([actual.splits[s]['y'] for s in ('train', 'validation')])
                    model = estimators(entry['task_type'], 11)[chosen].fit(ax, ay)
                pred = model.predict(actual.splits['test']['x'])
                write_npz(out / 'control-predictions' / f"{card['card']}.npz", predictions=pred,
                          truth=actual.splits['test']['y'], train_y=actual.splits['train']['y'])
    summaries = {g: dict(reference_score=statistics.mean(r['reference_score'] for r in values),
                         A1_score=statistics.mean(r['A1_score'] for r in values) if all(r['A1_score'] is not None for r in values) else None,
                         headroom=statistics.mean(r['headroom'] for r in values) if all(r['headroom'] is not None for r in values) else None,
                         reference_kind=values[0]['reference_kind'], effective_instance_n=values[0]['effective_instance_n'],
                         reference_uncertainty_se=max(statistics.stdev(r['reference_score'] for r in values) /
                                                      (1 if values[0]['effective_instance_n'] == 1 else 2),
                                                      statistics.mean(r['reference_test_se'] for r in values)))
                 for g, values in by_generator.items()}
    report = dict(schema='practice-references/2', generators=summaries, instances=rows,
                  limitation='Attainable references, not Bayes scores. Independent synthetic mechanisms may change latent coefficients across seeds; headroom is a generator-distribution estimate. Finite sources share rows; effective n=1, not four independent datasets.')
    write_json(out / 'references.json', report)
    return report


def reconcile(report, family):
    """Attach measured A1 scores only after the independent references finish."""
    cards = {r['card']: r for r in family['cards']}
    for row in report['instances']:
        row['A1_score'] = cards[row['card']]['pipelines']['A1']['test_score']
        row['headroom'] = row['reference_score'] - row['A1_score']
    for g, summary in report['generators'].items():
        rows = [r for r in report['instances'] if r['generator'] == g]
        summary['A1_score'] = statistics.mean(r['A1_score'] for r in rows)
        summary['headroom'] = statistics.mean(r['headroom'] for r in rows)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', help='optional completed gate; otherwise only reference scores are produced')
    parser.add_argument('--data', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--evaluation-id', required=True)
    parser.add_argument('--candidate-digest', required=True)
    args = parser.parse_args()
    family = json.loads(Path(args.family).read_text()) if args.family else json.loads((Path(args.data) / 'index.json').read_text())
    from study.practice.ledger import register
    registration = register(evaluation_id=args.evaluation_id, candidate_digest=args.candidate_digest,
                            classes=[g['id'] for g in registry()], primary_verdict='candidate versus A2: named transfer',
                            kind='harness-validation', data_root=args.data)
    produce(family, args.data, args.out, registration=registration)


if __name__ == '__main__':
    main()
