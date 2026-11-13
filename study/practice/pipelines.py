"""A0–A3 comparators and selection replay. Test scores are read only after selection."""
import hashlib
import importlib.metadata
import importlib.util
import json
import time
import warnings
from pathlib import Path

import numpy as np
from sklearn.base import clone
from sklearn.exceptions import ConvergenceWarning
from sklearn.model_selection import GroupKFold, KFold, StratifiedKFold
from threadpoolctl import threadpool_limits

from study.practice.build_cards import ROOT
from study.practice.guard import check_in_fold
from study.practice.ledger import require_bundle_registration
from study.practice.models import Recipe
from study.practice.oracle import paired_interval, score, score_se


def recipe_specs(task_type):
    path = ROOT / 'components/eidos.search.recipe-sweep/recipe_sweep.py'
    spec = importlib.util.spec_from_file_location('_practice_sweep', path)
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    specs = []
    for family in sweep.TEMPLATE_FAMILIES:
        for settings in sweep.TEMPLATE_SETTINGS['broad'][family]:
            for pre in sweep.TEMPLATE_PREPROCESS:
                specs.append(dict(family=family, v1=dict(family=family, preprocess=pre, settings=settings), pipelines=['A1']))
    for family in ('linear', 'kernel', 'trees', 'boosting'):
        specs.append(dict(family=family, pipelines=['A2']))
    # Coding-arm families, all selected by CV, with learned transforms inside folds.
    for center in (False, True):
        specs.append(dict(family='neural', screen=True, center=center, pipelines=['A3']))
        specs.append(dict(family='kernel', screen=True, pca=True, center=center, pipelines=['A3']))
        if task_type == 'classification':
            specs.append(dict(family='generative', screen=True, center=center, pipelines=['A3']))
    specs.append(dict(family='linear', pipelines=['A0']))
    return [dict(id=f'r{i:03d}', complexity=i, **spec) for i, spec in enumerate(specs)]


def source_digest():
    paths = [*(Path(__file__).with_name(name) for name in ('pipelines.py', 'models.py', 'oracle.py')),
             Path(__file__).with_name('registry.json'),
             ROOT / 'components/eidos.search.recipe-sweep/recipe_sweep.py',
             ROOT / 'components/eidos.search.recipe-sweep/templates/fit_recipe.py',
             ROOT / 'docs/experiments/practice-family/MANIFEST.json']
    return hashlib.sha256(b''.join(p.read_bytes() for p in sorted(paths))).hexdigest()


def select(rows, pipeline, *, band=0.0, complexity_order=None):
    available = [row for row in rows if pipeline in row['spec']['pipelines'] and row.get('error') is None]
    if not available:
        raise ValueError(f'no completed recipes for {pipeline}')
    if pipeline == 'A0':
        return available[0]
    key = 'validation_score' if pipeline == 'A1' else 'cv_score'
    best = max(available, key=lambda row: (row[key], -row['spec']['complexity']))
    if band:
        best_folds = np.asarray(best['fold_scores'])
        eligible = []
        for row in available:
            differences = best_folds - row['fold_scores']
            se = float(differences.std(ddof=1) / np.sqrt(len(differences)))
            if float(differences.mean()) <= band * se + 1e-12:
                eligible.append(row)
        ranks = complexity_order or {}
        return min(eligible, key=lambda row: (ranks.get(row['spec']['family'], row['spec']['complexity']), row['spec']['complexity']))
    return best


def replay(table):
    rows = table['rows']
    result = {}
    for pipeline in ('A0', 'A1', 'A2', 'A3'):
        choice = select(rows, pipeline)
        eligible = [row for row in rows if pipeline in row['spec']['pipelines'] and row.get('error') is None]
        result[pipeline] = dict(recipe=choice['spec']['id'], validation_score=choice['validation_score'],
                                cv_score=choice['cv_score'], test_score=choice['test_score'],
                                optimism=choice['validation_score' if pipeline in ('A0', 'A1') else 'cv_score'] - choice['test_score'],
                                selection_regret=max(row['test_score'] for row in eligible) - choice['test_score'],
                                paired_baseline=choice['paired_baseline'])
    # These are replay diagnostics, never pipeline C or a tuned choice on holdout data.
    rng = np.random.default_rng(81723)
    families = sorted({row['spec']['family'] for row in rows})
    permuted = dict(zip(families, map(int, rng.permutation(len(families))), strict=False))
    ablations = {}
    for pipeline in ('A2', 'A3'):
        for band in (0.5, 1, 1.5, 2):
            for name, order in [('fixed', None), ('permuted', permuted)]:
                row = select(rows, pipeline, band=band, complexity_order=order)
                ablations[f'{pipeline}/band={band}/{name}'] = row['spec']['id']
    return dict(pipelines=result, ablations=ablations, pipeline_C='not implemented in step 1')


class FoldWorkflow:
    """Actual scoring path, including an optional intentionally leaky pre-CV transform."""
    def __init__(self, kind, spec, seed, pre_cv_transform):
        self.kind, self.spec, self.seed, self.pre_cv_transform = kind, spec, seed, pre_cv_transform

    def fit(self, x, y, ids, train):
        self.pre_ = clone(self.pre_cv_transform) if self.pre_cv_transform is not None else None
        features = self.pre_.fit_transform(x, y) if self.pre_ is not None else x
        self.model_ = Recipe(self.kind, self.spec, self.seed).fit(features[train], y[train], ids[train])
        self.transform_fit_ids_ = self.model_.transform_fit_ids_
        return self

    def predict(self, x):
        return self.model_.predict(self.pre_.transform(x) if self.pre_ is not None else x)


def compute_table(bundle, test_inputs, test_labels, *, partition_seed=81723, folds=5, model_seed=11, specs=None,
                  supplied_baseline=None, pre_cv_transform=None, registration=None):
    receipt = require_bundle_registration(registration, bundle, test_inputs=test_inputs, test_labels=test_labels, purpose='comparators')
    if receipt['kind'] == 'harness-validation' and (specs is not None or pre_cv_transform is not None
            or (partition_seed, folds, model_seed) != (81723, 5, 11)):
        raise ValueError('harness-validation scores only the frozen A0-A3 comparator configuration')
    started = time.perf_counter()
    bundle = Path(bundle)
    task = json.loads((bundle / 'task.json').read_text())
    kind = task['task_type']
    with np.load(bundle / 'train.npz', allow_pickle=False) as data:
        x, y, ids = data['x'], data['y'], data['ids']
        groups = data.get('groups', None)
    with np.load(bundle / 'validation.npz', allow_pickle=False) as data:
        xv, yv = data['x'], data['y']
    if groups is not None:
        partitions = list(GroupKFold(n_splits=folds, shuffle=True, random_state=partition_seed).split(x, y, groups))
    else:
        cls = StratifiedKFold if kind == 'classification' else KFold
        partitions = list(cls(n_splits=folds, shuffle=True, random_state=partition_seed).split(x, y))
    specs = specs or recipe_specs(kind)
    rows, fitted, validation_predictions, traces, audits = [], {}, {}, [], []
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter('ignore', ConvergenceWarning)
        for spec in specs:
            row = dict(spec=spec)
            fit_started = time.perf_counter()
            try:
                scores, oof = [], np.empty_like(y)
                for train, held in partitions:
                    def factory(features, targets, training, frozen_spec=spec):
                        return FoldWorkflow(kind, frozen_spec, model_seed, pre_cv_transform).fit(features, targets, ids, training)
                    model = factory(x, y, train)
                    # One independent poison replay per recipe (first fold); every fold's
                    # training path is identical. Compare the actual fitted objects.
                    if not scores:
                        audits.append(dict(recipe=spec['id'], passed=check_in_fold(factory, x, y, train, held, fitted=model),
                                           method='held-row poison; fitted pipeline byte equality; first fold'))
                    predictions = model.predict(x[held])
                    oof[held] = predictions
                    scores.append(score(kind, y[held], predictions))
                    traces.append(dict(recipe=spec['id'], train_ids=ids[train].tolist(), held_ids=ids[held].tolist(),
                                       transform_fit_ids=model.transform_fit_ids_))
                fitted[spec['id']] = model = FoldWorkflow(kind, spec, model_seed, pre_cv_transform).fit(x, y, ids, np.arange(len(x)))
                prediction = model.predict(xv)
                validation_predictions[spec['id']] = prediction
                row.update(fold_scores=scores, cv_score=float(np.mean(scores)),
                           validation_score=score(kind, yv, prediction), oof_score=score(kind, y, oof),
                           validation_predictions=prediction.tolist(),
                           oof_digest=hashlib.sha256(oof.tobytes()).hexdigest())
            except (ValueError, np.linalg.LinAlgError) as exc:
                row['error'] = str(exc)
            row['fit_seconds'] = time.perf_counter() - fit_started
            rows.append(row)
        if supplied_baseline is not None:
            baseline_row = select(rows, 'A0')
            with np.load(Path(supplied_baseline) / 'validation_predictions.npz', allow_pickle=False) as data:
                prediction = data['predictions']
            baseline_row.update(validation_score=score(kind, yv, prediction), validation_predictions=prediction.tolist(),
                                cv_score=None, fold_scores=[], oof_score=None, oof_digest=None)
            baseline_row['spec']['supplied'] = True
            validation_predictions[baseline_row['spec']['id']] = prediction
        # Freeze all selections before the controller opens any test labels.
        choices = {pipeline: select(rows, pipeline)['spec']['id'] for pipeline in ('A0', 'A1', 'A2', 'A3')}
        baseline = validation_predictions[choices['A0']]
        with np.load(test_inputs, allow_pickle=False) as data:
            xt, test_ids = data['x'], data['ids']
        with np.load(test_labels, allow_pickle=False) as data:
            if not np.array_equal(test_ids, data['ids']):
                raise ValueError('controller test IDs do not align')
            yt = data['y']
        for row in rows:
            if row.get('error') is not None:
                continue
            if row['spec'].get('supplied'):
                with np.load(Path(supplied_baseline) / 'test_predictions.npz', allow_pickle=False) as data:
                    if not np.array_equal(test_ids, data['ids']):
                        raise ValueError('supplied baseline IDs do not align')
                    prediction = data['predictions']
            else:
                prediction = fitted[row['spec']['id']].predict(xt)
            row.update(test_score=score(kind, yt, prediction), test_se=score_se(kind, yt, prediction),
                       paired_baseline=paired_interval(kind, yv, validation_predictions[row['spec']['id']], baseline))
    return dict(schema='practice-score-table/1', task_type=kind, partition_seed=partition_seed, model_seed=model_seed,
                folds=folds, choices_before_test=choices, rows=rows, in_fold_trace=traces,
                in_fold_ok=bool(audits) and all(a['passed'] for a in audits), in_fold_audits=audits,
                environment={name: importlib.metadata.version(name) for name in ('numpy', 'scikit-learn', 'joblib')},
                comparator_paired=paired_interval(kind, yt, fitted[choices['A3']].predict(xt), fitted[choices['A1']].predict(xt)),
                source_digest=source_digest(), wall_seconds=time.perf_counter() - started)
