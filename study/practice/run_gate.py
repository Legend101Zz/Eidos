"""Offline practice gate and deterministic cached selection replay.

Run as a module. Candidate execution uses the ordinary evaluator's no-network
candidate_command, never a model provider or a running lab.
"""
import argparse
import hashlib
import importlib.metadata
import json
import shutil
import time
from pathlib import Path

import numpy as np

from study.practice.build_cards import ROOT, tree_digest, write_json, write_npz
from study.practice.guard import check_fit_trace, claim_metrics, evaluate, guard_selftest
from study.practice.ledger import register, require_bundle_registration, require_registration
from study.practice.oracle import score
from study.practice.pipelines import compute_table, select, source_digest
from study.practice.selection_replay import replay


def package_development(out, cards=('N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04')):
    """Explicit public allowlist; no study/private registry or sealed source discovery."""
    if Path(out).resolve().is_relative_to(ROOT):
        raise ValueError('development bundles must live outside repository')
    if set(cards) - {'N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04'}:
        raise ValueError('only public development calibration cards are allowed')
    import importlib.util

    from study.packaging.p3_data import dataset as prospective
    path = ROOT / 'research/public/development/data.py'
    spec = importlib.util.spec_from_file_location('_practice_public_data', path)
    public = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(public)
    index = []
    for card in cards:
        if card.startswith('N'):
            data, kind, classes = prospective(card)
        else:
            project = {'D02': 'digits', 'D03': 'digits_shift', 'D04': 'conditioning'}[card]
            data, kind, classes = public.dataset(project)
        bundle = Path(out) / 'bundles' / card
        controller = Path(out) / 'controller' / card
        for split in ('train', 'validation'):
            write_npz(bundle / f'{split}.npz', **dict(zip(('x', 'y', 'ids'), data[split], strict=False)))
        write_npz(controller / 'test_inputs.npz', x=data['test'][0], ids=data['test'][2])
        write_npz(controller / 'test_labels.npz', y=data['test'][1], ids=data['test'][2])
        project = {'N01': 'breast_cancer', 'N02': 'diabetes', 'N03': 'friedman1', 'N04': 'classification',
                   'D02': 'digits', 'D03': 'digits_shift', 'D04': 'conditioning'}[card]
        write_json(bundle / 'task.json', dict(task_id=card, task_type=kind, nclasses=classes, project=project))
        baseline = ROOT / ('research/public/development/agent_project.py' if card == 'D02' else 'study/packaging/baseline_project.py')
        shutil.copyfile(baseline, bundle / 'project.py')
        index.append(dict(card=card, generator=card, instance=0, role='calibration-only',
                          bundle_digest=tree_digest(bundle), controller_digest=tree_digest(controller)))
    write_json(Path(out) / 'index.json', dict(schema='practice-index/1', cards=index))


def cache_key(bundle, controller, specs=None):
    payload = dict(bundle_digest=tree_digest(bundle), controller_digest=tree_digest(controller),
                   source_digest=source_digest(), specs=specs, model_seed=11, partition_seed=81723, folds=5)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def semantic_digest(value):
    """Exclude measured timings, keep choices, scores, identities and all checks."""
    def stable(v):
        if isinstance(v, dict):
            return {k: stable(x) for k, x in v.items() if not k.endswith('seconds') and k not in ('cache_hit', 'path', 'ledger_event_hash')}
        if isinstance(v, list):
            return [stable(x) for x in v]
        return v
    return hashlib.sha256(json.dumps(stable(value), sort_keys=True, allow_nan=False).encode()).hexdigest()


def policy_digest():
    """Audit provenance for reports; candidate registrations have a separate stable identity."""
    paths = [Path(__file__).with_name(name) for name in ('run_gate.py', 'guard.py', 'selection_replay.py', 'acceptance.py', 'operating.py',
                                                                  'controls.py', 'references.py', 'independence.py', 'recompute.py', 'ledger.py', 'generators.py')]
    return hashlib.sha256(source_digest().encode() + b''.join(p.read_bytes() for p in paths)).hexdigest()


def require_bundle_receipt(registration, bundle):
    """Bind every scoring entry to the actual indexed bundle, not just any ID."""
    return require_bundle_registration(registration, bundle)


def fresh_recipe(workspace, bundle, controller, row, task_type, baseline_score, practical_bar, *, registration=None):
    require_bundle_registration(registration, bundle, test_labels=controller / 'test_labels.npz', test_inputs=controller / 'test_inputs.npz')
    from study.common import research_python
    from study.fresh_check import candidate_command, equal_predictions
    candidate = workspace / 'candidate'
    inputs = workspace / 'inputs'
    candidate.mkdir(parents=True)
    shutil.copytree(bundle, inputs)
    # Test inputs have no labels. The candidate sandbox cannot read the controller's labels/oracle.
    shutil.copyfile(controller / 'test_inputs.npz', workspace / 'test_inputs.npz')
    if row['spec'].get('supplied'):
        shutil.copyfile(bundle / 'project.py', candidate / 'project.py')
    else:
        for source, target in [('models.py', 'models.py'), ('export_project.py', 'project.py')]:
            shutil.copyfile(Path(__file__).with_name(source), candidate / target)
        shutil.copyfile(ROOT / 'components/eidos.search.recipe-sweep/templates/fit_recipe.py', candidate / 'legacy.py')
        write_json(candidate / 'recipe.json', row['spec'])
    with np.load(controller / 'test_labels.npz', allow_pickle=False) as data:
        truth = data['y']
    gains, receipts = [], []
    equal = True
    started = time.perf_counter()
    try:
        for seed in (11, 23, 37):
            destination = workspace / f'seed-{seed}'
            command = [research_python(), str(candidate / 'project.py'), 'train', '--inputs', str(inputs),
                       '--out', str(destination), '--seed', str(seed)]
            train_started = time.perf_counter()
            candidate_command(workspace, command, timeout=120)
            train_wall_seconds = time.perf_counter() - train_started
            receipt_path = destination / 'receipt.json'
            receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {
                'fit_seconds': None, 'receipt_source': 'evaluator measurement; supplied project emits no receipt'}
            receipt['seed'] = seed
            receipt['train_wall_seconds'] = train_wall_seconds
            receipt['checkpoint_bytes'] = (destination / 'model.pt').stat().st_size
            receipt['fit_seconds'] = receipt.get('fit_seconds', receipt.get('elapsed_seconds'))
            reloaded = destination / 'fresh_validation.npz'
            output = destination / 'test_predictions.npz'
            for data_path, prediction_path in [(inputs / 'validation.npz', reloaded), (workspace / 'test_inputs.npz', output)]:
                candidate_command(workspace, [research_python(), str(candidate / 'project.py'), 'infer', '--model',
                                              str(destination / 'model.pt'), '--inputs', str(data_path), '--out', str(prediction_path)], timeout=120)
            equal = equal and equal_predictions(destination / 'validation_predictions.npz', reloaded)
            if seed == 11:
                with np.load(reloaded, allow_pickle=False) as data:
                    equal = equal and bool(np.allclose(data['predictions'], row['validation_predictions'], rtol=1e-5, atol=1e-6))
            repeated = destination / 'fresh_test_predictions.npz'
            candidate_command(workspace, [research_python(), str(candidate / 'project.py'), 'infer', '--model',
                                          str(destination / 'model.pt'), '--inputs', str(workspace / 'test_inputs.npz'),
                                          '--out', str(repeated)], timeout=120)
            equal = equal and equal_predictions(output, repeated)
            with np.load(output, allow_pickle=False) as data:
                gains.append(score(task_type, truth, data['predictions']) - baseline_score)
            receipts.append(receipt)
        eligible = equal and all(r['checkpoint_bytes'] <= 64_000_000 for r in receipts)
        return dict(fresh_gains=gains, fresh_process_equal=equal, export_eligible=eligible, receipts=receipts,
                    practical_bar=practical_bar, hard_gate=eligible and all(g >= practical_bar for g in gains),
                    wall_seconds=time.perf_counter() - started)
    except (ValueError, OSError) as exc:
        return dict(fresh_gains=gains, fresh_process_equal=False, export_eligible=False, receipts=receipts,
                    practical_bar=practical_bar, hard_gate=False, error=str(exc), wall_seconds=time.perf_counter() - started)


def supplied_baseline(workspace, bundle, controller, *, registration=None):
    """Execute the functioning supplied A0 through the same no-network evaluator command."""
    require_bundle_registration(registration, bundle, test_labels=controller / 'test_labels.npz', test_inputs=controller / 'test_inputs.npz')
    from study.common import research_python
    from study.fresh_check import candidate_command
    if workspace.exists():
        shutil.rmtree(workspace)
    (workspace / 'candidate').mkdir(parents=True)
    shutil.copytree(bundle, workspace / 'inputs')
    shutil.copyfile(bundle / 'project.py', workspace / 'candidate/project.py')
    shutil.copyfile(controller / 'test_inputs.npz', workspace / 'test_inputs.npz')
    output = workspace / 'output'
    candidate_command(workspace, [research_python(), str(workspace / 'candidate/project.py'), 'train', '--inputs',
                                  str(workspace / 'inputs'), '--out', str(output), '--seed', '11'], timeout=120)
    candidate_command(workspace, [research_python(), str(workspace / 'candidate/project.py'), 'infer', '--model',
                                  str(output / 'model.pt'), '--inputs', str(workspace / 'test_inputs.npz'),
                                  '--out', str(output / 'test_predictions.npz')], timeout=120)
    return output


def effect_tables(results):
    """Normalize each matched instance with its own paired test SE."""
    effects, raw, roles = {}, {}, {}
    for result in results:
        generator = result['generator']
        roles[generator] = result['role']
        delta = result['pipelines']['A3']['test_score'] - result['pipelines']['A1']['test_score']
        raw.setdefault(generator, []).append(delta)
        scale = max(result['paired_test_comparison']['se'], .001)
        effects.setdefault(generator, []).append(delta / scale)
    return effects, raw, roles


def run(data_root, out, *, fresh=False, specs=None, evaluation_id=None, candidate_digest=None, kind="candidate",
        registration=None, card_ids=None):
    if not evaluation_id or not candidate_digest:
        raise ValueError("every scoring run requires an evaluation ID and frozen candidate digest")
    data_root, out = Path(data_root).resolve(), Path(out).resolve()
    if out.is_relative_to(ROOT):
        raise ValueError('gate evidence must live outside repository')
    out.mkdir(parents=True, exist_ok=True)
    index = json.loads((data_root / 'index.json').read_text())['cards']
    environment = {name: importlib.metadata.version(name) for name in ('numpy', 'scikit-learn', 'joblib')}
    for item in index:
        lock = data_root / 'bundles' / item['card'] / 'requirements.lock'
        if lock.exists():
            for requirement in lock.read_text().splitlines():
                name, version = requirement.split('==')
                if environment.get(name) != version:
                    raise ValueError(f'practice gate requires {requirement}; current interpreter has {environment.get(name)}')
    classes = sorted({item['generator'] for item in index})
    if registration is None:
        registration = register(evaluation_id=evaluation_id, candidate_digest=candidate_digest, classes=classes,
                                primary_verdict='A3 versus A1 comparator calibration' if kind == 'construction' else 'candidate versus A2: named transfer',
                                kind=kind, data_root=data_root)
    else:
        require_registration(registration, classes=classes, exact=True)
        if (registration['evaluation_id'], registration['candidate_digest'], registration['kind']) != (evaluation_id, candidate_digest, kind):
            raise ValueError('run identity differs from registration')
    if card_ids is not None:
        if set(card_ids) - {r['card'] for r in index}:
            raise ValueError('unknown card selection')
        index = [r for r in index if r['card'] in card_ids]
    results = []
    for item in index:
        require_registration(registration, classes=[item["generator"]])
        started = time.perf_counter()
        card = item['card']
        bundle, controller = data_root / 'bundles' / card, data_root / 'controller' / card
        require_bundle_receipt(registration, bundle)
        key = cache_key(bundle, controller, specs)
        cache = out / 'cache' / f'{key}.json'
        hit = cache.exists()
        if hit:
            table = json.loads(cache.read_text())
            if table['cache_key'] != key or table['source_digest'] != source_digest():
                raise ValueError('stale or mismatched cache')
            if not table.get('in_fold_ok') or not table.get('in_fold_audits') or not all(a['passed'] for a in table['in_fold_audits']):
                raise ValueError('cache has no successful behavioural in-fold audit')
        else:
            baseline_started = time.perf_counter()
            baseline = supplied_baseline(out / 'baselines' / key, bundle, controller, registration=registration)
            baseline_seconds = time.perf_counter() - baseline_started
            table = compute_table(bundle, controller / 'test_inputs.npz', controller / 'test_labels.npz', specs=specs,
                                  supplied_baseline=baseline, registration=registration)
            table['baseline_wall_seconds'] = baseline_seconds
            if not table['in_fold_ok'] or not check_fit_trace(table['in_fold_trace']):
                raise ValueError('learned preprocessing crossed a CV fold')
            # Retain compact proof summaries; full row IDs are checked before serialization.
            table['in_fold_trace'] = [dict(recipe=t['recipe'], train_rows=len(t['train_ids']), held_rows=len(t['held_ids']),
                                           fit_ids_digest=hashlib.sha256(json.dumps(t['transform_fit_ids']).encode()).hexdigest(),
                                           in_fold=True) for t in table['in_fold_trace']]
            table['cache_key'] = key
            write_json(cache, table)
        comparison = replay(table)
        for name, pipeline in comparison['pipelines'].items():
            pipeline['test_se'] = select(table['rows'], name)['test_se']
        baseline = comparison['pipelines']['A0']['test_score']
        bar = 0.02 if table['task_type'] == 'classification' else max(abs(baseline) * 0.05, 1e-12)
        if fresh:
            export_digest = hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes()
                                                    for name in ('models.py', 'export_project.py'))).hexdigest()
            for name, pipeline in comparison['pipelines'].items():
                evidence_path = out / 'fresh' / key / export_digest / f'{name}.json'
                if evidence_path.exists():
                    evidence = json.loads(evidence_path.read_text())
                else:
                    evidence = None
                if evidence is None or 'error' in evidence:
                    workspace = out / 'executions' / key / name
                    if workspace.exists():
                        shutil.rmtree(workspace)
                    evidence = fresh_recipe(workspace, bundle, controller, select(table['rows'], name), table['task_type'], baseline, bar, registration=registration)
                    write_json(evidence_path, evidence)
                pipeline.update(evidence)
        if not hit:
            table['instance_wall_seconds'] = time.perf_counter() - started
            write_json(cache, table)
        result = dict(**item, **comparison, cache_key=key, cache_hit=hit, in_fold=True,
                      recipe_failures=sum('error' in row for row in table['rows']),
                      paired_test_comparison=table['comparator_paired'],
                      score_table_seconds=table['wall_seconds'],
                      measurement_seconds=table.get('instance_wall_seconds', table['wall_seconds'] + table.get('baseline_wall_seconds', 0)),
                      wall_seconds=time.perf_counter() - started)
        results.append(result)
        write_json(out / 'progress.json', dict(completed=len(results), total=len(index), latest=card))
        print(json.dumps(dict(card=card, completed=len(results), total=len(index), cache_hit=hit,
                              seconds=round(result['wall_seconds'], 3),
                              gap=result['pipelines']['A3']['test_score'] - result['pipelines']['A1']['test_score'])), flush=True)
    metrics = {}
    controls = [r for r in results if r['role'] == 'control']
    if fresh and controls:
        for name in ('A0', 'A1', 'A2', 'A3'):
            metrics[name] = claim_metrics([dict(visible_claim=r['pipelines'][name]['paired_baseline']['lower95'] > 0,
                                                **r['pipelines'][name]) for r in controls], practical_bar=0.02, null=True)
    effects, raw_effects, roles = effect_tables(results)
    # A3–A1 is descriptive comparator calibration, never acceptance of pipeline C.
    complete_candidate = (kind == 'candidate' and set(roles) == set(registration['classes'])
                          and all(len(values) >= 2 for values in effects.values()))
    diagnostic = evaluate(effects, roles, registration=registration, epsilon=.5,
                      in_fold_ok=all(r['in_fold'] for r in results), null_delta=(metrics['A3']['false_positive_claim_rate'] - metrics['A2']['false_positive_claim_rate']) if metrics else None,
                      null_tolerance=0., limits_ok=all(r['measurement_seconds'] <= 840 for r in results),
                      effective_instances={g: 1 for g in ('gen-008', 'gen-009', 'gen-015') if g in effects}) if complete_candidate else dict(status='construction-or-partial; no verdict')
    report = dict(schema='practice-gate/1', cards=results, claim_metrics=metrics,
                  guard_selftest=guard_selftest(registration=registration) if complete_candidate else {},
                  comparator_guard_descriptive=diagnostic,
                  comparator_effects_raw=raw_effects, comparator_effects_test_se=effects,
                  pipeline_C='not implemented in step 1; no C acceptance verdict', fresh_checks_executed=fresh,
                  preregistration=registration,
                  data_digest=tree_digest(data_root), source_digest=source_digest())
    report['environment'] = environment
    report['policy_digest'] = policy_digest()
    report['semantic_digest'] = semantic_digest(report)
    write_json(out / 'gate.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--evaluation-id', required=True)
    parser.add_argument('--candidate-digest', required=True)
    parser.add_argument('--kind', choices=('candidate', 'construction'), default='candidate')
    parser.add_argument('--fresh', action='store_true', help='execute all three evaluator seeds for each selected comparator')
    parser.add_argument('--package-development', action='store_true')
    args = parser.parse_args()
    if args.package_development:
        package_development(args.data)
    report = run(args.data, args.out, fresh=args.fresh, evaluation_id=args.evaluation_id, candidate_digest=args.candidate_digest, kind=args.kind)
    print(json.dumps(dict(semantic_digest=report['semantic_digest'], cards=len(report['cards']))))


if __name__ == '__main__':
    main()
