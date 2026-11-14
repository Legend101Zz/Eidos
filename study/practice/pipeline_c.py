"""Step-2 pipeline C and rerunnable model-free acceptance, without changing A0–A3.

Only generated practice/public development bundles are accepted. All candidate
choices and replay selections are frozen before controller test labels are opened.
Run --stage design first, then --stage holdout; --stage all does both in that order.
"""
import argparse
import copy
import hashlib
import json
import math
import shutil
import sys
import time
import warnings
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from study.practice.build_cards import ROOT, tree_digest, write_json
from study.practice.guard import check_in_fold, claim_metrics, evaluate, identifier_violations
from study.practice.ledger import register, require_bundle_registration, verify_chain
from study.practice.oracle import paired_interval, score, score_se
from study.practice.pipelines import compute_table, replay, select, source_digest
from study.practice.run_gate import fresh_recipe, semantic_digest, supplied_baseline

COMPONENT = ROOT / 'components/eidos.search.recipe-portfolio'
sys.path.insert(0, str(COMPONENT))
import portfolio  # noqa: E402
from eidos_recipes import cv, export, models  # noqa: E402
from eidos_recipes import select as recipe_select  # noqa: E402
from eidos_recipes.util import canonical  # noqa: E402

CONFIG = dict(mode='portfolio', include='auto', scheme='auto', budget=dict(max_seconds=350),
              partition_seed=81723, per_recipe_timeout_seconds=60)


def merge_config(overrides):
    """RFC 7396 merge into CONFIG, before freezing or spawning workers."""
    if not isinstance(overrides, dict):
        raise ValueError('config overrides must be a JSON object')
    def merge(target, patch):
        target = copy.deepcopy(target) if isinstance(target, dict) else {}
        for key, value in patch.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = merge(target.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
        return target
    return merge(CONFIG, overrides)


def _configure_worker(config):
    global CONFIG
    CONFIG = config


def candidate_digest():
    paths = sorted(p for p in COMPONENT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.json', '.md'))
    digest = hashlib.sha256()
    for p in paths:
        digest.update(p.relative_to(COMPONENT).as_posix().encode() + b'\0' + p.read_bytes())
    digest.update(Path(__file__).read_bytes())
    return hashlib.sha256((digest.hexdigest() + canonical(CONFIG)).encode()).hexdigest()


def freeze(out):
    path = Path(out) / 'frozen-candidate.json'
    value = dict(schema='pipeline-c-freeze/1', candidate_digest=candidate_digest(), library_digest=export.library_digest(),
                 config=CONFIG, paired_multiple=1., complexity='seeded-permutation/81723 of completed export-eligible rows; design-frozen',
                 primary='pipeline C versus A2: named transfer', secondary=['C versus A1', 'selection replay', 'resource diagnostics'],
                 design_band_sweep=[0., .5, 1., 1.5, 2.], holdout_tuning=False)
    if path.exists() and json.loads(path.read_text()) != value:
        raise ValueError('candidate differs from frozen acceptance run; use a new output directory and registered evaluation')
    write_json(path, value)
    return value


def comparator_table(data, out, item, comparator_root=None, *, registration=None):
    bundle, controller = data / 'bundles' / item['card'], data / 'controller' / item['card']
    require_bundle_registration(registration, bundle, test_inputs=controller / 'test_inputs.npz', test_labels=controller / 'test_labels.npz', purpose='comparators')
    key = hashlib.sha256((tree_digest(bundle) + tree_digest(controller) + source_digest()).encode()).hexdigest()
    path = out / 'comparators' / f'{key}.json'
    if path.exists():
        value = json.loads(path.read_text())
        if value['source_digest'] != source_digest():
            raise ValueError('stale comparator source digest')
        return value
    if comparator_root:
        # A per-card content-addressed cache avoids opening a combined report
        # containing protected results during design calibration.
        from study.practice.run_gate import cache_key
        expected = cache_key(bundle, controller)
        cache = comparator_root / 'cache' / f'{expected}.json'
        if cache.exists():
            value = json.loads(cache.read_text())
            if value['source_digest'] == source_digest() and value.get('in_fold_ok') and value.get('cache_key') == expected:
                write_json(path, value)
                return value
    baseline = supplied_baseline(out / 'baselines' / key, bundle, controller, registration=registration)
    value = compute_table(bundle, controller / 'test_inputs.npz', controller / 'test_labels.npz', supplied_baseline=baseline, registration=registration)
    write_json(path, value)
    return value


def replay_choices(rows, *, multiples=(.5, 1., 1.5, 2.)):
    """Freeze all replay choices before a single test score is visible."""
    completed = [r for r in rows if r['status'] == 'ok' and r['export_eligible']]
    rng = np.random.default_rng(81723)
    ranks = dict(zip([r['id'] for r in completed], map(int, rng.permutation(len(completed))), strict=True))
    choices = {'argmax-cv': max(completed, key=lambda r: (r['mean'], r['id']))['id']}
    for multiple in multiples:
        for name, prior in [('fixed', None), ('permuted', ranks)]:
            try:
                choices[f'paired-{multiple}/{name}'] = recipe_select.choose(rows, multiple=multiple, complexity_order=prior)['chosen_id']
            except ValueError:
                choices[f'paired-{multiple}/{name}'] = None
    return choices


def controller_scores(bundle, controller, table, *, design_sweep=False, registration=None):
    """Cached post-selection fits, trusted controller only; never fed back to search."""
    require_bundle_registration(registration, bundle, test_inputs=controller / 'test_inputs.npz', test_labels=controller / 'test_labels.npz')
    with np.load(bundle / 'train.npz', allow_pickle=False) as data:
        x, y = np.asarray(data['x'], dtype=float).reshape(len(data['x']), -1), data['y']
        groups = data.get('groups', None)
    with np.load(bundle / 'validation.npz', allow_pickle=False) as data:
        xv, yv = np.asarray(data['x'], dtype=float).reshape(len(data['x']), -1), data['y']
    # Freeze CV/paired/permutation choices before reading controller test labels.
    choices = replay_choices(table['recipes'], multiples=(.5, 1., 1.5, 2.) if design_sweep else (1.,))
    with np.load(controller / 'test_inputs.npz', allow_pickle=False) as data:
        xt, ids = np.asarray(data['x'], dtype=float).reshape(len(data['x']), -1), data['ids']
    # Validation-argmax is a separate controller replay, never a component choice.
    fitted, rows, audits, audited_paths = {}, [], [], set()
    splits = cv.partitions(x, y, table['task_type'], seed=table['partition_seed'], groups=groups,
                           folds=table.get('cv_folds', 5), repeats=table.get('cv_repeats'))
    if table['scheme'] == 'cv-train' and table.get('folds_digest') and cv.folds_digest(splits) != table['folds_digest']:
        raise ValueError('controller CV partitions differ from portfolio partitions')
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter('ignore')
        for r in table['recipes']:
            if r['status'] != 'ok':
                continue
            started = time.perf_counter()
            training, held = splits[0]
            def factory(features, targets, train, frozen_spec=r['spec']):
                return cv.fit_fold(frozen_spec, features, targets, train, seed=table['model_seed'])
            audit_key = canonical(dict(steps=r['spec']['steps'], family=r['spec']['model']['family']))
            if audit_key not in audited_paths:
                audits.append(dict(recipe=r['id'], passed=check_in_fold(factory, x, y, training, held), coverage='distinct fitted-step sequence and model family; actual fold-fit path'))
                audited_paths.add(audit_key)
            model = models.build(r['spec'], table['model_seed']).fit(x, y)
            pv, pt = model.predict(xv), model.predict(xt)
            cv_result = dict(mean=r['mean'], scores=r['scores']) if table['scheme'] == 'cv-train' else cv.evaluate(r['spec'], x, y, partitions=splits, seed=table['model_seed'])
            row = dict(id=r['id'], spec=r['spec'], selection_score=r['mean'], complexity=r['complexity'],
                       validation_score=score(table['task_type'], yv, pv), predictions=pt.tolist(),
                       cv_score=cv_result['mean'], cv_fold_scores=cv_result['scores'],
                       controller_fit_seconds=time.perf_counter() - started)
            rows.append(row)
            fitted[r['id']] = (pv, pt)
            if r['id'] == table['selection']['chosen_id'] and not np.allclose(pv, table['validation_check']['predictions'], rtol=1e-5, atol=1e-6):
                raise ValueError('controller fit differs from portfolio/export executor')
    choices['argmax-cv'] = max(rows, key=lambda r: (r['cv_score'], r['id']))['id']
    choices['argmax-validation'] = max(rows, key=lambda r: (r['validation_score'], r['id']))['id']
    with np.load(controller / 'test_labels.npz', allow_pickle=False) as data:
        if not np.array_equal(ids, data['ids']):
            raise ValueError('controller test ids do not align')
        truth = data['y']
    for row in rows:
        row['test_score'] = score(table['task_type'], truth, np.asarray(row.pop('predictions')))
    best_test = max(r['test_score'] for r in rows)
    replayed = {}
    for name, rid in choices.items():
        if rid is None:
            replayed[name] = dict(error='no export-eligible recipe in paired band')
            continue
        r = next(r for r in rows if r['id'] == rid)
        estimated = r['cv_score'] if name == 'argmax-cv' else r['validation_score'] if name == 'argmax-validation' or table['scheme'] == 'train-fit-validation-score' else r['selection_score']
        replayed[name] = dict(recipe=rid, test_score=r['test_score'], selection_regret=best_test - r['test_score'],
                             optimism=estimated - r['test_score'])
    chosen_id = table['selection']['chosen_id']
    chosen = next(r for r in rows if r['id'] == chosen_id)
    return dict(rows=rows, in_fold_audits=audits, choices_before_test=choices, replay=replayed, chosen=chosen), fitted[chosen_id], (xt, truth)


def fresh_c(workspace, bundle, controller, source, table, baseline, bar, *, registration=None):
    require_bundle_registration(registration, bundle, test_inputs=controller / 'test_inputs.npz', test_labels=controller / 'test_labels.npz')
    from study.common import research_python
    from study.fresh_check import candidate_command, equal_predictions
    if workspace.exists():
        shutil.rmtree(workspace)
    candidate = workspace / 'candidate'
    candidate.mkdir(parents=True)
    for name, text in source['files'].items():
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    shutil.copytree(bundle, workspace / 'inputs')
    shutil.copyfile(controller / 'test_inputs.npz', workspace / 'test_inputs.npz')
    with np.load(controller / 'test_labels.npz', allow_pickle=False) as data:
        truth = data['y']
    gains, receipts, equal = [], [], True
    try:
        for seed in (11, 23, 37):
            out = workspace / f'seed-{seed}'
            started = time.perf_counter()
            command = [research_python(), str(candidate / 'project.py'), 'train', '--inputs', str(workspace / 'inputs'),
                       '--out', str(out), '--seed', str(seed)]
            candidate_command(workspace, command, timeout=120)
            wall = time.perf_counter() - started
            receipt = json.loads((out / 'receipt.json').read_text())
            receipt.update(seed=seed, evaluator_train_wall_seconds=wall,
                           thread_variables={k: '1' for k in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')})
            for split, inputs in [('validation', workspace / 'inputs/validation.npz'), ('test', workspace / 'test_inputs.npz')]:
                candidate_command(workspace, [research_python(), str(candidate / 'project.py'), 'infer', '--model', str(out / 'model.pt'),
                                             '--inputs', str(inputs), '--out', str(out / f'fresh_{split}.npz')], timeout=120)
            equal = equal and equal_predictions(out / 'validation_predictions.npz', out / 'fresh_validation.npz')
            if seed == 11:
                with np.load(out / 'fresh_validation.npz', allow_pickle=False) as data:
                    equal = equal and bool(np.allclose(data['predictions'], table['validation_check']['predictions'], rtol=1e-5, atol=1e-6))
            with np.load(out / 'fresh_test.npz', allow_pickle=False) as data:
                gains.append(score(table['task_type'], truth, data['predictions']) - baseline)
            receipts.append(receipt)
        return dict(fresh_gains=gains, receipts=receipts, fresh_process_equal=equal,
                    export_eligible=equal and all(r['checkpoint_bytes'] <= 64_000_000 for r in receipts),
                    hard_gate=equal and all(g >= bar for g in gains), practical_bar=bar)
    except (ValueError, OSError) as exc:
        return dict(fresh_gains=gains, receipts=receipts, fresh_process_equal=False, export_eligible=False,
                    hard_gate=False, practical_bar=bar, error=str(exc))


def compute(item, data, out, comparator_root=None, registration=None):
    started = time.perf_counter()
    card = item['card']
    require_bundle_registration(registration, data / 'bundles' / card, test_labels=data / 'controller' / card / 'test_labels.npz')
    result_path = out / 'cards' / f'{card}.json'
    if result_path.exists():
        result = json.loads(result_path.read_text())
        if result['candidate_digest'] != candidate_digest() or result['comparator_source_digest'] != source_digest():
            raise ValueError('stale pipeline C result: use a new evidence output')
        return result
    bundle, controller = data / 'bundles' / card, data / 'controller' / card
    task = json.loads((bundle / 'task.json').read_text())
    comparator = comparator_table(data, out, item, comparator_root, registration=registration)
    baseline = replay(comparator)['pipelines']['A0']['test_score']
    bar = .02 if task['task_type'] == 'classification' else max(abs(baseline) * .05, 1e-12)
    portfolio_path = out / 'portfolio' / f'{card}.json'
    if portfolio_path.exists():
        saved = json.loads(portfolio_path.read_text())
        table, oof, source = saved['table'], saved['oof'], saved['source']
        if table['library_digest'] != export.library_digest():
            raise ValueError('stale portfolio library')
    else:
        ports = {key: bundle / f'{key}.npz' for key in ('train', 'validation')}
        ports['task'] = bundle / 'task.json'
        table, oof, source = portfolio.search(ports, CONFIG, out / 'scratch' / card, seed=11)
        write_json(portfolio_path, dict(table=table, oof=oof, source=source))
    scores, (pv, pt), (xt, truth) = controller_scores(bundle, controller, table, design_sweep=item['role'] == 'design', registration=registration)
    with np.load(bundle / 'validation.npz', allow_pickle=False) as data_values:
        yv = data_values['y']
    baseline_validation = np.asarray(select(comparator['rows'], 'A0')['validation_predictions'])
    visible = paired_interval(task['task_type'], yv, pv, baseline_validation)
    fresh = fresh_c(out / 'executions' / card / 'C', bundle, controller, source, table, baseline, bar, registration=registration)
    pipeline = dict(recipe=table['selection']['chosen_id'], test_score=scores['chosen']['test_score'],
                    selection_regret=max(r['test_score'] for r in scores['rows']) - scores['chosen']['test_score'],
                    optimism=next(r for r in table['recipes'] if r['id'] == table['selection']['chosen_id'])['mean'] - scores['chosen']['test_score'],
                    paired_baseline=visible, visible_claim=visible['lower95'] > 0, **fresh)
    comparisons = {}
    with np.load(bundle / 'train.npz', allow_pickle=False) as data_values:
        train_x, train_y, train_ids = data_values['x'], data_values['y'], data_values['ids']
    with np.load(controller / 'test_inputs.npz', allow_pickle=False) as comparator_inputs:
        comparator_test_x = comparator_inputs['x']
    from study.practice.models import Recipe
    for name in ('A1', 'A2'):
        r = select(comparator['rows'], name)
        with threadpool_limits(limits=1), warnings.catch_warnings():
            warnings.simplefilter('ignore')
            m = Recipe(task['task_type'], r['spec'], 11).fit(train_x, train_y, train_ids)
            pred = m.predict(comparator_test_x)
            if not np.isclose(score(task['task_type'], truth, pred), r['test_score'], rtol=1e-6, atol=1e-8):
                raise ValueError('comparator refit differs from the unchanged cached executor')
        comparisons[name] = paired_interval(task['task_type'], truth, pt, pred)
    a2_fresh = None
    if item['role'] == 'control':
        a2_fresh = fresh_recipe(out / 'executions' / card / 'A2', bundle, controller, select(comparator['rows'], 'A2'), task['task_type'], baseline, bar, registration=registration)
    result = dict(**item, task_type=task['task_type'], pipeline_C=pipeline,
                  comparators=replay(comparator)['pipelines'], paired_test_comparisons=comparisons,
                  A2_fresh=a2_fresh, replay=scores['replay'],
                  controller_score_rows=scores['rows'], in_fold_audits=scores['in_fold_audits'],
                  in_fold_ok=all(a['passed'] for a in scores['in_fold_audits']),
                  coverage_complete=table['coverage_complete'], round1_wall_seconds=table['wall_seconds'],
                  scheme=table['scheme'], stop_reason=table['stop_reason'],
                  failed_recipes=[dict(id=r['id'], error=r.get('error'), status=r['status']) for r in table['recipes'] if r['status'] != 'ok'],
                  lab_fit_seconds=next(r for r in table['recipes'] if r['id'] == table['selection']['chosen_id'])['fit_seconds_train'],
                  source_digest=source['library_digest'], candidate_digest=candidate_digest(),
                  comparator_source_digest=source_digest(), wall_seconds=time.perf_counter() - started)
    write_json(result_path, result)
    print(json.dumps(dict(card=card, generator=item['generator'], role=item['role'], seconds=round(result['wall_seconds'], 2),
                          round1_seconds=round(table['wall_seconds'], 2), coverage=table['coverage_complete'],
                          delta_A2=comparisons['A2']['effect'], fresh_hard_gate=fresh['hard_gate'])), flush=True)
    return result



def control_claims(rows, null_generators):
    """Null cards use D12; weak-signal cards use the practical-bar grader branch."""
    metrics = {}
    for name in ('C', 'A2'):
        groups = {}
        for label, null in [('null', True), ('weak_signal', False)]:
            inputs = []
            for row in rows:
                if (row['generator'] in null_generators) != null:
                    continue
                value = dict(row['pipeline_C']) if name == 'C' else dict(
                    visible_claim=row['comparators']['A2']['paired_baseline']['lower95'] > 0,
                    **row['A2_fresh'])
                inputs.append(value)
            if inputs:
                groups[label] = claim_metrics(inputs, practical_bar=.02, null=null)
        n = sum(group['instances'] for group in groups.values())
        metrics[name] = dict(instances=n, groups=groups, **{
            key: sum(g[key] * g['instances'] for g in groups.values()) / n
            for key in ('visible_claim_rate', 'false_positive_claim_rate', 'verified_success_rate')})
    return metrics

def aggregate(data, out, registration=None):
    index = json.loads((data / 'index.json').read_text())['cards']
    rows = [json.loads((out / 'cards' / f'{r["card"]}.json').read_text()) for r in index if (out / 'cards' / f'{r["card"]}.json').exists()]
    controls = [r for r in rows if r['role'] == 'control']
    mechanisms = json.loads(Path(__file__).with_name('registry.json').read_text())
    metrics = control_claims(controls, {g['id'] for g in mechanisms if g['mechanism'] == 'noise-control'}) if controls else {}
    null_delta = max(
        metrics['C']['groups'][group][key] - metrics['A2']['groups'][group][key]
        for group in metrics['C']['groups'] for key in ('visible_claim_rate', 'false_positive_claim_rate')
    ) if metrics else None
    roles = {r['generator']: r['role'] for r in rows}
    inference = {}
    for name in ('A1', 'A2'):
        effects = {}
        raw = {}
        for r in rows:
            paired = r['paired_test_comparisons'][name]
            effects.setdefault(r['generator'], []).append(paired['effect'] / max(paired['se'], .001))
            raw.setdefault(r['generator'], []).append(paired['effect'])
        if registration and registration['kind'] == 'candidate' and len(rows) == len(index) and all(len(values) >= 2 for values in effects.values()):
            prior_primaries = sum(e['event'] == 'registration' and e.get('kind') == 'candidate'
                                  and e['sequence'] < registration['sequence'] for e in verify_chain())
            inference[name] = dict(evaluate(effects, roles, registration=registration, epsilon=.5, in_fold_ok=all(r['in_fold_ok'] for r in rows),
                       null_delta=null_delta, null_tolerance=0., limits_ok=all(r['pipeline_C']['export_eligible'] for r in rows),
                       primary_pvalues=[1.] * prior_primaries if name == 'A2' else [],
                       effective_instances={g: 1 for g in ('gen-008', 'gen-009', 'gen-015') if g in effects}),
                       effects_raw=raw, effects_test_se=effects,
                       status='primary' if name == 'A2' else 'descriptive secondary',
                       programme_multiplicity='Conservative Holm: unknown earlier programme primary p-values treated as 1.')
    scales = {}
    for r in rows:
        with np.load(data / 'controller' / r['card'] / 'test_labels.npz', allow_pickle=False) as values:
            truth = values['y']
        path = out / 'executions' / r['card'] / 'C/seed-11/fresh_test.npz'
        if path.exists():
            with np.load(path, allow_pickle=False) as values:
                scales[r['card']] = max(score_se(r['task_type'], truth, values['predictions']), .001)
        else:
            scales[r['card']] = None
    replay_metrics = {}
    for subset, eligible in [('all', rows), ('small_n', [r for r in rows if r['generator'] == 'gen-008'])]:
        replay_metrics[subset] = {}
        for name in ('C', 'argmax-validation', 'argmax-cv'):
            values = [r['pipeline_C'] if name == 'C' else r['replay'][name] for r in eligible]
            if values:
                replay_metrics[subset][name] = {key: float(np.mean([v[key] for v in values])) for key in ('selection_regret', 'optimism')}
                replay_metrics[subset][name]['per_task'] = {kind: {key: float(np.mean([v[key] for v, r in zip(values, eligible, strict=True) if r['task_type'] == kind])) for key in ('selection_regret', 'optimism')} for kind in sorted({r['task_type'] for r in eligible})}
                replay_metrics[subset][name]['test_se_units'] = {key: float(np.mean([v[key] / scales[r['card']] for v, r in zip(values, eligible, strict=True) if scales[r['card']] is not None])) for key in ('selection_regret', 'optimism')}
    # Production source cannot contain generator/card ids or oracle field names.
    oracle_fields = set()
    for item in rows:
        oracle_path = data / 'controller' / item['card'] / 'oracle.json'
        if oracle_path.exists():
            oracle_fields.update(json.loads(oracle_path.read_text()).keys())
    identifiers = {r['generator'] for r in index} | {r['card'] for r in index} | oracle_fields | {'N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04'}
    violations = identifier_violations(COMPONENT.rglob('*'), identifiers)
    frozen = json.loads((out / 'frozen-candidate.json').read_text())
    slow = [r['card'] for r in rows if r['round1_wall_seconds'] > 350]
    censored = [r['card'] for r in rows if r['stop_reason'] == 'budget']
    complete = len(rows) == len(index)
    replay_no_worse = all(
        replay_metrics.get('all', {}).get('C', {}).get('test_se_units', {}).get(key, math.inf) <= replay_metrics.get('all', {}).get(name, {}).get('test_se_units', {}).get(key, -math.inf)
        for key in ('selection_regret', 'optimism') for name in ('argmax-validation', 'argmax-cv'))
    criteria = {
        'complete_family': dict(passed=complete, expected=len(index), measured=len(rows)),
        'generator_transfer_A2': dict(passed=inference.get('A2', {}).get('verdict') in ('accept-named', 'accept-class'), verdict=inference.get('A2', {}).get('verdict', 'not measured')),
        'generator_transfer_A1_secondary': dict(passed=inference.get('A1', {}).get('verdict') in ('accept-named', 'accept-class'), verdict=inference.get('A1', {}).get('verdict', 'not measured')),
        'selection_regret_and_optimism': dict(passed=replay_no_worse, measurements=replay_metrics, interpretation='descriptive means in each card chosen-score test SE units (floor .001), plus raw and per-task means; no new hypothesis-test primary'),
        'null_claim_metrics_no_worse_A2': dict(passed=null_delta is not None and null_delta <= 0, measurements=metrics),
        'fresh_seed_gate_measured': dict(passed=bool(rows) and all(len(r['pipeline_C']['fresh_gains']) == 3 for r in rows),
                                        passes=sum(r['pipeline_C']['hard_gate'] for r in rows), total=len(rows),
                                        rate=sum(r['pipeline_C']['hard_gate'] for r in rows) / len(rows) if rows else 0.),
        'export_eligibility': dict(passed=bool(rows) and all(r['pipeline_C']['export_eligible'] for r in rows),
                                   passes=sum(r['pipeline_C']['export_eligible'] for r in rows), total=len(rows)),
        'round1_coverage': dict(passed=bool(rows) and all(r['coverage_complete'] for r in rows),
                                passes=sum(r['coverage_complete'] for r in rows), total=len(rows)),
        'round1_wall_measured': dict(passed=bool(rows), above_350_seconds=slow, budget_censored=censored,
             halving_reopen_trigger=len(slow) > len(rows) * .1,
             halving_trigger_assessable=not censored, interpretation='A budget-stopped grid is censored; a capped wall cannot establish its uncapped completion time.'),
        'evaluator_and_lab_clock_measured': dict(passed=bool(rows) and all(r['pipeline_C']['receipts'] for r in rows),
             measurements=[dict(card=r['card'], lab_fit_seconds=r['lab_fit_seconds'],
                evaluator=[dict(seed=s['seed'], fit_seconds=s['fit_seconds'], cold_wall_seconds=s['evaluator_train_wall_seconds'], checkpoint_bytes=s['checkpoint_bytes']) for s in r['pipeline_C']['receipts']]) for r in rows]),
        'in_fold_audits': dict(passed=bool(rows) and all(r['in_fold_ok'] for r in rows), recipes=sum(len(r['in_fold_audits']) for r in rows)),
        'identifier_lint': dict(passed=not violations, violations=violations),
    }
    result = dict(schema='v2-step2-acceptance/1', all_met=all(c['passed'] for c in criteria.values()), criteria=criteria,
                  generator_inference=inference, model_calls=0, provider_calls=0, frozen=frozen,
                  guard_source_digest=hashlib.sha256(Path(__file__).with_name('guard.py').read_bytes()).hexdigest(),
                  harness_source_digest=source_digest(), preregistration=registration,
                  design_replay_sweep={r['card']: r['replay'] for r in rows if r['role'] == 'design'},
                  cards=rows, data_digest=tree_digest(data),
                  limitation='Offline practice measurements only. Current base guard used unchanged. One primary C-vs-A2 verdict; A1/replays descriptive. No held-out study or investigator claim. Holdout-class results are named diagnostics, not a class-level claim.')
    result['semantic_digest'] = semantic_digest(result)
    write_json(out / 'acceptance.json', result)
    lines = ['# V2 step 2 acceptance', '', f"Measured {len(rows)}/{len(index)} practice cards; model/provider calls: 0.", '']
    lines += [f"- {'PASS' if c['passed'] else 'NOT MET'} — {name}" for name, c in criteria.items()]
    lines += ['', f"C vs A2 primary: {inference.get('A2', {}).get('verdict', 'not measured')}.",
              f"Fresh-seed hard gates: {criteria['fresh_seed_gate_measured']['passes']}/{len(rows)}; export eligible: {criteria['export_eligibility']['passes']}/{len(rows)}.",
              f"Round 1 >350s: {len(slow)}/{len(rows)}; halving trigger: {criteria['round1_wall_measured']['halving_reopen_trigger']}.", '', result['limitation']]
    (out / 'SUMMARY.md').write_text('\n'.join(lines) + '\n')
    return result


def run(data, out, *, stage='all', comparator_root=None, evaluation_id='v2-step2-c-v1', workers=1):
    data, out = Path(data).resolve(), Path(out).resolve()
    if data.is_relative_to(ROOT) or out.is_relative_to(ROOT):
        raise ValueError('practice data and acceptance evidence must live outside the repository')
    # Only the neutral generated family; no registry discovery or arbitrary card paths.
    index = json.loads((data / 'index.json').read_text())['cards']
    if any(not r['card'].startswith('P') or not r['card'][1:].isdigit() for r in index):
        raise ValueError('pipeline C acceptance requires generated practice bundles')
    out.mkdir(parents=True, exist_ok=True)
    frozen = freeze(out)
    design = [r for r in index if r['role'] in ('design', 'calibration-only')]
    rest = [r for r in index if r not in design]
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('use one to four sequential one-thread card workers')
    def score_items(items, scoring_data, receipt):
        from concurrent.futures import ProcessPoolExecutor
        if workers == 1:
            for item in items:
                compute(item, scoring_data, out, Path(comparator_root) if comparator_root else None, receipt)
        else:
            with ProcessPoolExecutor(max_workers=workers, initializer=_configure_worker, initargs=(CONFIG,)) as pool:
                futures = [pool.submit(compute, item, scoring_data, out, Path(comparator_root) if comparator_root else None, receipt) for item in items]
                for future in futures:
                    future.result()
    registration = None
    if stage in ('design', 'all'):
        design_data = out / 'design-data'
        if not (design_data / 'index.json').exists():
            for item in design:
                for part in ('bundles', 'controller'):
                    shutil.copytree(data / part / item['card'], design_data / part / item['card'])
            write_json(design_data / 'index.json', dict(schema='practice-index/2', cards=design))
        construction = register(evaluation_id=evaluation_id + '-design', candidate_digest=frozen['candidate_digest'],
            classes=sorted({r['generator'] for r in design}), primary_verdict='design-only descriptive calibration',
            kind='construction', data_root=design_data)
        write_json(out / 'design-registration.json', construction)
        score_items(design, design_data, construction)
    if stage in ('holdout', 'all'):
        if not all((out / 'cards' / f'{r["card"]}.json').exists() for r in design):
            raise ValueError('finish design scoring before frozen holdout evaluation')
        registration = register(evaluation_id=evaluation_id, candidate_digest=frozen['candidate_digest'],
            classes=sorted({r['generator'] for r in index}), primary_verdict=frozen['primary'],
            kind='candidate', data_root=data)
        write_json(out / 'preregistration.json', registration)
        score_items(rest, data, registration)
    return aggregate(data, out, registration)



def development(data, out, *, comparator_root=None, workers=1, evaluation_id='v2-step2-c-r2-development'):
    """Descriptive frozen-candidate checks on the seven explicitly public cards."""
    from concurrent.futures import ProcessPoolExecutor
    data, out = Path(data).resolve(), Path(out).resolve()
    if data.is_relative_to(ROOT) or out.is_relative_to(ROOT):
        raise ValueError('development evidence must live outside the repository')
    index = json.loads((data / 'index.json').read_text())['cards']
    if {r['card'] for r in index} - {'N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04'}:
        raise ValueError('only the seven public development cards are allowed')
    frozen = freeze(out)
    receipt = register(evaluation_id=evaluation_id, candidate_digest=frozen['candidate_digest'],
        classes=sorted({r['generator'] for r in index}), primary_verdict='public calibration descriptive', kind='construction', data_root=data)
    with ProcessPoolExecutor(max_workers=workers, initializer=_configure_worker, initargs=(CONFIG,)) as pool:
        rows = list(pool.map(compute, index, [data] * len(index), [out] * len(index),
                             [Path(comparator_root) if comparator_root else None] * len(index), [receipt] * len(index)))
    result = dict(schema='pipeline-c-public-development/1', cards=rows, model_calls=0, provider_calls=0,
                  candidate_digest=candidate_digest(),
                  interpretation='Descriptive public calibration only; fresh gains use the practice raw-metric bar, not a study verdict.')
    write_json(out / 'development.json', result)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--comparators', type=Path)
    parser.add_argument('--stage', choices=('design', 'holdout', 'all', 'development'), default='all')
    parser.add_argument('--evaluation-id')
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--config-overrides', type=json.loads, default={})
    args = parser.parse_args()
    global CONFIG
    CONFIG = merge_config(args.config_overrides)
    evaluation_id = args.evaluation_id or ('v2-step2-c-r2-development' if args.stage == 'development' else 'v2-step2-c-v1')
    if args.stage == 'development':
        result = development(args.data, args.out, comparator_root=args.comparators, workers=args.workers, evaluation_id=evaluation_id)
        print(json.dumps(dict(development_cards=len(result['cards']))))
        return
    result = run(args.data, args.out, stage=args.stage, comparator_root=args.comparators, evaluation_id=evaluation_id, workers=args.workers)
    print(json.dumps(dict(all_met=result['all_met'], cards=len(result['cards']))))


if __name__ == '__main__':
    main()
