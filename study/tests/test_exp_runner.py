"""Runner controls and real model-free subprocess qualification; no private inputs."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from study.common import ROOT, read_json, write_json
from study.exp import heldout, run
from study.private_card_registry import PROFILES, CardProfile, PoolProfile, load_pool_manifest, load_pools
from study.tests.test_private_seal import make_seal


def options(tmp_path, *extra):
    return run.parser().parse_args(['--treatment', 'stub', '--cards', 'N01,N03', '--out', str(tmp_path / 'out'), *extra])


def synthetic_manifest(pool='FAB', cards=('R0001', 'R0002'), full=False):
    return {('pool_id' if full else 'pool'): pool, 'commitment': 'a' * 64,
            'cards': [{('card_id' if full else 'id'): card, 'metric': 'accuracy', 'task_type': 'classification',
                       'bar_rule': 'absolute accuracy gain >= 0.02'} for card in cards]}


def test_both_pool_shapes_collisions_empty_registry_golden(tmp_path):
    legacy = {k: v for k, v in PROFILES.items() if isinstance(v, CardProfile)}
    profiles = dict(legacy)
    load_pools(profiles, tmp_path)
    assert tuple(profiles) == ('pilot-4', 'pilot-5', 'pilot-6')
    assert {c for p in profiles.values() for c in p.cards} == {f'N{i:02}' for i in range(5, 17)}
    load_pool_manifest(synthetic_manifest(full=True), profiles)
    assert profiles['pool:FAB'].bars() == {'R0001': ('accuracy', .02), 'R0002': ('accuracy', .02)}
    for manifest in (synthetic_manifest('OTHER'), synthetic_manifest('OTHER', ('N01',)),
                     synthetic_manifest('OTHER', ('N05',)), synthetic_manifest('OTHER', ('R0003', 'R0003'))):
        with pytest.raises(ValueError):
            load_pool_manifest(manifest, profiles)
    assert profiles['pool:FAB'].commitment() == 'a' * 64
    assert len(PROFILES['pool:E3'].cards) == 2


def test_pool_root_first_existing_and_explicit_lab(tmp_path, monkeypatch):
    profiles = {}
    load_pool_manifest(synthetic_manifest(), profiles)
    profile = profiles['pool:FAB']
    monkeypatch.setenv('EIDOS_POOLS_ROOT', str(tmp_path / 'missing') + os.pathsep + str(tmp_path))
    (tmp_path / 'FAB/seal').mkdir(parents=True)
    assert profile.seal_root() == tmp_path / 'FAB/seal'
    with pytest.raises(ValueError, match='explicit'):
        run.explicit_lab(None)
    with pytest.raises(ValueError, match='explicit'):
        run.explicit_lab(Path('relative'))


def test_plan_interleaves_pair_and_replicates():
    plan = run.make_plan(['N01', 'N03'], 2, (2, 4), [{'index': 0}, {'index': 1}])
    assert [(r['block_id'], r['variant']) for r in plan] == [
        ('N01-r1', 0), ('N01-r1', 1), ('N01-r2', 0), ('N01-r2', 1),
        ('N03-r1', 0), ('N03-r1', 1), ('N03-r2', 0), ('N03-r2', 1)]
    assert all(r['arms'] == [2, 4] for r in plan)
    with pytest.raises(ValueError):
        run.make_plan(['N01', 'N01'], 1, (4,), [{'index': 0}])


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    monkeypatch.setattr(run, 'disk_guard', lambda: None)
    args = options(tmp_path)
    args.references = tmp_path / 'references.json'
    write_json(args.references, {card: reference_block() for card in ('N01', 'N03')})
    selected = run.variants(args)
    plan = run.make_plan(['N01', 'N03'], 1, (4,), selected)
    return args, selected, plan


def test_preflight_each_refusal(tmp_path, monkeypatch, preflight):
    args, selected, plan = preflight
    assert run.preflight(args, selected, plan)['budget']['estimated_usd'] == 0
    monkeypatch.setattr(run, 'disk_guard', Mock(side_effect=RuntimeError('disk floor')))
    with pytest.raises(RuntimeError, match='disk floor'):
        run.preflight(args, selected, plan)
    monkeypatch.setattr(run, 'disk_guard', lambda: None)
    args.out = tmp_path.parent / 'escape'
    with pytest.raises(ValueError, match='output'):
        run.preflight(args, selected, plan)
    args.out = tmp_path / 'out'
    (tmp_path / 'PILOT_ACTIVE').touch()
    with pytest.raises(ValueError, match='PILOT_ACTIVE'):
        run.preflight(args, selected, plan)
    (tmp_path / 'PILOT_ACTIVE').unlink()
    args.wall_seconds = 3
    with pytest.raises(ValueError, match='smoke'):
        run.preflight(args, selected, plan)
    args.smoke = True
    run.preflight(args, selected, plan)
    args.concurrency = 0
    with pytest.raises(ValueError, match='positive'):
        run.preflight(args, selected, plan)
    args.concurrency = 2
    selected[0]['stub'] = False
    monkeypatch.setattr(run, 'controller_record', Mock(side_effect=ValueError('clean controller required')))
    with pytest.raises(ValueError, match='clean controller'):
        run.preflight(args, selected, plan)


def live_budget(tmp_path, provider):
    args = options(tmp_path)
    treatment = run.resolve('pilot4-v1', provider=provider)
    selected = [dict(index=0, label='live-test', stub=False, fixture=False, treatment=treatment.record(), provider=treatment.provider.record())]
    plan = run.make_plan(['N01', 'N03'], 1, (2, 4), selected)
    ledger = tmp_path / 'spend.jsonl'
    ledger.write_text('')
    args.budget = tmp_path / 'budget.json'
    budget = dict(spend_ledger=str(ledger), providers={provider: dict(usd_cap=20, stop_fraction=.8)},
                  minimax=dict(weekly_share=.5, weekly_floor=.15, interval_floor=.2))
    write_json(args.budget, budget)
    return args, selected, plan, ledger


def test_usd_worst_case_includes_arms_replicates_retries(tmp_path):
    args, selected, plan, ledger = live_budget(tmp_path, 'openai-default')
    assert run.budget_check(args, selected, plan)['estimated_usd'] == pytest.approx(4.8)
    ledger.write_text(json.dumps(dict(provider='openai-default', usd=12)) + '\n')
    with pytest.raises(ValueError, match='stop cap'):
        run.budget_check(args, selected, plan)
    args.budget = None
    with pytest.raises(ValueError, match='require --budget'):
        run.budget_check(args, selected, plan)


def test_minimax_quota_age_floors_share_and_payg(tmp_path):
    args, selected, plan, _ = live_budget(tmp_path, 'minimax-m3.1-flash')
    with pytest.raises(ValueError, match='quota-file'):
        run.budget_check(args, selected, plan)
    args.quota_file = tmp_path / 'quota.json'
    quota = dict(utc=datetime.now(UTC).isoformat(), weekly_remaining_fraction=.8, interval_remaining_fraction=.8)
    write_json(args.quota_file, quota)
    run.budget_check(args, selected, plan)
    for patch, message in [({'weekly_remaining_fraction': .1}, 'floor'),
                           ({'interval_remaining_fraction': .1}, 'floor'),
                           ({'weekly_remaining_fraction': .4}, 'share'),
                           ({'utc': (datetime.now(UTC) - timedelta(minutes=11)).isoformat()}, 'ten minutes')]:
        write_json(args.quota_file, {**quota, **patch})
        with pytest.raises(ValueError, match=message):
            run.budget_check(args, selected, plan)
    args.payg_approved = 'owner, 2026-11-14, one connectivity run'
    assert run.budget_check(args, selected, plan)['payg_approved'] == args.payg_approved


def test_incident_retry_reuses_launcher_archive(tmp_path, monkeypatch):
    from study import grade_block, launcher
    mock_run = Mock()
    mock_grade = Mock(side_effect=[[{'status': 'incident'}], [{'status': 'valid'}]])
    monkeypatch.setattr(launcher, 'run_block', mock_run)
    monkeypatch.setattr(grade_block, 'grade_block', mock_grade)
    request = dict(block=dict(card='N01', replicate=1, arms=[4], block_id='N01-r1', order=1), root=str(tmp_path),
                   stub=True, options=dict(wall_seconds=None, retry_incidents=1))
    assert run.block_worker(request) == 0
    assert [c.kwargs['replace_incident'] for c in mock_run.call_args_list] == [False, True]
    assert len(read_json(tmp_path / 'attempts/N01-r1.json')) == 2
    mock_grade.side_effect = [[{'status': 'incident'}], [{'status': 'incident'}]]
    assert run.block_worker(request) == 1


def test_dry_run_stops_before_labs_and_blocks(tmp_path, monkeypatch, preflight, capsys):
    args, _, _ = preflight
    args.dry_run = True
    monkeypatch.setattr(run.lab, 'start', Mock(side_effect=AssertionError('must not start')))
    assert run.execute(args) == 0
    manifest = read_json(args.out / 'manifest.json')
    assert len(manifest['plan']) == 2
    assert manifest['controller_commit']
    assert 'estimated_usd' in capsys.readouterr().out
    assert not (args.out / 'runs').exists()


def reference_block():
    return dict(headroom=.2, err_base=.5, err_ref=.3, se_paired=.01, base_score=.5, ref_score=.7, null_card=False)


def fabricated_pool(root):
    pool = root / 'FAB'
    bars = {'R0001': ('accuracy', .02), 'R0002': ('accuracy', .02)}
    digest = make_seal(pool / 'seal', bars)
    manifest = synthetic_manifest()
    manifest['commitment'] = digest
    write_json(pool / 'pool.json', manifest)
    for card in bars:
        write_json(pool / 'controller' / card / 'reference.json', reference_block())
    return pool


@pytest.mark.parametrize('pool', [False, True])
def test_stub_two_card_cli_end_to_end(tmp_path, monkeypatch, pool):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    argv = ['--treatment', 'stub', '--out', str(tmp_path / 'out'), '--arms', '2,4']
    if pool:
        source = fabricated_pool(tmp_path)
        monkeypatch.setenv('EIDOS_EXTRA_POOL_MANIFESTS', str(source / 'pool.json'))
        monkeypatch.setenv('EIDOS_POOLS_ROOT', str(tmp_path))
        argv += ['--pool', 'FAB']
    else:
        references = tmp_path / 'references.json'
        write_json(references, {card: reference_block() for card in ('N01', 'N03')})
        argv += ['--cards', 'N01,N03', '--references', str(references)]
    process = subprocess.run([sys.executable, '-m', 'study.exp.run', *argv], cwd=ROOT,
                             capture_output=True, text=True, timeout=240)
    assert process.returncode == 0, process.stdout + process.stderr + (tmp_path / 'out/run.log').read_text()
    score = read_json(tmp_path / 'out/scorecard.json')
    assert score['aggregate']['vrg'] == 0
    assert score['aggregate']['false_claims'] == 0
    assert score['aggregate']['tokens']['total'] == 0
    assert all(row['grade_status'] == 'valid' for row in score['individual'])
    assert len(score['individual']) == 4
    assert str(tmp_path) not in json.dumps(score)
    manifest = read_json(tmp_path / 'out/manifest.json')
    assert manifest['exit_status'] == 0
    assert manifest['labs'][0]['receipt']['test_fixture']
    assert manifest['variants'][0]['treatment']['id'] == 'stub'
    if pool:
        for path in (tmp_path / 'out/runs').glob('*/arm*/task'):
            assert not (path / 'test_labels.npz').exists()
            assert not (path / 'manifest.json').exists()
        assert len(list((tmp_path / 'out/grading').glob('*/arm*/workspace/test-inference/predictions.npz'))) == 4


def fake_scores():
    score = dict(treatment={'id': 'stub'}, build={'commit': 'a' * 40}, pool={'id': 'H'}, references_sha256='x',
                 per_card={'R0001': dict(vrg=0, null_card=False)}, individual=[],
                 aggregate=dict(verified_rate=0, false_claims=0, dead_ends=0, tokens={'total': 0}, usd=0))
    return [score, copy.deepcopy(score)]


def test_heldout_receipt_and_exception_do_not_leak(tmp_path, monkeypatch, capsys):
    args = SimpleNamespace(note='prospective test', pair=['stub@HEAD', 'stub@HEAD'], threshold=.1,
                           private_root=tmp_path, provider=None, budget=None, quota_file=None, payg_approved=None)
    write_json(tmp_path / 'H/pool.json', synthetic_manifest('H', ('R0001',)))
    scores = fake_scores()
    def fake_run(command, **kwargs):
        request = read_json(command[-1])
        root = Path(request['argv'][request['argv'].index('--out') + 1])
        write_json(root / 'manifest.json', dict(variants=[{'label': 'a'}, {'label': 'b'}]))
        for label, score in zip(('a', 'b'), scores, strict=True):
            write_json(root / label / 'scorecard.json', score)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(heldout.subprocess, 'run', fake_run)
    for k in range(1, 4):
        record = heldout.consult(args)
        assert record['consultation'] == f'{k} of 3'
        assert 'R0001' not in json.dumps(record)
        assert str(tmp_path) not in json.dumps(record)
    with pytest.raises(ValueError, match='limit'):
        heldout.consult(args)
    monkeypatch.setattr(heldout, 'consult', Mock(side_effect=RuntimeError(f'private {tmp_path}/R0001')))
    assert heldout.main(['--pair', 'stub@HEAD', 'stub@HEAD', '--threshold', '.1', '--note', 'test']) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'RuntimeError\n'


def test_pool_model_surfaces_and_environment_markers(tmp_path, monkeypatch):
    """One fabricated roster exercises prompt, charter and arm component selection."""
    from study import common, launcher
    from study.eidos_arms import charters, lab
    import study.private_card_registry as registry

    profiles = {}
    load_pool_manifest(synthetic_manifest(), profiles)
    monkeypatch.setitem(registry.PROFILES, 'pool:FAB', profiles['pool:FAB'])
    cards = frozenset({'R0001', 'R0002'}) | registry.ALL_PRIVATE_CARDS
    monkeypatch.setattr(registry, 'ALL_PRIVATE_CARDS', cards)
    monkeypatch.setattr(common, 'ALL_PRIVATE_CARDS', cards)
    monkeypatch.setattr(charters, 'ALL_PRIVATE_CARDS', cards)
    monkeypatch.setattr(launcher, 'ALL_PRIVATE_CARDS', cards)
    for card in ('R0001', 'R0002'):
        monkeypatch.setitem(common.FAMILIES, card, card)
    planned = launcher.plan(42, runs=1, arms=(4,), profile='pool:FAB')
    assert {row['task'] for row in planned} == {'R0001', 'R0002'}
    for name in registry.SEAL_ROOT_ENVS:
        monkeypatch.setenv(name, 'FABRICATED_SEAL_MARKER')
    for card in ('R0001', 'R0002'):
        assert '.02' in common.render_prompt(4, card)
        assert charters.charter(card)['success_rules'][0]['min_effect'] == .02
        assert any('paired-classification' in str(item.directory)
                   for item in run.resolve('autopilot-v2').component_plan(card))
        assert 'FABRICATED_SEAL_MARKER' not in common.render_prompt(4, card)
        assert 'FABRICATED_SEAL_MARKER' not in json.dumps(charters.charter(card))
    assert all(name not in lab.environment() for name in registry.SEAL_ROOT_ENVS)
    assert 'FABRICATED_SEAL_MARKER' not in json.dumps(common.agent_env(tmp_path))
    assert 'FABRICATED_SEAL_MARKER' not in json.dumps(lab.environment())


def test_blind_runner_real_stub_pair(tmp_path, monkeypatch, capsys):
    """All run/grade/score files stay private, including OS subprocess diagnostics."""
    source = fabricated_pool(tmp_path / 'source')
    private = tmp_path / 'private'
    private.mkdir()
    source.rename(private / 'H')
    manifest = read_json(private / 'H/pool.json')
    manifest['pool'] = 'H'
    write_json(private / 'H/pool.json', manifest)
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    assert heldout.main(['--pair', 'stub@HEAD', 'stub@HEAD', '--threshold', '.1', '--note', 'mechanical control',
                         '--private-root', str(private)]) == 0
    captured = capsys.readouterr()
    record = json.loads(captured.out)
    assert record['pass'] is False and record['tokens'] == [0, 0]
    assert not captured.err
    for marker in ('R0001', 'R0002', str(private)):
        assert marker not in captured.out
    assert len(list((private.parent / 'receipts').glob('*.json'))) == 1
    assert len(list((private / 'runs').glob('*/pair/paired.md'))) == 1


def test_smoke_output_cannot_enter_compare(tmp_path, monkeypatch):
    from study.exp.compare import compatible
    # compare already refuses any schema other than scientific scorecards.
    a, b = fake_scores()
    a.update(schema='eidos-exp-smoke-scorecard/1', smoke=True)
    with pytest.raises(ValueError, match='scorecard'):
        compatible(a, b)
    assert 'refused' in run.descriptive_pair(a, b)


def test_provider_family_caps_combine_all_anthropic_profiles(tmp_path):
    args, selected, plan, ledger = live_budget(tmp_path, 'anthropic-haiku-5-5')
    ledger.write_text(json.dumps(dict(provider='anthropic-sonnet-5-5', usd=12)) + '\n')
    with pytest.raises(ValueError, match='stop cap'):
        run.budget_check(args, selected, plan)


def test_multiclass_sealed_manifest_and_prediction_bounds(tmp_path):
    import hashlib
    import numpy as np
    from study.private_seal import PrivateSeal

    seal_root = tmp_path / 'seal'
    bars = {'R0001': ('accuracy', .02)}
    make_seal(seal_root, bars)
    def digest(data):
        return hashlib.sha256(data).hexdigest()
    task_path = seal_root / 'R0001/bundle/task.json'
    task = read_json(task_path)
    task['nclasses'] = 3
    write_json(task_path, task)
    manifest_path = seal_root / 'R0001/manifest.json'
    manifest = read_json(manifest_path)
    manifest['nclasses'] = 3
    manifest['files']['bundle/task.json'] = digest(task_path.read_bytes())
    write_json(manifest_path, manifest)
    files = {p.relative_to(seal_root).as_posix(): digest(p.read_bytes()) for p in seal_root.rglob('*')
             if p.is_file() and p.name != 'commitment-opening.json'}
    salt = bytes(range(32))
    commitment = digest(salt + json.dumps(files, sort_keys=True, separators=(',', ':')).encode())
    write_json(seal_root / 'commitment-opening.json', dict(files=files, salt=salt.hex(), sha256=commitment))
    seal = PrivateSeal(seal_root, commitment, bars)
    prediction = tmp_path / 'prediction.npz'
    np.savez(prediction, ids=[20, 10], predictions=[2, 0])
    assert seal.score('R0001', prediction)['candidate'] == .5
    for invalid in ([3, 0], [-1, 0], [1.5, 0]):
        np.savez(prediction, ids=[20, 10], predictions=invalid)
        with pytest.raises(ValueError, match='class'):
            seal.score('R0001', prediction)
    manifest_path.write_text('{}')
    with pytest.raises(ValueError, match='digest changed'):
        seal.score('R0001', prediction)


def test_accounting_includes_archived_incidents_and_unknown_cost(tmp_path):
    args, selected, _, ledger = live_budget(tmp_path, 'openai-default')
    root = tmp_path / 'attempt'
    write_json(root / 'runs/N01-r1/arm4/run_record.json', dict(tokens={'input': 100, 'output': 20}, billed_usd=.01))
    write_json(root / 'runs/incidents/N01-first/arm4/run_record.json', dict(tokens={}, billed_usd=None))
    run.account(args, dict(live=True, spend_ledger=str(ledger)), [root], selected, 'mechanical-control')
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2
    assert {row['usd'] for row in rows} == {.01, None}
    assert all(row['run_id'] == 'mechanical-control' for row in rows)


def test_failed_runner_manifest_matches_exit_status(tmp_path, monkeypatch, preflight):
    args, _, _ = preflight
    monkeypatch.setattr(run, 'ensure_lab', Mock(side_effect=RuntimeError('fabricated build failure')))
    assert run.main(['--treatment', 'stub', '--cards', 'N01,N03', '--references', str(args.references),
                     '--out', str(args.out)]) == 2
    manifest = read_json(args.out / 'manifest.json')
    assert manifest['exit_status'] == 2 and manifest['error_type'] == 'RuntimeError'
    assert 'RuntimeError' in (args.out / 'run.log').read_text()


def test_pool_arm_common_prepare_routes_classification_and_regression(tmp_path, monkeypatch):
    installed = os.environ.get('EIDOS_STUDY_PYTHON')
    if not installed or not Path(installed).is_file():
        pytest.skip('requires the study lab interpreter')
    manifest = synthetic_manifest()
    manifest['cards'][1].update(metric='rmse', task_type='regression',
                               bar_rule='absolute RMSE reduction >= 0.05 * seed-11 baseline test RMSE')
    monkeypatch.setenv('EIDOS_STUDY_POOL_PROFILES', json.dumps([manifest]))
    # No package imports, installation or lab operations cross the fake client boundary.
    source = '''
import json, sys
from pathlib import Path
from unittest.mock import Mock, patch
from study.eidos_arms import common
from study.exp.treatment import resolve
root = Path(sys.argv[1])
client = Mock()
client.query.return_value = {'body': {'policy': {'llm_broker': {'enabled': False}}}}
for card, regression in [('R0001', False), ('R0002', True)]:
    directory = root / card
    directory.mkdir()
    (directory / 'arm.json').write_text(json.dumps({'card': card, 'treatment': resolve().record()}))
    with patch('eidos.tasks.bundle.import_bundle', return_value={'task_id':card, 'artifacts':{}}), \\
         patch('eidos.cli.main.install_component', return_value={'component_ref':'fabricated'}), \\
         patch('study.eidos_arms.common.install_shared_project_command'), \\
         patch('study.eidos_arms.charters.commit', return_value={'charter_id':'fabricated'}), \\
         patch('study.eidos_arms.protocols.prepare') as protocols:
        _, imported = common.prepare(client, directory, 4)
        assert imported['task_id'] == card
        assert protocols.call_count == (0 if regression else 1)
        assert json.loads((directory / 'lab_import.json').read_text())['task_id'] == card
'''
    process = subprocess.run([installed, '-c', source, str(tmp_path)], cwd=ROOT,
                             text=True, capture_output=True, timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr
