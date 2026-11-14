"""Epoch transitions, budget isolation and frozen-comparator authorization."""
import hashlib
import json
import shutil

import numpy as np
import pytest

from study.practice import guard, ledger, pipelines, run_gate
from study.practice.build_cards import build
from study.practice.generators import generate, registry


@pytest.fixture
def epoch_data(tmp_path):
    root = tmp_path / 'epoch2'
    build(root, instances=1)
    event = ledger.open_epoch(2, data_roots=[root], reason='r5 controller authorization')
    classes = [g['id'] for g in registry()]
    return root, classes, event


def receipt(root, classes, name, kind='candidate'):
    return ledger.register(evaluation_id=name, candidate_digest=ledger.comparator_digest() if kind == 'harness-validation' else name,
                           classes=classes, primary_verdict='named transfer', kind=kind, data_root=root)


def test_step2_candidate_scores_epoch2_after_harness_validation(epoch_data):
    root, classes, event = epoch_data
    validation = receipt(root, classes, 'harness', 'harness-validation')
    bundle, controller = root / 'bundles/P0005', root / 'controller/P0005'
    table = pipelines.compute_table(bundle, controller / 'test_inputs.npz', controller / 'test_labels.npz', registration=validation)
    assert table['in_fold_ok']
    assert not [e for e in ledger.verify_chain() if e['event'] == 'registration' and e['kind'] == 'candidate']
    candidate = receipt(root, classes, 'step2-pipeline-C')
    # An actual step-2 custom recipe can score fresh protected labels under a
    # candidate receipt even after the comparator has consumed its own budget.
    spec = dict(id='step2', family='linear', complexity=0, pipelines=['A0', 'A1', 'A2', 'A3'])
    scored = pipelines.compute_table(bundle, controller / 'test_inputs.npz', controller / 'test_labels.npz',
                                     specs=[spec], registration=candidate)
    assert scored['rows'][0]['test_score'] is not None
    assert candidate['holdout_epoch'] == event['epoch'] == 2
    assert ledger.require_bundle_registration(candidate, bundle) == candidate
    receipt(root, classes, 'step2-second')
    with pytest.raises(ValueError, match='two-burn'):
        receipt(root, classes, 'step2-third')


def test_epoch1_history_and_burns_preserved_and_retired(tmp_path):
    old = [ledger.register(evaluation_id=f'legacy-{i}', candidate_digest=str(i), classes=['gen-002'], primary_verdict='legacy') for i in range(2)]
    original = (ledger.LEDGER_ROOT / 'entries.jsonl').read_bytes()
    root = tmp_path / 'epoch2'
    build(root, instances=1)
    ledger.open_epoch(2, data_roots=[root], reason='explicit new data; no reset')
    assert (ledger.LEDGER_ROOT / 'entries.jsonl').read_bytes().startswith(original)
    for entry in old:
        with pytest.raises(ValueError, match='retired'):
            ledger.require_registration(entry, candidate=True)
        with pytest.raises(ValueError, match='retired'):
            ledger.record_verdict(entry, {'verdict': 'accept-named'}, classes=['gen-002'])
    with pytest.raises(ValueError, match='retired'):
        ledger.register(evaluation_id='new-on-old', candidate_digest='C', classes=['gen-002'], primary_verdict='transfer', epoch=1)
    new = receipt(root, [g['id'] for g in registry()], 'step2')
    assert new['holdout_epoch'] == 2
    assert [e for e in ledger.verify_chain() if e['event'] == 'registration'][:2] == old


@pytest.mark.parametrize('attack', ['copied-root', 'changed-index', 'wrong-epoch', 'no-binding', 'third-harness', 'changed-source', 'candidate-verdict', 'custom-recipe', 'implicit-purpose'])
def test_harness_cannot_bypass_epoch_or_candidate_accounting(epoch_data, tmp_path, monkeypatch, attack):
    root, classes, _ = epoch_data
    validation = receipt(root, classes, 'harness', 'harness-validation')
    bundle, controller = root / 'bundles/P0005', root / 'controller/P0005'
    with pytest.raises(ValueError):
        if attack == 'copied-root':
            clone = tmp_path / 'clone'
            shutil.copytree(root, clone)
            receipt(clone, classes, 'copy')
        elif attack == 'changed-index':
            with (root / 'index.json').open('a') as stream:
                stream.write(' ')
            ledger.require_bundle_registration(validation, bundle, purpose='comparators')
        elif attack == 'wrong-epoch':
            ledger.register(evaluation_id='wrong', candidate_digest='C', classes=classes, primary_verdict='x', data_root=root, epoch=3)
        elif attack == 'no-binding':
            ledger.register(evaluation_id='no-root', candidate_digest='C', classes=classes, primary_verdict='x', epoch=2)
        elif attack == 'third-harness':
            receipt(root, classes, 'harness2', 'harness-validation')
            receipt(root, classes, 'harness3', 'harness-validation')
        elif attack == 'changed-source':
            monkeypatch.setattr(ledger, 'comparator_digest', lambda: 'changed')
            ledger.require_bundle_registration(validation, bundle, purpose='comparators')
        elif attack == 'candidate-verdict':
            guard.evaluate({g: [.9]*4 for g in classes}, {g['id']: g['role'] for g in registry()}, registration=validation)
        elif attack == 'custom-recipe':
            pipelines.compute_table(bundle, controller / 'test_inputs.npz', controller / 'test_labels.npz', specs=[{}], registration=validation)
        else:
            ledger.require_bundle_registration(validation, bundle)
    assert not [e for e in ledger.verify_chain() if e['event'] == 'registration' and e['kind'] == 'candidate']


def test_harness_diagnostic_is_never_a_candidate_verdict(epoch_data):
    root, classes, _ = epoch_data
    validation = receipt(root, classes, 'harness', 'harness-validation')
    result = guard.evaluate({g: [.9]*4 for g in classes}, {g['id']: g['role'] for g in registry()},
                            registration=validation, harness_validation=True, in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
    assert result['harness_validation_only'] and result['candidate_verdict'] is False
    assert ledger.verify_chain()[-1]['event'] == 'harness-validation-result'
    with pytest.raises(ValueError, match='candidate registration'):
        ledger.record_verdict(validation, result, classes=classes)


def test_all_protected_instances_have_new_seeds_data_and_tokens():
    for entry in registry():
        if entry['role'] not in ('holdout', 'holdout-class'):
            continue
        old = dict(entry, seed_stream=entry['seed_stream'] - 1000000000)
        for i in range(4):
            a, b = generate(old, i), generate(entry, i)
            assert entry['holdout_epoch'] == 2
            for split in a.splits:
                assert not np.array_equal(a.splits[split]['x'], b.splits[split]['x'])
                assert not set(a.splits[split]['ids']) & set(b.splits[split]['ids'])


def test_harness_cli_candidate_kind_refused_before_spending_burns(tmp_path):
    with pytest.raises(ValueError, match='never candidate burns'):
        run_gate.run(tmp_path, tmp_path, evaluation_id='bad', candidate_digest='C', kind='candidate')
    assert ledger.verify_chain() == []


def test_epoch_pins_index_digest_and_full_protected_coverage(epoch_data):
    root, _, event = epoch_data
    assert event['index_bindings'][0]['index_digest'] == hashlib.sha256((root / 'index.json').read_bytes()).hexdigest()
    assert len(event['index_bindings'][0]['protected_cards']) == 14
    assert json.loads((root / 'index.json').read_text())['holdout_epoch'] == 2


def test_single_loss_cells_remain_visible_but_are_not_hard_targets(epoch_data):
    from study.practice.operating import simulate
    root, classes, _ = epoch_data
    validation = receipt(root, classes, 'harness', 'harness-validation')
    roles = {g['id']: g['role'] for g in registry()}
    report = simulate({g: [-.8, -.2, .2, .8] for g in classes}, roles, registration=validation, trials=8)
    single = [r for r in report['rows'] if r['scenario'].startswith('single-loss-4/')]
    assert single and all(r['target'] is None and r['target_met'] is None for r in single)
    assert report['single_generator_targets_met'] is None
    assert report['single_generator_diagnostic']
    assert report['targets_met'] == (all(r['target_met'] for r in report['rows']
        if r['primaries'] == 1 and r['target'] is not None) and all(r['target_met'] for r in report['catastrophic_neutral']))
