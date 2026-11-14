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
from study.private_card_registry import PROFILES, CardProfile, load_pool_manifest, load_pools
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
    manifest = json.loads(capsys.readouterr().out)
    assert len(manifest['plan']) == 2
    assert manifest['controller_commit']
    assert 'estimated_usd' in manifest['preflight']['budget']
    assert not args.out.exists()
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
@pytest.mark.parametrize('arms', ['2,4', '4'])
def test_stub_two_card_cli_end_to_end(tmp_path, monkeypatch, pool, arms):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    argv = ['--treatment', 'stub', '--out', str(tmp_path / 'out'), '--arms', arms,
            '--tier', 'T2', '--pool-role', 'evaluation_rotation', '--look', 'extension', '--mode', 'live']
    if pool:
        source = fabricated_pool(tmp_path)
        monkeypatch.setenv('EIDOS_IMP_PRIVATE_ROOT', str(tmp_path / 'private'))
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
    for arm in map(int, arms.split(',')):
        score = read_json(tmp_path / f'out/arm{arm}/scorecard.json')
        assert (tmp_path / f'out/arm{arm}/scorecard.md').is_file()
        assert score['aggregate']['vrg'] == 0
        assert score['aggregate']['false_claims'] == 0
        assert score['aggregate']['tokens']['total'] == 0
        assert len(score['individual']) == 2
        assert all(row['grade_status'] == 'valid' and row['arm'] == arm
                   and row['tier'] == 'T2' and row['pool'] == 'evaluation_rotation'
                   and row['look'] == 'extension' and row['mode'] == 'live' for row in score['individual'])
        assert str(tmp_path) not in json.dumps(score)
    if arms == '4':
        assert read_json(tmp_path / 'out/scorecard.json') == score
        assert (tmp_path / 'out/scorecard.md').read_bytes() == (tmp_path / 'out/arm4/scorecard.md').read_bytes()
    else:
        assert not (tmp_path / 'out/scorecard.json').exists()
        assert not (tmp_path / 'out/scorecard.md').exists()
    manifest = read_json(tmp_path / 'out/manifest.json')
    assert manifest['exit_status'] == 0
    assert manifest['labs'][0]['receipt']['test_fixture']
    assert manifest['variants'][0]['treatment']['id'] == 'stub'
    assert {k: manifest[k] for k in ('tier', 'pool_role', 'look', 'mode')} == dict(
        tier='T2', pool_role='evaluation_rotation', look='extension', mode='live')
    assert [entry['arm'] for entry in manifest['scorecards']] == list(map(int, arms.split(',')))
    for entry in manifest['scorecards']:
        assert (tmp_path / 'out' / entry['json']).is_file()
        assert (tmp_path / 'out' / entry['markdown']).is_file()
    usage = read_json(tmp_path / 'out' / manifest['usage'])
    assert usage['schema'] == 'eidos-exp-usage/1'
    assert usage['runs'] == 2 * len(arms.split(',')) and usage['usd'] == 0
    assert usage['quota_weekly_percent'] is None
    assert usage['time_seconds'] > 0
    assert usage['per_variant'] == {manifest['variants'][0]['label']: dict(runs=usage['runs'], usd=0)}
    if pool:
        for path in (tmp_path / 'out/runs').glob('*/arm*/task'):
            assert not (path / 'test_labels.npz').exists()
            assert not (path / 'manifest.json').exists()
        assert not list((tmp_path / 'out/grading').rglob('test_inputs.npz'))
        assert len(list((tmp_path / 'private/grading').glob('*/**/workspace/test-inference/predictions.npz'))) == usage['runs']


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
    monkeypatch.setattr(heldout, 'nonprivate_preflight', lambda *a: None)
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
    import study.private_card_registry as registry
    from study import common, launcher
    from study.eidos_arms import charters, lab

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
    assert record['novelty_wins'] == [0, 0]
    assert not captured.err
    for marker in ('R0001', 'R0002', str(private)):
        assert marker not in captured.out
    assert len(list((private.parent / 'receipts').glob('*.json'))) == 1
    assert len(list((private / 'runs').glob('*/pair/paired.md'))) == 1
    manifest_file = next((private / 'runs').glob('*/pair/manifest.json'))
    run_manifest = read_json(manifest_file)
    usage_file = (manifest_file.parent / run_manifest['usage']).resolve()
    assert usage_file.is_relative_to(private)
    usage = read_json(usage_file)
    assert usage['runs'] == 4 and usage['usd'] == 0
    assert not (private.parent / 'usage.json').exists()


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
    usage = read_json(args.out / 'usage.json')
    assert usage['runs'] == 2 and usage['usd'] is None
    assert usage['per_variant'] == {'live-test': dict(runs=2, usd=None)}


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


def test_builder_child_cannot_inherit_seal_locations(tmp_path, monkeypatch):
    args = options(tmp_path)
    variant = run.variants(args)[0]
    variant['lab_root'] = str(tmp_path / 'ip-build')
    monkeypatch.delenv('EIDOS_EXP_TEST_ROOT', raising=False)
    controller_names = ('EIDOS_POOLS_ROOT', 'EIDOS_EXTRA_POOL_MANIFESTS', 'EIDOS_IMP_PRIVATE_ROOT')
    for name in (*run.SEAL_ROOT_ENVS, *controller_names):
        monkeypatch.setenv(name, 'FABRICATED_SEAL_MARKER')
    monkeypatch.setenv('OPENAI_API_KEY', 'FAKE_KEY_MARKER')
    builder = Mock()
    monkeypatch.setattr(run.subprocess, 'run', builder)
    monkeypatch.setattr(run, 'configure_lab', lambda *args: {'verified': True})
    with run.environment({}):
        run.ensure_lab(variant)
    child_env = builder.call_args.kwargs['env']
    assert all(name not in child_env for name in (*run.SEAL_ROOT_ENVS, *controller_names))
    assert 'OPENAI_API_KEY' not in child_env


def test_novelty_receipt_counts_known_zero_and_preserves_unknown():
    assert heldout.novelty_total({'individual': [{'verified': False}]}) == 0
    assert heldout.novelty_total({'individual': [{'verified': True, 'novelty': {'class': 'catalog'}}]}) == 0
    assert heldout.novelty_total({'individual': [{'verified': True, 'novelty': {'class': 'external'}}]}) is None
    assert heldout.novelty_total({'individual': [{'novelty_win': True}, {'novelty_win': False}]}) == 1


@pytest.mark.parametrize('classes', ['missing', None, 1, True, 2.5, 3])
def test_task_class_count_base_goldens(tmp_path, monkeypatch, classes):
    """Hard-coded bdce95a outcomes from fabricated Pilot-5 metadata."""
    import numpy as np

    from study.private_seal import PrivateSeal
    bars = {'N09': ('accuracy', .02)}
    seal = PrivateSeal(tmp_path / 'seal', make_seal(tmp_path / 'seal', bars), bars)
    read = seal._read
    monkeypatch.setattr(seal, '_read', lambda card, name: json.dumps({} if classes == 'missing' else {'nclasses': classes}).encode()
                        if name == 'bundle/task.json' else read(card, name))
    prediction = tmp_path / 'prediction.npz'
    np.savez(prediction, ids=[20, 10], predictions=[2, 0])
    if classes == 3:
        assert seal.score('N09', prediction)['candidate'] == .5
    else:
        with pytest.raises(RuntimeError, match='private task class count missing or invalid'):
            seal.score('N09', prediction)


def test_d03_reference_is_unestablished_and_loader_refuses(tmp_path):
    reference = read_json(ROOT / 'experiments/pools/legacy-references.json')['D03']
    assert reference['reference_established'] is False
    assert set(reference) == {'card', 'reference_established', 'reason'}
    assert 'ref_scores [0.1, 0.1, 0.1]' in reference['reason']
    args = options(tmp_path)
    with pytest.raises(ValueError, match='explicit --references'):
        run.load_references(args, None, ['D03'])


def test_heldout_account_one_aggregate_and_private_detail(tmp_path):
    args, selected, _, ledger = live_budget(tmp_path, 'openai-default')
    private = tmp_path / 'private'
    out = private / 'runs' / 'utc'
    for card, tokens in [('R0001', 100), ('R0002', 900)]:
        write_json(out / 'pair' / 'runs' / card / 'arm4/run_record.json',
                   dict(tokens={'input': tokens, 'output': 20}, billed_usd=.01))
    run.account(args, dict(live=True, spend_ledger=str(ledger)), [out / 'pair'], selected, 'utc',
                private_root=private, out=out)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]['pool'] == 'held-out' and rows[0]['n_runs'] == 2
    assert rows[0]['tokens_in'] == 1000 and rows[0]['usd'] == .02
    assert 'R000' not in ledger.read_text() and str(private) not in ledger.read_text()
    assert len((out / 'spend.jsonl').read_text().splitlines()) == 2
    assert read_json(out / 'usage.json')['runs'] == 2
    assert not (args.out / 'usage.json').exists()


def test_lab_environment_never_exports_roster(monkeypatch):
    monkeypatch.setenv('EIDOS_STUDY_POOL_PROFILES', json.dumps([synthetic_manifest('H')]))
    exported = 'EIDOS_STUDY_POOL_PROFILES' in run.lab.environment()
    assert not exported


@pytest.mark.parametrize('name', ['EIDOS_POOLS_ROOT', 'EIDOS_EXTRA_POOL_MANIFESTS', 'EIDOS_IMP_PRIVATE_ROOT'])
def test_controller_environment_markers_explicit(monkeypatch, name):
    monkeypatch.setenv(name, 'PRIVATE_CONTROLLER_MARKER')
    assert name not in run.lab.environment()


def test_heldout_rounding_and_signflip_pass_rule():
    a, b = fake_scores()
    b['per_card']['R0001']['vrg'] = .123
    b['aggregate']['verified_rate'] = .123
    result = heldout.receipt([a, b], utc='now', note='test', consultation=1, threshold=.1)
    assert result['mean_delta_vrg'] == .1
    assert result['verified_rate'] == [0, .1]
    assert result['pass'] is False  # one positive pair has p=.5


def test_usd_budget_boundary_and_stop_fraction(tmp_path):
    args, selected, plan, ledger = live_budget(tmp_path, 'openai-default')
    ledger.write_text(json.dumps(dict(provider='OpenAI', usd=11.2)) + '\n')
    with pytest.raises(ValueError, match='stop cap'):
        run.budget_check(args, selected, plan)
    ledger.write_text('')
    budget = read_json(args.budget)
    budget['providers']['openai-default']['stop_fraction'] = .81
    write_json(args.budget, budget)
    with pytest.raises(ValueError, match='cap/stop fraction'):
        run.budget_check(args, selected, plan)


def test_minimax_payg_whitespace(tmp_path):
    args, selected, plan, _ = live_budget(tmp_path, 'minimax-m3.1-flash')
    args.payg_approved = '   '
    with pytest.raises(ValueError, match='payg approval'):
        run.budget_check(args, selected, plan)


@pytest.mark.parametrize('scenario', ['start-failure', 'block-failure', 'stop-failure', 'keep'])
def test_execute_lab_explicit_home_and_finally(tmp_path, monkeypatch, preflight, scenario):
    args, _, _ = preflight
    args.pair = ['autopilot-v2@HEAD', 'autopilot-v2@HEAD~1']
    args.keep_lab_running = scenario == 'keep'
    selected = run.variants(args)
    for variant in selected:
        variant.update(stub=False, fixture=True)
    monkeypatch.setattr(run, 'variants', lambda _: selected)
    monkeypatch.setattr(run, 'controller_record', lambda *a, **k: {'git_sha': 'a' * 40})
    def ensure(v, **kwargs):
        home = tmp_path / 'labs' / v['commit'] / 'home'
        # Deliberately omit HOME: execute must explicitly set it at the start boundary.
        return dict(home=str(home), python=sys.executable, receipt={}, env={'EIDOS_STUDY_ROOT': str(tmp_path)})
    monkeypatch.setattr(run, 'ensure_lab', ensure)
    starts, stops = [], []
    def start(*a, **k):
        starts.append(os.environ.get('EIDOS_STUDY_HOME'))
        assert starts[-1] == selected[len(starts) - 1]['lab']['home']
        if scenario == 'start-failure':
            raise RuntimeError('partial startup')
    def stop():
        stops.append(os.environ.get('EIDOS_STUDY_HOME'))
        if scenario == 'stop-failure' and len(stops) == 1:
            raise RuntimeError('first stop fails')
    monkeypatch.setattr(run.lab, 'start', start)
    monkeypatch.setattr(run.lab, 'stop', stop)
    real_subprocess = run.subprocess.run
    def child(command, **kwargs):
        if '--_block' in command:
            raise RuntimeError('block failed')
        return real_subprocess(command, **kwargs)
    monkeypatch.setattr(run, 'dispatch_process', child)
    old_home = os.environ.get('EIDOS_STUDY_HOME')
    with pytest.raises(RuntimeError):
        run.execute(args)
    assert len(starts) == (1 if scenario == 'start-failure' else 2)
    assert len(stops) == (0 if scenario == 'keep' else len(starts))
    assert set(stops) <= {v['lab']['home'] for v in selected}
    assert os.environ.get('EIDOS_STUDY_HOME') == old_home


@pytest.fixture
def blind_fake(tmp_path, monkeypatch):
    args = SimpleNamespace(note='prospective test', pair=['stub@HEAD', 'stub@HEAD'], threshold=.1,
                           private_root=tmp_path, provider=None, budget=None, quota_file=None, payg_approved=None)
    write_json(tmp_path / 'H/pool.json', synthetic_manifest('H', ('R0001',)))
    monkeypatch.setattr(heldout, 'nonprivate_preflight', lambda *a: None)
    def child(command, **kwargs):
        request = read_json(command[-1])
        root = Path(request['argv'][request['argv'].index('--out') + 1])
        write_json(root / 'manifest.json', dict(variants=[{'label': 'a'}, {'label': 'b'}]))
        for label, score in zip(('a', 'b'), fake_scores(), strict=True):
            write_json(root / label / 'scorecard.json', score)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(heldout.subprocess, 'run', child)
    return args


def test_heldout_failed_attempts_consume_consultations(tmp_path, monkeypatch, blind_fake):
    monkeypatch.setattr(heldout.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    for _ in range(3):
        with pytest.raises(RuntimeError, match='private run failed'):
            heldout.consult(blind_fake)
    assert read_json(tmp_path / 'consultations.json')['count'] == 3
    with pytest.raises(ValueError, match='limit'):
        heldout.consult(blind_fake)


@pytest.mark.parametrize('marker', ['R0001', 'private-path'])
def test_heldout_finished_receipt_privacy_filter(tmp_path, monkeypatch, blind_fake, marker):
    marker = str(tmp_path) if marker == 'private-path' else marker
    monkeypatch.setattr(heldout, 'receipt', lambda *a, **k: {'unsafe': marker})
    with pytest.raises(ValueError, match='unsafe receipt'):
        heldout.consult(blind_fake)


@pytest.mark.parametrize('marker', ['R0001', 'private-path'])
def test_heldout_note_privacy_filter(tmp_path, blind_fake, marker):
    blind_fake.note = str(tmp_path) if marker == 'private-path' else marker
    with pytest.raises(ValueError, match='private identifiers'):
        heldout.consult(blind_fake)
    assert not (tmp_path / 'consultations.json').exists()


def test_heldout_child_preflight_refund_recorded(tmp_path, monkeypatch, blind_fake):
    def child(command, **kwargs):
        request = read_json(command[-1])
        root = Path(request['argv'][request['argv'].index('--out') + 1])
        write_json(root / 'manifest.json', {'status': 'preflight'})
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(heldout.subprocess, 'run', child)
    with pytest.raises(RuntimeError):
        heldout.consult(blind_fake)
    state = read_json(tmp_path / 'consultations.json')
    assert state['count'] == 0
    assert [event['status'] for event in state['events']] == ['reserved', 'refunded']


@pytest.mark.parametrize('pair', [['nonesuch@HEAD', 'stub@HEAD'], ['stub@does-not-exist', 'stub@HEAD']])
def test_heldout_invalid_variant_does_not_consume(tmp_path, monkeypatch, pair):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    write_json(tmp_path / 'H/pool.json', synthetic_manifest('H', ('R0001',)))
    args = SimpleNamespace(note='test', pair=pair, threshold=.1, private_root=tmp_path,
                           provider=None, budget=None, quota_file=None, payg_approved=None)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        heldout.consult(args)
    assert not (tmp_path / 'consultations.json').exists()


@pytest.mark.parametrize('pool', [False, True])
def test_smoke_real_cli_schema(tmp_path, monkeypatch, pool):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    argv = ['--treatment', 'stub', '--out', str(tmp_path / 'out'), '--smoke', '--wall-seconds', '10']
    if pool:
        source = fabricated_pool(tmp_path)
        monkeypatch.setenv('EIDOS_EXTRA_POOL_MANIFESTS', str(source / 'pool.json'))
        monkeypatch.setenv('EIDOS_POOLS_ROOT', str(tmp_path))
        monkeypatch.setenv('EIDOS_IMP_PRIVATE_ROOT', str(tmp_path / 'private'))
        argv += ['--pool', 'FAB', '--card-limit', '1']
    else:
        refs = tmp_path / 'refs.json'
        write_json(refs, {'N01': reference_block()})
        argv += ['--cards', 'N01', '--references', str(refs)]
    child = subprocess.run([sys.executable, '-m', 'study.exp.run', *argv], capture_output=True, text=True, timeout=180)
    assert child.returncode == 0, child.stdout + child.stderr
    score = read_json(tmp_path / 'out/scorecard.json')
    assert score['schema'] == 'eidos-exp-smoke-scorecard/1' and score['smoke'] is True
    from study.exp.compare import compatible
    with pytest.raises(ValueError, match='scorecard'):
        compatible(score, score)


def test_heldout_lab_home_is_under_private_root(tmp_path, monkeypatch):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path / 'public'))
    variant = run.variants(options(tmp_path))[0]
    private = tmp_path / 'private'
    with run.environment({}):
        result = run.ensure_lab(variant, private_root=private)
    assert Path(result['home']).is_relative_to(private)
    assert result['env']['EIDOS_STUDY_HOME'] == result['home']
    assert not (tmp_path / 'public').exists()


def test_pilot_cards_refused(tmp_path):
    args = options(tmp_path)
    args.cards = 'N05,N16'
    with pytest.raises(ValueError, match='sealed Pilot'):
        run.pool_selection(args)


def test_extra_manifest_malformed_is_loud(tmp_path):
    manifest = tmp_path / 'broken.json'
    write_json(manifest, {'cards': []})
    with pytest.raises(ValueError, match='identity'):
        load_pools({}, tmp_path / 'empty', extra=[manifest])


@pytest.mark.parametrize('case', ['naive-quota', 'nan-cap', 'missing-ledger-key', 'negative-limit', 'nan-limit'])
def test_malformed_budget_quota_and_ledger_exit_two(tmp_path, monkeypatch, capsys, case):
    provider = 'minimax-m3.1-flash' if case == 'naive-quota' else 'openai-default'
    args, selected, plan, ledger = live_budget(tmp_path, provider)
    if case == 'naive-quota':
        args.quota_file = tmp_path / 'quota.json'
        write_json(args.quota_file, dict(utc=datetime.now().isoformat(), weekly_remaining_fraction=.8,
                                        interval_remaining_fraction=.8))
    elif case == 'nan-cap':
        budget = read_json(args.budget)
        budget['providers']['openai-other'] = dict(usd_cap=float('nan'), stop_fraction=.8)
        args.budget.write_text(json.dumps(budget))
    elif case == 'missing-ledger-key':
        ledger.write_text('{"usd": 1}\n')
    else:
        treatment = run.resolve('pilot4-v1', provider=provider)
        invalid = SimpleNamespace(id=treatment.provider.id,
                                  model_usd_per_run=-1 if case == 'negative-limit' else float('nan'))
        monkeypatch.setattr(run, 'resolve', lambda *a, **k: SimpleNamespace(provider=invalid))
    monkeypatch.setattr(run, 'execute', lambda _: run.budget_check(args, selected, plan))
    assert run.main(['--treatment', 'stub', '--cards', 'N01', '--out', str(args.out)]) == 2
    assert 'experiment runner failed:' in capsys.readouterr().err


def test_live_preflight_checks_real_pilot_flag_with_test_root(tmp_path, monkeypatch, preflight):
    args, selected, plan = preflight
    programme = tmp_path / 'real-programme'
    programme.mkdir()
    (programme / 'PILOT_ACTIVE').touch()
    monkeypatch.setattr(run, 'PROGRAMME', programme)
    selected[0].update(stub=False, fixture=False)
    with pytest.raises(ValueError, match='PILOT_ACTIVE'):
        run.preflight(args, selected, plan)


def test_live_reservations_precede_dispatch_and_settle(tmp_path):
    args, selected, plan, ledger = live_budget(tmp_path, 'openai-default')
    budget = run.budget_check(args, selected, plan)
    reservations = []
    run.reserve_spend(args, budget, selected, plan, plan[0], reservations)
    row = json.loads(ledger.read_text())
    assert row['status'] == 'reserved' and row['usd'] == 2.4
    root = tmp_path / 'run'
    for arm in (2, 4):
        write_json(root / f'runs/N01-r1/arm{arm}/run_record.json',
                   dict(block_id='N01-r1', tokens={'input': 100, 'output': 20}, billed_usd=.1))
    run.account(args, budget, [root], selected, 'test', reservations=reservations)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert rows[0]['status'] == 'settled' and rows[0]['usd'] == 0
    assert sum(row['usd'] for row in rows) == .2


def test_heldout_reservation_is_one_aggregate_and_settles(tmp_path):
    args, selected, plan, ledger = live_budget(tmp_path, 'openai-default')
    budget = run.budget_check(args, selected, plan)
    reservations = []
    private = tmp_path / 'private'
    for block in plan:
        run.reserve_spend(args, budget, selected, plan, block, reservations, private_root=private)
    row = json.loads(ledger.read_text())
    assert row['status'] == 'reserved' and row['usd'] == 4.8 and row['pool'] == 'held-out'
    assert not {'block', 'variant', 'utc'} & row.keys()
    out = private / 'runs/utc'
    root = out / 'pair'
    for block in plan:
        for arm in block['arms']:
            write_json(root / f'runs/{block["block_id"]}/arm{arm}/run_record.json',
                       dict(tokens={'input': 1, 'output': 2}, billed_usd=.1))
    run.account(args, budget, [root], selected, 'test', private_root=private, out=out, reservations=reservations)
    row = json.loads(ledger.read_text())
    assert row['status'] == 'settled' and row['n_runs'] == 4 and row['usd'] == .4
    assert not {'block', 'variant', 'utc'} & row.keys()


def test_termination_handlers_restore_and_raise():
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        with pytest.raises(RuntimeError, match='termination requested'), run.termination_handlers():
            signal.raise_signal(sig)
        assert signal.getsignal(sig) == previous


@pytest.mark.parametrize('kind,predictions', [('classification', [1, 1, 1]), ('regression', [.5, .5, .5])])
def test_legacy_constant_reference_is_unestablished(tmp_path, monkeypatch, kind, predictions):
    import numpy as np

    from study.exp.cards import reference_legacy as legacy
    monkeypatch.setattr(legacy, 'dev_command', lambda *a: write_json(tmp_path / 'bundle/task.json', {'project': 'test'}))
    data = {'train': (np.zeros((3, 1)), np.array([0, 1, 1]), np.arange(3)),
            'test': (np.zeros((3, 1)), np.array([0, 1, 1]), np.arange(3))}
    monkeypatch.setattr(legacy, 'prospective_dataset', lambda _: (data, kind, 2))
    monkeypatch.setattr(legacy, 'select_reference', lambda *a, **k: {})
    monkeypatch.setattr(legacy, 'run_cli', lambda *a: (np.array([0, 0, 0]), np.arange(3), 0, 0))
    frozen = tmp_path / 'frozen.json'
    baseline = 0 if kind == 'regression' else 1 / 3
    if kind == 'regression':
        baseline = (2 / 3) ** .5
    write_json(frozen, {'N01': {'bar': .02, 'baseline': baseline}})
    monkeypatch.setattr(legacy, 'FROZEN', frozen)
    def evaluate(*a, **k):
        target = tmp_path / 'reference'
        target.mkdir()
        np.savez(target / 'reference_test.npz', predictions=predictions)
        return {'ref_scores': [.1, .1, .1]}
    monkeypatch.setattr(legacy, 'evaluate_spec', evaluate)
    result = legacy.reference('N01', tmp_path, 0)
    assert result['reference_established'] is False and result['card'] == 'N01'
    assert set(result) == {'card', 'reference_established', 'reason'}


@pytest.mark.parametrize('classes', [None, 1, True, 2.5])
def test_manifest_class_count_validation(tmp_path, monkeypatch, classes):
    import numpy as np

    from study.private_seal import PrivateSeal
    bars = {'N09': ('accuracy', .02)}
    seal = PrivateSeal(tmp_path / 'seal', make_seal(tmp_path / 'seal', bars), bars)
    manifest = seal.card('N09')
    monkeypatch.setattr(seal, 'card', lambda _: {**manifest, 'nclasses': classes})
    prediction = tmp_path / 'prediction.npz'
    np.savez(prediction, ids=[20, 10], predictions=[0, 0])
    with pytest.raises(RuntimeError, match='class count invalid'):
        seal.score('N09', prediction)


def test_manifest_and_task_class_counts_must_agree(tmp_path, monkeypatch):
    import numpy as np

    from study.private_seal import PrivateSeal
    bars = {'N09': ('accuracy', .02)}
    seal = PrivateSeal(tmp_path / 'seal', make_seal(tmp_path / 'seal', bars), bars)
    manifest = seal.card('N09')
    monkeypatch.setattr(seal, 'card', lambda _: {**manifest, 'nclasses': 3})
    prediction = tmp_path / 'prediction.npz'
    np.savez(prediction, ids=[20, 10], predictions=[2, 0])
    with pytest.raises(RuntimeError, match='disagrees'):
        seal.score('N09', prediction)


@pytest.mark.parametrize('keep', [False, True])
def test_termination_stops_fake_lab_and_settles_reservation(tmp_path, monkeypatch, preflight, keep):
    import signal
    args, selected, plan = preflight
    args.cards = 'N01'
    args.keep_lab_running = keep
    selected = run.variants(args)
    selected[0].update(stub=False, fixture=False)
    monkeypatch.setattr(run, 'variants', lambda _: selected)
    monkeypatch.setattr(run, 'controller_record', lambda *a, **k: {'git_sha': 'a' * 40})
    budget_args, live, _, ledger = live_budget(tmp_path, 'openai-default')
    selected[0].update(provider=live[0]['provider'], treatment=live[0]['treatment'])
    args.budget = budget_args.budget
    home = tmp_path / 'lab/home'
    monkeypatch.setattr(run, 'ensure_lab', lambda *a, **k: dict(home=str(home), python=sys.executable,
                       receipt={}, env={'EIDOS_STUDY_HOME': str(home)}))
    monkeypatch.setattr(run.lab, 'start', lambda *a, **k: None)
    stop = Mock()
    monkeypatch.setattr(run.lab, 'stop', stop)
    original_reserve = run.reserve_spend
    def interrupted(*a, **k):
        original_reserve(*a, **k)
        assert json.loads(ledger.read_text())['status'] == 'reserved'
        signal.raise_signal(signal.SIGTERM)
    # Raise in the main thread, before a real child can dispatch.
    monkeypatch.setattr(run, 'reserve_spend', interrupted)
    class InlineExecutor:
        def __init__(self, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def map(self, function, items):
            return map(function, items)
    monkeypatch.setattr(run, 'ThreadPoolExecutor', InlineExecutor)
    with pytest.raises(RuntimeError, match='termination requested'):
        run.execute(args)
    stop.assert_called_once()
    row = json.loads(ledger.read_text())
    assert row['status'] == 'uncertain' and row['usd'] > 0
    assert read_json(args.out / 'manifest.json')['exit_status'] == 2


def test_missing_reference_error_redacts_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, 'execute', Mock(side_effect=FileNotFoundError(str(tmp_path / 'PRIVATE_REFERENCE'))))
    assert run.main(['--treatment', 'stub', '--cards', 'N01', '--out', str(tmp_path / 'out')]) == 2
    output = capsys.readouterr()
    assert str(tmp_path) not in output.err and 'PRIVATE_REFERENCE' not in output.err


def test_heldout_live_uses_verified_build_and_private_lab_home(tmp_path, monkeypatch):
    variant = run.variants(options(tmp_path))[0]
    variant['stub'] = False
    frozen = tmp_path / 'frozen/ip-build'
    frozen.mkdir(parents=True)
    variant['lab_root'] = str(frozen)
    private = tmp_path / 'private'
    write_json(frozen / 'ports.json', dict(api=40001, pg=40002, temporal=30100, mcp=40003))
    verified = []
    def configure(home, python, stub, commit):
        verified.append((home, python, stub, commit))
        os.environ.update(EIDOS_STUDY_HOME=str(home), EIDOS_STUDY_ROOT=str(home.parent),
                          EIDOS_STUDY_PYTHON=str(python), EIDOS_STUDY_MCP_PORT='40003')
        return {'verified': True}
    def initialize(*a, **k):
        home = Path(os.environ['EIDOS_STUDY_HOME'])
        assert home.is_relative_to(private)
        assert os.environ['EIDOS_STUDY_PYTHON'] == str(frozen / 'venv/bin/python')
        write_json(home / 'config/lab.json', {'lab_python': str(frozen / 'venv/bin/python')})
    monkeypatch.setattr(run, 'configure_lab', configure)
    monkeypatch.setattr(run.lab, 'cli', initialize)
    with run.environment({}):
        result = run.ensure_lab(variant, private_root=private)
    assert verified == [(frozen / 'home', frozen / 'venv/bin/python', False, variant['commit'])]
    assert Path(result['home']) == private / 'labs/ip-build/home'
    assert result['env']['EIDOS_STUDY_ROOT'] == str(private / 'labs/ip-build')
    assert not (frozen / 'home').exists()


def test_archived_receipt_does_not_release_missing_final_reservation(tmp_path):
    args, selected, plan, ledger = live_budget(tmp_path, 'openai-default')
    budget = run.budget_check(args, selected, plan)
    reservations = []
    run.reserve_spend(args, budget, selected, plan, plan[0], reservations)
    root = tmp_path / 'run'
    for arm in (2, 4):
        write_json(root / f'runs/incidents/N01-r1-old/arm{arm}/run_record.json',
                   dict(block_id='N01-r1', tokens={'input': 100, 'output': 20}, billed_usd=.1))
    run.account(args, budget, [root], selected, 'test', reservations=reservations)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert rows[0]['status'] == 'uncertain'
    assert sum(row['usd'] for row in rows) == pytest.approx(2.4)


def test_heldout_preflight_git_error_does_not_leak(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    write_json(tmp_path / 'H/pool.json', synthetic_manifest('H', ('R0001',)))
    assert heldout.main(['--pair', 'stub@' + str(tmp_path / 'R0001'), 'stub@HEAD', '--threshold', '.1',
                         '--note', 'test', '--private-root', str(tmp_path)]) == 2
    captured = capfd.readouterr()
    assert captured.out == '' and captured.err == 'CalledProcessError\n'
    assert not (tmp_path / 'consultations.json').exists()


@pytest.mark.parametrize('unknown', [False, True])
def test_usage_all_variants_arms_retries_costs_and_quota(tmp_path, unknown):
    args, selected, _, ledger = live_budget(tmp_path, 'openai-default')
    selected.append({**selected[0], 'index': 1, 'label': 'candidate'})
    roots = [tmp_path / 'incumbent', tmp_path / 'candidate']
    for index, root in enumerate(roots):
        for arm in (2, 4):
            write_json(root / f'runs/N01-r1/arm{arm}/run_record.json',
                       dict(tokens={'input': 1000000, 'output': 1000000}, wall_s=3))
        # Both arms of the first attempt were archived by the launcher.
        for arm in (2, 4):
            write_json(root / f'runs/incidents/N01-r1-first/arm{arm}/run_record.json',
                       dict(tokens={} if unknown and index else {'input': 10, 'output': 20},
                            billed_usd=None if unknown and index else .1, wall_s=2))
    args.quota_file = tmp_path / 'quota.json'
    write_json(args.quota_file, dict(weekly_remaining_fraction=.75))
    budget = dict(live=True, spend_ledger=str(ledger), quota_before=dict(weekly_remaining_fraction=.8))
    run.account(args, budget, roots, selected, 'paired')
    usage = read_json(args.out / 'usage.json')
    assert usage['runs'] == 8 and usage['time_seconds'] == 20
    assert usage['quota_weekly_percent'] == pytest.approx(5)
    assert usage['per_variant']['live-test'] == dict(runs=4, usd=pytest.approx(1.1))
    assert usage['per_variant']['candidate']['runs'] == 4
    if unknown:
        assert usage['usd'] is None and usage['per_variant']['candidate']['usd'] is None
    else:
        assert usage['usd'] == pytest.approx(2.2)
        assert usage['per_variant']['candidate']['usd'] == pytest.approx(1.1)
    assert len(ledger.read_text().splitlines()) == 8


@pytest.mark.parametrize('fixture', [False, True])
def test_usage_model_free_attempts_need_no_spend_ledger(tmp_path, fixture):
    args = options(tmp_path)
    selected = run.variants(args)
    selected[0].update(stub=not fixture, fixture=fixture)
    root = tmp_path / 'variant'
    for suffix in ('N01-r1/arm4', 'incidents/N01-r1-first/arm4'):
        write_json(root / 'runs' / suffix / 'run_record.json', dict(wall_s=2, billed_usd=None))
    run.account(args, dict(live=False), [root], selected, 'model-free')
    usage = read_json(args.out / 'usage.json')
    assert usage == dict(schema='eidos-exp-usage/1', runs=2, usd=0, quota_weekly_percent=None,
                         time_seconds=4, per_variant={selected[0]['label']: dict(runs=2, usd=0)})
    assert not (args.out / 'spend.jsonl').exists()


def test_heldout_usage_refuses_public_destination(tmp_path):
    args = options(tmp_path)
    with pytest.raises(ValueError, match='private root'):
        run.account(args, dict(live=False), [], [], 'held-out', private_root=tmp_path / 'private', out=tmp_path / 'public')
    assert not (tmp_path / 'public').exists()


@pytest.mark.parametrize('reading', ['before', 'after'])
def test_usage_without_both_quota_readings_keeps_quota_unknown(tmp_path, reading):
    args = options(tmp_path)
    args.quota_file = tmp_path / 'quota.json'
    budget = dict(live=False)
    if reading == 'before':
        budget['quota_before'] = dict(weekly_remaining_fraction=.8)
    else:
        write_json(args.quota_file, dict(weekly_remaining_fraction=.7))
    run.account(args, budget, [tmp_path / 'empty'], run.variants(args), 'partial-quota')
    assert read_json(args.out / 'usage.json')['quota_weekly_percent'] is None


def test_paired_pool_cli_scores_and_describes_each_arm(tmp_path, monkeypatch):
    monkeypatch.setenv('EIDOS_EXP_TEST_ROOT', str(tmp_path))
    pool = fabricated_pool(tmp_path)
    monkeypatch.setenv('EIDOS_IMP_PRIVATE_ROOT', str(tmp_path / 'private'))
    monkeypatch.setenv('EIDOS_EXTRA_POOL_MANIFESTS', str(pool / 'pool.json'))
    monkeypatch.setenv('EIDOS_POOLS_ROOT', str(tmp_path))
    out = tmp_path / 'pair'
    process = subprocess.run([sys.executable, '-m', 'study.exp.run', '--pair', 'stub@HEAD', 'stub@HEAD',
                              '--pool', 'FAB', '--arms', '2,4', '--out', str(out)],
                             cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert process.returncode == 0, process.stdout + process.stderr + (out / 'run.log').read_text()
    manifest = read_json(out / 'manifest.json')
    assert len(manifest['scorecards']) == 4
    for variant in manifest['variants']:
        assert not (out / variant['label'] / 'scorecard.json').exists()
        for arm in (2, 4):
            score = read_json(out / variant['label'] / f'arm{arm}/scorecard.json')
            assert {row['arm'] for row in score['individual']} == {arm}
    assert manifest['paired'] == {'2': 'arm2/paired.md', '4': 'arm4/paired.md'}
    for path in manifest['paired'].values():
        assert '"mean_delta": 0.0' in (out / path).read_text()
        assert 'No decision' in (out / path).read_text()
    assert '## Arm 2' in (out / 'paired.md').read_text() and '## Arm 4' in (out / 'paired.md').read_text()
    usage = read_json(out / 'usage.json')
    assert usage['runs'] == 8 and usage['usd'] == 0
    assert usage['per_variant'] == {v['label']: dict(runs=4, usd=0) for v in manifest['variants']}
