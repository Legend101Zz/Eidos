"""Adversarial regressions for the r2 re-review bypasses and mixed harm."""
import json
from collections import Counter

import numpy as np
import pytest

from study.practice import guard, ledger
from study.practice.generators import generate, registry
from study.tests.practice_support import registration


def test_scoring_api_and_cli_refuse_missing_evaluation(tmp_path):
    from study.practice.pipelines import compute_table
    from study.practice.run_gate import run
    with pytest.raises(ValueError, match='registration'):
        compute_table(tmp_path, tmp_path, tmp_path)
    for options in ({}, {'evaluation_id': 'x'}, {'candidate_digest': 'x'}):
        with pytest.raises(ValueError, match='every scoring run'):
            run(tmp_path, tmp_path, **options)
    from unittest.mock import patch

    from study.practice.run_gate import main
    with patch('sys.argv', ['run_gate', '--data', str(tmp_path), '--out', str(tmp_path)]):
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2


def test_accept_and_reject_are_durably_recorded_and_receipt_bound():
    roles = {f'g{i}': 'holdout' for i in range(12)}
    effects = {g: [.6, .8, 1., .8] for g in roles}
    options = dict(in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
    with pytest.raises(ValueError, match='registration'):
        guard.evaluate(effects, roles, **options)
    receipt = registration(roles)
    for altered in (dict(receipt, candidate_digest='different'), dict(receipt, classes=['x'])):
        with pytest.raises(ValueError, match='does not match'):
            guard.evaluate(effects, roles, registration=altered, **options)
    for checks in (options, dict(options, in_fold_ok=False)):
        result = guard.evaluate(effects, roles, registration=receipt, **checks)
        event = ledger.verify_chain()[-1]
        assert event['event'] == 'verdict'
        assert event['result']['verdict'] == result['verdict']
        assert event['hash'] == result['ledger_event_hash']
    assert result['verdict'] == 'reject-in-fold'
    with pytest.raises(ValueError, match='generator set'):
        guard.evaluate({'other': [.8]*4}, {'other': 'design'}, registration=receipt, **options)


@pytest.mark.parametrize('attack', ['edit', 'middle-delete', 'tail-delete', 'file-delete', 'reorder', 'forge'])
def test_chain_verification_blocks_every_run_after_tampering(attack, tmp_path):
    for i in range(3):
        registration([f'g{i}'])
    path = ledger.LEDGER_ROOT / 'entries.jsonl'
    lines = path.read_bytes().splitlines(keepends=True)
    if attack == 'file-delete':
        path.unlink()
    elif attack == 'edit':
        path.write_bytes(b''.join(lines).replace(b'test-fixture', b'evil-fixture'))
    elif attack == 'middle-delete':
        path.write_bytes(lines[0] + lines[-1])
    elif attack == 'tail-delete':
        path.write_bytes(b''.join(lines[:-1]))
    elif attack == 'reorder':
        path.write_bytes(b''.join(reversed(lines)))
    else:
        entry = json.loads(lines[-1])
        entry['candidate_digest'] = 'forged'
        path.write_bytes(b''.join(lines[:-1]) + json.dumps(entry).encode() + b'\n')
    with pytest.raises(ValueError):
        ledger.verify_chain()
    with pytest.raises(ValueError):
        registration(['new'])


def test_missing_fixed_ledger_is_not_automatically_recreated(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, 'LEDGER_ROOT', tmp_path / 'missing')
    with pytest.raises(ValueError, match='fixed programme ledger is missing'):
        registration(['g'])
    assert not ledger.LEDGER_ROOT.exists()


def test_new_output_directory_cannot_reset_burns():
    for i in range(2):
        ledger.register(evaluation_id=str(i), candidate_digest=str(i), classes=['g'], primary_verdict='transfer')
    with pytest.raises(ValueError, match='two-burn'):
        ledger.register(evaluation_id='different-out', candidate_digest='new', classes=(g for g in ['g']), primary_verdict='transfer')
    # Construction is still logged, but cannot erase or change candidate burns.
    ledger.register(evaluation_id='design-only', candidate_digest='test', classes=['gen-001'],
                    primary_verdict='construction', kind='construction')
    assert len(ledger.verify_chain()) == 3


@pytest.mark.parametrize('generator_id', ['gen-008', 'gen-009', 'gen-015'])
def test_public_row_stream_cannot_invert_wine_or_digits_labels(generator_id):
    entry = next(e for e in registry() if e['id'] == generator_id)
    data = generate(entry, 0)
    n = sum(entry['sizes'].values())
    public = np.random.default_rng(np.random.SeedSequence([entry['seed_stream'], 718239])).integers(
        0, np.iinfo(np.int64).max, size=n, dtype=np.int64)
    held = data.splits['test']
    # The reviewer's lookup attack has no matching token, hence no recoverable label.
    assert not set(public) & set(held['ids'])
    assert np.array_equal(generate(entry, 0).splits['test']['ids'], held['ids'])


def test_nuisance_templates_do_not_dominate_machinery_holdouts():
    holdouts = [e for e in registry() if e['role'] == 'holdout']
    counts = Counter(e['nuisance_template'] for e in holdouts)
    assert max(counts.values()) <= len(holdouts) / 3
    # Each declared structure is exercised by generation, with genuinely distinct widths.
    widths = {generate(e, 0).splits['train']['x'].shape[1] for e in holdouts[3:]}
    assert len(widths) >= 6


def test_pooled_harm_catches_mixed_change_that_passes_machinery_gain():
    from study.practice.operating import decisions
    roles = {f'h{i}': 'holdout' for i in range(12)} | {f'd{i}': 'design' for i in range(6)} | {'c1': 'holdout-class', 'c2': 'holdout-class'}
    centered = np.array([-.8, -.3, .3, .8])
    effects = {g: centered + (.8 if r == 'holdout' else -1.) for g, r in roles.items()}
    result = guard.evaluate(effects, roles, registration=registration(roles), in_fold_ok=True,
                           null_delta=0., null_tolerance=0., limits_ok=True)
    assert result['inference']['lower90'] > 0
    assert result['pooled_harm']['elsewhere']['credible_loss']
    assert result['verdict'] == 'reject-no-harm'
    assert not decisions({g: v[None, :] for g, v in effects.items()}, roles)[0]


def test_n04_fingerprint_catches_perturbed_copy_without_factory_trace(monkeypatch):
    from sklearn.datasets import make_classification

    from study.practice import independence
    from study.practice.generators import Instance
    x, y = make_classification(n_samples=1800, n_features=30, n_informative=8, n_redundant=4,
                               n_clusters_per_class=3, class_sep=.83, flip_y=.025, random_state=12)
    # Generate before installing the lint: no observed sklearn call can reveal it.
    copy = Instance({'train': {'x': x, 'y': y}}, 'classification', {})
    assert independence.n04_fingerprint(copy)
    original = independence.generate
    monkeypatch.setattr(independence, 'generate', lambda entry, i: copy if entry['id'] == 'gen-001' else original(entry, i))
    assert ('gen-001', 'N04-statistical-fingerprint') in independence.recipe_violations()


def test_no_verdict_is_returned_when_ledger_append_fails(monkeypatch):
    receipt = registration(['g'])
    def fail(*args):
        raise OSError('simulated durable-write failure')
    monkeypatch.setattr(ledger, '_append', fail)
    with pytest.raises(OSError, match='durable-write'):
        guard.evaluate({'g': [.8]*4}, {'g': 'holdout'}, registration=receipt, in_fold_ok=False)


def test_alternative_scoring_entries_also_require_registration(tmp_path):
    from study.practice.run_gate import fresh_recipe, supplied_baseline
    with pytest.raises(ValueError, match='registration'):
        supplied_baseline(tmp_path, tmp_path, tmp_path)
    with pytest.raises(ValueError, match='registration'):
        fresh_recipe(tmp_path, tmp_path, tmp_path, {}, 'classification', .5, .02)
    from study.practice.build_cards import write_json
    indexed = tmp_path / 'data/bundles/P0001'
    write_json(tmp_path / 'data/index.json', {'cards': [{'card': 'P0001', 'generator': 'gen-011'}]})
    wrong = registration(['unrelated'])
    with pytest.raises(ValueError, match='index binding'):
        supplied_baseline(tmp_path / 'work', indexed, tmp_path, registration=wrong)
    with pytest.raises(ValueError, match='index binding'):
        fresh_recipe(tmp_path / 'work', indexed, tmp_path, {}, 'classification', .5, .02, registration=wrong)


def test_mixed_heteroskedastic_decisions_match_recorded_guard():
    from study.practice.operating import decisions
    roles = {e['id']: e['role'] for e in registry()}
    rng = np.random.default_rng(88173)
    receipt = registration(roles)
    for loss in (-.25, -.5, -1., -2.):
        draws = {g: rng.normal(.8 if role == 'holdout' else loss if role in ('design', 'holdout-class') else 0.,
                               rng.uniform(.3, 4.), (8, 4)) for g, role in roles.items()}
        for primaries in (1, 8):
            actual = []
            for i in range(8):
                result = guard.evaluate({g: v[i].tolist() for g, v in draws.items()}, roles, registration=receipt,
                    in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True,
                    primary_pvalues=[1.] * (primaries - 1),
                    effective_instances={g: 1 for g in ('gen-008', 'gen-009', 'gen-015')})
                actual.append(result['verdict'].startswith('accept'))
            assert np.array_equal(decisions(draws, roles, other_primaries=[1.] * (primaries - 1)), actual)


@pytest.mark.parametrize('harmed_role', ['design', 'holdout-class'])
def test_each_role_loss_rejects_even_when_elsewhere_pool_is_positive(harmed_role):
    roles = {f'h{i}': 'holdout' for i in range(12)} | {f'd{i}': 'design' for i in range(6)} | {'c1': 'holdout-class', 'c2': 'holdout-class'}
    centered = np.array([-.1, -.03, .03, .1])
    effects = {g: centered + (-1. if role == harmed_role else .8 if role == 'holdout' else 10.) for g, role in roles.items()}
    result = guard.evaluate(effects, roles, registration=registration(roles), in_fold_ok=True,
                           null_delta=0., null_tolerance=0., limits_ok=True)
    assert result['pooled_harm']['elsewhere']['upper90'] > 0
    assert result['pooled_harm'][harmed_role]['credible_loss']
    assert result['verdict'] == 'reject-no-harm'


def test_design_reuse_does_not_block_independent_holdout_replacement():
    for i in range(3):
        ledger.register(evaluation_id=f'candidate-{i}', candidate_digest=str(i),
                        classes=['gen-001', f'replacement-holdout-{i}'], primary_verdict='transfer')
    assert len(ledger.verify_chain()) == 3


def test_verdict_cannot_omit_registered_generators_to_hide_harm():
    receipt = registration(['helpful', 'harmful'])
    with pytest.raises(ValueError, match='generator set'):
        guard.evaluate({'helpful': [.8]*4}, {'helpful': 'holdout'}, registration=receipt,
                       in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
