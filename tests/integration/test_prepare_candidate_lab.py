"""Real PostgreSQL jobs and investigator-authored fixture locks; no provider calls."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import test_recipe_portfolio_lab as portfolio_lab
from temporalio.testing import ActivityEnvironment
from test_command_path import _publish
from test_diagnose_and_template_sweep_lab import ROOT, _bundle
from test_turn_recovery import ShimClient

from eidos.cli.main import bundle_dir
from eidos.domain.promotion import PORTFOLIO_REF, suggested_lock
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.context import POLICY_V4, pack
from eidos.investigator.runtime import fixture_policy

pytestmark = pytest.mark.pg
scene = portfolio_lab.scene
COMMAND = 'eidos.project.command@1.0.9'


@fixture_policy('prepare-candidate-step3')
def fixture(req):
    if req.turn_so_far:
        return dict(rationale='wait for admitted work', calls=[], control=dict(action='wait_for_jobs'))
    view = req.view
    body = view['investigation']['body']
    kind = body['task']['task_type']
    spec = dict(schema='recipe-spec/1', task_type=kind, steps=[dict(op='standardize')],
                model=dict(family='logistic' if kind == 'classification' else 'ridge', params={}))
    locked = any(n.get('kind') == 'candidate_lock' for n in view.get('pinned_notes', []))
    candidate = suggested_lock(view)
    if locked:
        calls = [dict(tool='run_component', args=dict(component_ref=PORTFOLIO_REF, environment='numpy',
            inputs=body['inputs'], config=dict(mode='refine', task_type=kind, add=[spec], scheme='cv-train', cv_folds=2,
                                              cv_repeats=1, budget=dict(max_seconds=60))))]
    elif candidate:
        lines = pack(view, [], policy=POLICY_V4)['text'].splitlines()
        label = next(i for i, line in enumerate(lines) if line.startswith('- Prepared lock text ('))
        calls = [dict(tool='record_note', args=dict(kind='candidate_lock', text=lines[label + 1]))]
    else:
        calls = [dict(tool='run_component', args=dict(component_ref=PORTFOLIO_REF, environment='numpy', inputs=body['inputs'],
            config=dict(mode='portfolio', task_type=kind, include=['linear', 'neighbors'], scheme='cv-train', cv_folds=2,
                        cv_repeats=1, budget=dict(max_seconds=60)))),
                 dict(tool='prepare_candidate', args=dict(source={'$ref': 'call[0].outputs.promotion_source'}, seeds=[11, 23]))]
    return dict(rationale='fixture investigator chooses independently from recorded evidence', calls=calls,
                control=dict(action='wait_for_jobs'))


def setup_scene(scene, kind, *, patience_rounds=1):
    lab, pid = scene['lab'], scene['pid']
    name = 'eidos.search.recipe-portfolio'
    _publish(lab, pid, *_bundle(name), bundle_bytes=bundle_dir(ROOT / 'components' / name))
    directory = ROOT / 'study/eidos_arms/components' / COMMAND
    raw = bundle_dir(directory)
    files = json.loads(raw)['files']
    _publish(lab, pid, json.loads(files.pop('eidos-component.json')), files, bundle_bytes=raw)
    charter = lab.cmd(lab.admin, 'commit_study_charter', dict(charter=dict(study_id=pid, question='q', exploration_only=True)), project=pid)['result']['charter_id']
    inv = lab.cmd(lab.admin, 'start_investigation', dict(charter_id=charter, limits=dict(model_tokens=1e6, compute_seconds=100000, trials=1000), question='prepare a practice candidate', environment='numpy',
        inputs=scene['arts'], task=dict(task_type=kind), investigator=dict(runtime='fixture', context_policy='context-policy/4',
            wall_seconds=1800, max_calls_per_turn=6, max_model_calls_per_turn=2, settings=dict(policy='prepare-candidate-step3'),
            exploration=dict(policy='exploration-policy/3', project_command_ref=COMMAND, patience_rounds=patience_rounds))), project=pid)['result']['investigation_id']
    client = ShimClient(lab, lab.admin)
    acts = InvestigatorActivities(client, client)
    acts.batch_wait_slice, acts.batch_poll = .01, .001
    # Baseline is investigator-designated, using the same ordinary component interface.
    spec = dict(schema='recipe-spec/1', task_type=kind, steps=[dict(op='standardize')],
                model=dict(family='logistic' if kind == 'classification' else 'ridge', params={}))
    import sys
    sys.path.insert(0, str(ROOT / 'components' / name))
    from eidos_recipes.export import source_bundle
    src = client.upload(pid, data=json.dumps(source_bundle(spec, basis='explicit-export')).encode(), schema='source-bundle/1')['result']['artifact_id']
    job_id = client.command('run_component', dict(component_ref=COMMAND, environment='numpy', investigation_id=inv,
        inputs=dict(source=src, **scene['arts']), config=dict(argv=['python', 'project.py', 'train', '--inputs', '{work}', '--out', '{out}', '--seed', '{seed}'], seed=11)), project=pid)['result']['job_id']
    scene['runner'].run_job(job_id, pid, activity_attempt=1)
    baseline = client.query('get_job', dict(job_id=job_id), project=pid)
    assert baseline['status'] == 'succeeded', baseline.get('terminal')
    client.command('record_note', dict(investigation_id=inv, kind='baseline', text=json.dumps(dict(job_id=job_id))), project=pid)
    return inv, acts, client


def run_queued(scene, inv):
    jobs = scene['lab'].q(scene['lab'].admin, 'list_jobs', dict(investigation_id=inv), project=scene['pid'])
    for job in jobs:
        if job['status'] in ('admitted', 'queued'):
            scene['runner'].run_job(job['id'], scene['pid'], activity_attempt=1)


def fixture_turn(scene, inv, acts, index):
    env = ActivityEnvironment()
    base = dict(project_id=scene['pid'], investigation_id=inv, index=index)
    begun = env.run(acts.begin_turn, base)
    base['turn_id'] = begun['turn_id']
    env.run(acts.build_context, base)
    decision = env.run(acts.decide, dict(base, slot=0))
    assert decision['accepted'], decision
    for continuation in range(12):
        result = env.run(acts.execute_decision, dict(base, decision_id=decision['decision_id'], continuation=continuation))
        run_queued(scene, inv)
        if not result.get('pending'):
            assert all(s['ok'] for s in result['summary']), result
            break
    else:
        pytest.fail('preparation did not complete')
    env.run(acts.complete_turn, dict(base, stop_reason='waiting', control=dict(action='wait_for_jobs')))
    return decision, client_view(scene, inv)


def client_view(scene, inv):
    lab, pid = scene['lab'], scene['pid']
    horizon = lab.q(lab.admin, 'list_events', {}, project=pid)['horizon']
    return lab.q(lab.admin, 'investigation_view', dict(investigation_id=inv, horizon=horizon), project=pid)


@pytest.mark.parametrize('kind', ['classification', 'regression'])
def test_prepare_lock_reproduce_and_refine_patience_on_real_ledger(scene, kind):
    inv, acts, client = setup_scene(scene, kind)
    decision, view = fixture_turn(scene, inv, acts, 1)
    prepared = suggested_lock(view)
    assert prepared['readiness']['readiness_ok'] == 1
    assert prepared['paired']['paired_n'] > 0
    assert prepared['reproduction']['reproduced_by']
    turn = client.query('get_turn', dict(turn_id=view['turns'][-1]['id']), project=scene['pid'])
    saved = next(d['body'] for d in turn['decisions'] if d['id'] == decision['decision_id'])
    assert saved['proposed_calls'][-1]['tool'] == 'prepare_candidate'
    assert all(c['macro']['proposed_by'] == decision['decision_id'] for c in saved['calls'][1:])
    assert not any(n['kind'] == 'candidate_lock' for n in view['pinned_notes'])
    _, locked = fixture_turn(scene, inv, acts, 2)
    assert any(n['kind'] == 'candidate_lock' for n in locked['pinned_notes'])
    fixture_turn(scene, inv, acts, 3)
    begun = ActivityEnvironment().run(acts.begin_turn, dict(project_id=scene['pid'], investigation_id=inv, index=4))
    state = client.query('get_turn', dict(turn_id=begun['turn_id']), project=scene['pid'])['spec']['exploration']
    assert state['rounds_without_improvement'] == 1
    assert state['stop'] == 'ceiling-no-improvement'
    assert state['incumbent']['reproduced_by']


def test_author_source_includes_pinned_library_and_refuses_collisions(scene):
    inv, acts, client = setup_scene(scene, 'classification')
    env = ActivityEnvironment()
    base = dict(project_id=scene['pid'], investigation_id=inv, index=1)
    begun = env.run(acts.begin_turn, base)
    base['turn_id'] = begun['turn_id']
    good = dict(tool='author_source', macro=dict(tool='prepare_candidate', basis='forged'),
                args=dict(files={'project.py': 'from eidos_recipes import cv, select\n'}, include=['eidos_recipes']))
    accepted = client.command('accept_decision', dict(turn_id=begun['turn_id'], slot=0, usage_recorded=True,
        proposal=dict(rationale='include library', calls=[good], control=dict(action='wait_for_jobs'))), project=scene['pid'])['result']
    assert accepted['accepted'], accepted
    assert 'macro' not in accepted['calls'][0]
    env.run(acts.execute_decision, dict(base, decision_id=accepted['decision_id']))
    receipts = client.query('get_record', dict(kind='decision_receipts', id=accepted['decision_id']), project=scene['pid'])['body']['receipts']
    aid = receipts[0]['result']['artifact_id']
    art = client.query('get_artifact', dict(artifact_id=aid), project=scene['pid'])
    source = json.loads(Path(scene['lab'].store.path_for(art['digest'])).read_text())
    assert source['library_digest'] and 'eidos_recipes/cv.py' in source['files']
    good['args']['files']['eidos_recipes/cv.py'] = 'collision'
    refused = client.command('accept_decision', dict(turn_id=begun['turn_id'], slot=1, usage_recorded=True,
        proposal=dict(calls=[good], control=dict(action='wait_for_jobs'))), project=scene['pid'])['result']
    assert not refused['accepted'] and 'collide' in refused['error']['message']


@pytest.mark.temporal
@pytest.mark.parametrize('kind', ['classification', 'regression'])
async def test_preparation_fixture_through_temporal_and_postgres(scene, kind, tmp_path):
    import asyncio
    import os
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
    from eidos.orchestration.workflows import TurnWorkflow

    cli = os.environ.get('EIDOS_TEMPORAL_CLI')
    if not cli:
        pytest.skip('set EIDOS_TEMPORAL_CLI to a dedicated test Temporal CLI')
    inv, acts, client = setup_scene(scene, kind)
    database = tmp_path / 'temporal.db'
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=cli, dev_server_database_filename=str(database))
    stopped = asyncio.Event()
    async def pump():
        while not stopped.is_set():
            await asyncio.to_thread(run_queued, scene, inv)
            await asyncio.sleep(.05)
    task = asyncio.create_task(pump())
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[TurnWorkflow]), Worker(
                env.client, task_queue=INVESTIGATOR_QUEUE, activity_executor=executor,
                activities=[acts.begin_turn, acts.build_context, acts.decide, acts.execute_decision, acts.complete_turn,
                            acts.record_turn_retry]):
                for index in (1, 2, 3):
                    await env.client.execute_workflow(TurnWorkflow.run, dict(project_id=scene['pid'], investigation_id=inv, index=index),
                        id='prepare-' + uuid.uuid4().hex, task_queue=WORKFLOW_QUEUE)
                    # Only the pump owns job execution; a second runner would fence its in-flight attempt.
                    for _ in range(1200):
                        jobs = await asyncio.to_thread(client.query, 'list_jobs', dict(investigation_id=inv), project=scene['pid'])
                        if all(j['status'] in ('succeeded', 'failed', 'cancelled', 'pruned') for j in jobs):
                            break
                        await asyncio.sleep(.05)
                    else:
                        pytest.fail('fixture jobs did not drain')
                view = client_view(scene, inv)
                prepared = suggested_lock(view)
                assert prepared['readiness']['readiness_ok'] == 1 and prepared['reproduction']['reproduced_by']
                assert any(n['kind'] == 'candidate_lock' for n in view['pinned_notes'])
                begun = ActivityEnvironment().run(acts.begin_turn, dict(project_id=scene['pid'], investigation_id=inv, index=4))
                state = client.query('get_turn', dict(turn_id=begun['turn_id']), project=scene['pid'])['spec']['exploration']
                assert state['rounds_without_improvement'] == 1
    finally:
        stopped.set()
        try:
            await task
        finally:
            await env.shutdown()


def evaluator_contract(scene, view, splits, workspace):
    """Run the exported grader argv on two seeds, all four thread caps, cold import included."""
    import shutil
    import time

    import numpy as np
    from eidos_recipes import models, select
    from study.eidos_arms.arm4 import grader_reproduce_command
    from study.fresh_check import candidate_command, equal_predictions

    candidate = suggested_lock(view)
    train = next(j for j in view['jobs'] if j['id'] == candidate['lock']['job_id'])
    lab = scene['lab']
    source_art = lab.q(lab.admin, 'get_artifact', dict(artifact_id=train['inputs']['source']), project=scene['pid'])
    bundle = json.loads(Path(lab.store.path_for(source_art['digest'])).read_text())
    code = workspace / 'candidate/source'
    code.mkdir(parents=True)
    for name, text in bundle['files'].items():
        path = code / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    inputs = workspace / 'inputs'
    inputs.mkdir()
    for port in ('train', 'validation'):
        art = lab.q(lab.admin, 'get_artifact', dict(artifact_id=scene['arts'][port]), project=scene['pid'])
        shutil.copyfile(lab.store.path_for(art['digest']), inputs / (port + '.npz'))
    np.savez(workspace / 'test_inputs.npz', x=splits['test']['x'], ids=splits['test']['ids'])
    expected = workspace / 'locked_validation.npz'
    output = next(o for o in train['outputs'] if o['slot'] == 'validation_predictions')
    art = lab.q(lab.admin, 'get_artifact', dict(artifact_id=output['artifact_id']), project=scene['pid'])
    shutil.copyfile(lab.store.path_for(art['digest']), expected)
    timings, scores = [], []
    for seed in (11, 23):
        out = workspace / ('fresh-' + str(seed))
        out.mkdir()
        argv = grader_reproduce_command(train['config']['argv'])
        substitutions = dict(python=os.environ['EIDOS_RESEARCH_PYTHON'], **{'candidate/source/project.py': str(code / 'project.py'), '{inputs}': str(inputs), '{out}': str(out), '{seed}': str(seed)})
        started = time.perf_counter()
        candidate_command(workspace, [substitutions.get(arg, arg) for arg in argv], timeout=120)
        elapsed = time.perf_counter() - started
        candidate_command(workspace, [os.environ['EIDOS_RESEARCH_PYTHON'], str(code / 'project.py'), 'infer', '--model', str(out / 'model.pt'),
                                     '--inputs', str(workspace / 'test_inputs.npz'), '--out', str(out / 'test_predictions.npz')], timeout=120)
        with np.load(out / 'test_predictions.npz') as p:
            predictions = p['predictions']
        spec = bundle['recipe']
        model = models.build(spec, seed).fit(np.asarray(splits['train']['x'], dtype=float).reshape(len(splits['train']['x']), -1), splits['train']['y'])
        portfolio_predictions = model.predict(np.asarray(splits['test']['x'], dtype=float).reshape(len(splits['test']['x']), -1))
        np.testing.assert_allclose(predictions, portfolio_predictions, rtol=0, atol=1e-12)
        if seed == 11:
            assert equal_predictions(expected, out / 'validation_predictions.npz')
        lab_job = next(j for j in view['jobs'] if j.get('macro') == train['macro'] and j.get('config', {}).get('seed') == seed
                       and 'train' in j.get('config', {}).get('argv', []))
        fit = lab_job['receipt']['metrics']['fit_seconds']
        assert fit * 2 <= 120 and elapsed <= 120 and (out / 'model.pt').stat().st_size <= 64_000_000
        timings.append(dict(seed=seed, evaluator_seconds=elapsed, lab_fit_seconds=fit,
                            evaluator_to_fit_ratio=elapsed / max(fit, 1e-9), guard_passed=elapsed <= 120))
        scores.append(select.score(spec['task_type'], splits['test']['y'], predictions))
    return dict(evaluator=timings, fresh_evaluator=fresh_seed_contract(workspace, splits), test_scores=scores, test_score_equals_portfolio=True)


@pytest.mark.skipif(not os.environ.get('EIDOS_STEP3_ACCEPTANCE'), reason='explicit 60-card development acceptance run')
@pytest.mark.parametrize('case', range(60))
def test_sixty_design_practice_preparations(scene, monkeypatch, tmp_path, case):
    import hashlib
    import time

    import numpy as np
    monkeypatch.syspath_prepend(str(ROOT))
    from study.practice import generators

    entries = [e for e in generators.registry() if e['role'] == 'design']
    entry = dict(entries[case // 10], instances=10)
    # Newly generated practice cards use local deterministic IDs, never the production salt.
    monkeypatch.setattr(generators, 'row_tokens', lambda seed, n: np.arange(n, dtype=np.int64) + seed * 100000)
    generated = generators.generate(entry, case % 10)
    splits = generated.splits
    for port in ('train', 'validation'):
        path = tmp_path / (port + '.npz')
        np.savez(path, **splits[port])
        digest, size = scene['lab'].store.put_bytes(path.read_bytes())
        scene['arts'][port] = scene['lab'].cmd(scene['lab'].admin, 'register_artifact',
            dict(digest=digest, size=size, schema='task-array/1', media_type='application/x-npz'), project=scene['pid'])['result']['artifact_id']
    started = time.perf_counter()
    inv, acts, client = setup_scene(scene, generated.task_type)
    decision, view = fixture_turn(scene, inv, acts, 1)
    candidate = suggested_lock(view)
    assert candidate['readiness']['readiness_ok'] == 1 and candidate['paired']['paired_n'] > 0
    assert candidate['reproduction']['reproduced_by']
    lines = pack(view, [], policy=POLICY_V4)['text'].splitlines()
    label = next(i for i, line in enumerate(lines) if line.startswith('- Prepared lock text ('))
    lock_text = lines[label + 1]
    _, locked = fixture_turn(scene, inv, acts, 2)
    (lock_note,) = [n for n in locked['pinned_notes'] if n['kind'] == 'candidate_lock']
    assert lock_note['text'] == lock_text
    from eidos.domain.exploration import at_ceiling
    if not at_ceiling(candidate['validation']['value'], candidate['lock']['selection']):
        fixture_turn(scene, inv, acts, 3)
    begun = ActivityEnvironment().run(acts.begin_turn, dict(project_id=scene['pid'], investigation_id=inv, index=4))
    state = client.query('get_turn', dict(turn_id=begun['turn_id']), project=scene['pid'])['spec']['exploration']
    # Perfect-score ceiling may stop the search before a refinement is admitted.
    assert state['stop'] in ('ceiling-no-improvement', 'ceiling-perfect-score')
    evidence = evaluator_contract(scene, view, splits, tmp_path / 'evaluator')
    record = dict(evaluator_workspace=str((tmp_path / 'evaluator').resolve()),
                  case=case, generator=entry['id'], instance=case % 10, task_type=generated.task_type,
                  newly_generated=case % 10 >= 4, wall_seconds=time.perf_counter() - started, model_calls=0,
                  readiness=candidate['readiness'], paired=candidate['paired'], reproduction=candidate['reproduction'],
                  emitted_lock_text=lock_text, recorded_lock_text=lock_note['text'],
                  decision_id=decision['decision_id'], macro=candidate['macro'], lock=candidate['lock'], state=state, **evidence)
    record['jobs'] = [dict(id=j['id'], config=j['config'], receipt=j['receipt'], macro=j.get('macro')) for j in view['jobs']]
    accepted_turn = client.query('get_turn', dict(turn_id=view['turns'][-1]['id']), project=scene['pid'])
    record['accepted_decision'] = next(d['body'] for d in accepted_turn['decisions'] if d['id'] == decision['decision_id'])
    record['reserves'] = dict(conclude_seconds=accepted_turn['spec']['completion']['conclude_threshold_seconds'], holdback_seconds=state['allowance']['holdback_seconds'])
    assert record['reserves'] == dict(conclude_seconds=270., holdback_seconds=270.)
    assert record['accepted_decision']['proposed_calls'][-1]['tool'] == 'prepare_candidate'
    assert all(c.get('macro') for c in record['accepted_decision']['calls'][1:])
    record['source_digest'] = hashlib.sha256((ROOT / 'src/eidos/domain/promotion.py').read_bytes()).hexdigest()
    destination = Path(os.environ['EIDOS_STEP3_ACCEPTANCE'])
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f'case-{case:02d}.json').write_text(json.dumps(record, indent=2, default=str) + '\n')


def fresh_seed_contract(workspace, splits):
    """Two evaluator seeds absent from both the lock and the lab reproduction."""
    import time

    import numpy as np
    from eidos_recipes import models, select
    from study.eidos_arms.arm4 import grader_reproduce_command
    from study.fresh_check import candidate_command

    code = workspace / 'candidate/source/project.py'
    spec = json.loads(code.with_name('recipe.json').read_text())
    argv = grader_reproduce_command(['python', 'project.py', 'train', '--inputs', '{work}', '--out', '{out}', '--seed', '{seed}'])
    rows = []
    for seed in (37, 53):
        out = workspace / ('fresh-' + str(seed))
        out.mkdir(exist_ok=True)
        substitutions = dict(python=os.environ['EIDOS_RESEARCH_PYTHON'], **{'candidate/source/project.py': str(code),
            '{inputs}': str(workspace / 'inputs'), '{out}': str(out), '{seed}': str(seed)})
        started = time.perf_counter()
        candidate_command(workspace, [substitutions.get(arg, arg) for arg in argv], timeout=120)
        elapsed = time.perf_counter() - started
        candidate_command(workspace, [os.environ['EIDOS_RESEARCH_PYTHON'], str(code), 'infer', '--model', str(out / 'model.pt'),
            '--inputs', str(workspace / 'test_inputs.npz'), '--out', str(out / 'test_predictions.npz')], timeout=120)
        with np.load(out / 'test_predictions.npz') as values:
            predictions = values['predictions']
        started = time.perf_counter()
        model = models.build(spec, seed).fit(np.asarray(splits['train']['x'], dtype=float).reshape(len(splits['train']['x']), -1), splits['train']['y'])
        fit = time.perf_counter() - started
        expected = model.predict(np.asarray(splits['test']['x'], dtype=float).reshape(len(splits['test']['x']), -1))
        np.testing.assert_allclose(predictions, expected, rtol=0, atol=1e-12)
        assert elapsed <= 120 and (out / 'model.pt').stat().st_size <= 64_000_000
        rows.append(dict(seed=seed, evaluator_seconds=elapsed, reference_fit_seconds=fit,
                         test_score=select.score(spec['task_type'], splits['test']['y'], predictions),
                         checkpoint_bytes=(out / 'model.pt').stat().st_size, predictions_equal=True))
    return rows


@pytest.mark.skipif(not os.environ.get('EIDOS_STEP3_ACCEPTANCE'), reason='explicit saved-workspace evaluator audit')
@pytest.mark.parametrize('case', range(60))
def test_two_unseen_evaluator_seeds_on_each_prepared_practice_card(monkeypatch, case):
    import hashlib

    import numpy as np
    monkeypatch.syspath_prepend(str(ROOT))
    import sys

    from study.practice import generators
    if str(ROOT / 'components/eidos.search.recipe-portfolio') not in sys.path:
        monkeypatch.syspath_prepend(str(ROOT / 'components/eidos.search.recipe-portfolio'))
    entries = [e for e in generators.registry() if e['role'] == 'design']
    monkeypatch.setattr(generators, 'row_tokens', lambda seed, n: np.arange(n, dtype=np.int64) + seed * 100000)
    generated = generators.generate(dict(entries[case // 10], instances=10), case % 10)
    evidence_path = Path(os.environ['EIDOS_STEP3_ACCEPTANCE']) / f'case-{case:02d}.json'
    record = json.loads(evidence_path.read_text())
    # Reuse the workspace actually prepared by the explicit acceptance run.
    # Older records without this field must be regenerated.
    workspace = Path(record['evaluator_workspace'])
    record['fresh_evaluator'] = fresh_seed_contract(workspace, generated.splits)
    record['fresh_seed_contract_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    evidence_path.write_text(json.dumps(record, indent=2, default=str) + '\n')
