"""r4 adversarial authorization, loss and transformed-recipe regressions."""
import json
import shutil

import numpy as np
import pytest
from sklearn.datasets import make_classification
from sklearn.preprocessing import QuantileTransformer

from study.practice import guard, ledger
from study.practice.build_cards import build
from study.practice.generators import Instance
from study.practice.independence import n04_fingerprint
from study.practice.pipelines import compute_table
from study.tests.practice_support import registration


@pytest.mark.parametrize('classes', [['gen-017'], ['gen-001', 'gen-002'], ['unknown']])
def test_construction_cannot_register_any_holdout_or_unknown(classes):
    with pytest.raises(ValueError, match='construction registration'):
        ledger.register(evaluation_id='escape', candidate_digest='copy', classes=classes,
                        primary_verdict='escape', kind='construction')
    assert ledger.verify_chain() == []


def test_construction_cannot_emit_accept_or_reject():
    receipt = ledger.register(evaluation_id='design', candidate_digest='x', classes=['gen-001'],
                              primary_verdict='design-only', kind='construction')
    for in_fold in (True, False):
        with pytest.raises(ValueError, match='candidate registration'):
            guard.evaluate({'gen-001': [.9]*4}, {'gen-001': 'design'}, registration=receipt,
                           in_fold_ok=in_fold, null_delta=0., null_tolerance=0., limits_ok=True)
    with pytest.raises(ValueError, match='candidate registration'):
        ledger.record_verdict(receipt, {'verdict': 'accept-named'}, classes=['gen-001'])
    assert len(ledger.verify_chain()) == 1


def test_construction_copy_of_gen017_fails_before_label_load(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    build(data, generator_ids=['gen-017'], instances=1)
    copied = tmp_path / 'copy' / 'P0065'
    shutil.copytree(data / 'bundles/P0065', copied)
    receipt = ledger.register(evaluation_id='construction', candidate_digest='x', classes=['gen-001'],
                              primary_verdict='design-only', kind='construction')
    monkeypatch.setattr(np, 'load', lambda *a, **k: pytest.fail('must refuse before loading any labels'))
    with pytest.raises(ValueError, match='index binding'):
        compute_table(copied, data / 'controller/P0065/test_inputs.npz', data / 'controller/P0065/test_labels.npz', registration=receipt)


@pytest.mark.parametrize('attack', ['absent-binding', 'copy', 'missing-index', 'changed-index', 'bundle', 'controller', 'labels-path'])
def test_scoring_requires_exact_registered_index_and_content(tmp_path, monkeypatch, attack):
    data = tmp_path / 'data'
    build(data, generator_ids=['gen-017'], instances=1)
    receipt = ledger.register(evaluation_id='bound', candidate_digest='x', classes=['gen-017'],
                              primary_verdict='transfer', data_root=None if attack == 'absent-binding' else data)
    bundle = data / 'bundles/P0065'
    controller = data / 'controller/P0065'
    labels = controller / 'test_labels.npz'
    if attack == 'copy':
        copied = tmp_path / 'copied/P0065'
        shutil.copytree(bundle, copied)
        bundle = copied
    elif attack == 'missing-index':
        (data / 'index.json').unlink()
    elif attack == 'changed-index':
        index = data / 'index.json'
        index.write_text(index.read_text() + '\n')
    elif attack == 'bundle':
        (bundle / 'project.py').write_text('changed')
    elif attack == 'controller':
        labels.write_bytes(b'changed')
    elif attack == 'labels-path':
        labels = tmp_path / 'elsewhere.npz'
    monkeypatch.setattr(np, 'load', lambda *a, **k: pytest.fail('must authorize before opening labels'))
    with pytest.raises(ValueError, match='index|content'):
        compute_table(bundle, controller / 'test_inputs.npz', labels, registration=receipt)


def test_index_cannot_relabel_holdout_as_design(tmp_path):
    data = tmp_path / 'data'
    build(data, generator_ids=['gen-017'], instances=1)
    path = data / 'index.json'
    index = json.loads(path.read_text())
    index['cards'][0]['role'] = 'design'
    path.write_text(json.dumps(index))
    with pytest.raises(ValueError, match='trusted registry'):
        ledger.register(evaluation_id='fake-role', candidate_digest='x', classes=['gen-017'],
                        primary_verdict='transfer', data_root=data)


def test_each_append_is_witnessed_without_rewriting_history():
    receipt = registration(['g'])
    path = ledger.LEDGER_ROOT / 'HEADS.log'
    original = path.read_bytes()
    result = guard.evaluate({'g': [.9]*4}, {'g': 'holdout'}, registration=receipt, in_fold_ok=False)
    assert path.read_bytes().startswith(original)
    assert path.read_text().splitlines() == [f"{e['sequence']} {e['hash']}" for e in ledger.verify_chain()]
    assert path.read_text().splitlines()[-1].endswith(result['ledger_event_hash'])


@pytest.mark.parametrize('role', ['design', 'holdout', 'holdout-class'])
def test_single_credible_catastrophic_loss_rejects_despite_positive_pools(role):
    roles = {f'h{i}': 'holdout' for i in range(12)} | {f'd{i}': 'design' for i in range(6)} | {'c1': 'holdout-class', 'c2': 'holdout-class'}
    target = next(g for g, r in roles.items() if r == role)
    effects = {g: np.array([-.1, -.03, .03, .1]) + (-4. if g == target else 10.) for g in roles}
    result = guard.evaluate(effects, roles, registration=registration(roles),
                           in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
    assert result['catastrophic_loss'][target]['reject']
    assert result['verdict'] == 'reject-no-harm'


@pytest.mark.parametrize('seed', [12, 24, 36])
@pytest.mark.parametrize('redundant', [0, 1, 2, 4])
@pytest.mark.parametrize('transform', ['rotation', 'jitter', 'monotone', 'quantile', 'rotation-quantile'])
def test_fingerprint_catches_reviewers_disguises(seed, redundant, transform):
    x, y = make_classification(n_samples=1800, n_features=30, n_informative=8, n_redundant=redundant,
                               n_clusters_per_class=3, class_sep=.83, flip_y=.025, random_state=seed)
    rng = np.random.default_rng(934)
    if transform.startswith('rotation'):
        x = x @ np.linalg.qr(rng.normal(size=(30, 30)))[0]
    if transform == 'jitter':
        x += rng.normal(0, .02, x.shape)
    if transform == 'monotone':
        x = np.tanh(x / 4)
    if 'quantile' in transform:
        x = QuantileTransformer(n_quantiles=500, random_state=41).fit_transform(x)
    assert n04_fingerprint(Instance({'train': {'x': x, 'y': y}}, 'classification', {}))


@pytest.mark.parametrize('settings', [
    {'n_samples': 800}, {'n_samples': 6000}, {'n_features': 16}, {'n_features': 40},
    {'n_informative': 6}, {'n_informative': 10}, {'n_clusters_per_class': 1}, {'n_clusters_per_class': 5},
    {'class_sep': .5}, {'class_sep': 1.4}, {'flip_y': .1},
])
def test_fingerprint_preserves_previous_parameter_sensitivity(settings):
    options = dict(n_samples=1800, n_features=30, n_informative=8, n_redundant=4,
                   n_clusters_per_class=3, class_sep=.8, flip_y=.02, random_state=12)
    options.update(settings)
    x, y = make_classification(**options)
    assert n04_fingerprint(Instance({'train': {'x': x, 'y': y}}, 'classification', {}))


def test_cold_one_instance_per_generator_emits_measurements_without_inference(tmp_path, monkeypatch):
    from study.practice import run_gate
    from study.practice.build_cards import write_json
    from study.practice.pipelines import source_digest
    # Exercise assembly over tiny valid cached tables: a single instance cannot
    # support a t interval and must never fabricate an acceptance/rejection.
    data = tmp_path / 'data'
    build(data, generator_ids=['gen-001'], instances=1)
    out = tmp_path / 'gate'
    item = json.loads((data / 'index.json').read_text())['cards'][0]
    bundle, controller = data / 'bundles' / item['card'], data / 'controller' / item['card']
    key = run_gate.cache_key(bundle, controller)
    table = dict(cache_key=key, source_digest=source_digest(), in_fold_ok=True, in_fold_audits=[{'passed': True}],
                 task_type='classification', rows=[{'spec': {'id': 'r0', 'complexity': 0, 'pipelines': ['A0', 'A1', 'A2', 'A3']},
                                                   'test_se': .01, 'validation_score': .8, 'cv_score': .8}],
                 wall_seconds=1., comparator_paired={'se': .01})
    write_json(out / 'cache' / f'{key}.json', table)
    monkeypatch.setattr(run_gate, 'replay', lambda t: dict(pipelines={name: {'test_score': .8} for name in ('A0', 'A1', 'A2', 'A3')}))
    result = run_gate.run(data, out, evaluation_id='cold', candidate_digest='x')
    assert 'verdict' not in result['comparator_guard_descriptive']
    assert result['cards'][0]['measurement_seconds'] == 1.


@pytest.mark.parametrize('attack', [None, 'holdout-entry', 'add-generator', 'forged-previous'])
def test_design_index_revision_preserves_protected_entries_and_burns(tmp_path, attack):
    from study.practice.build_cards import tree_digest, write_json
    data = tmp_path / 'data'
    build(data, generator_ids=['gen-001', 'gen-017'], instances=1)
    options = dict(evaluation_id='bound', candidate_digest='frozen', classes=['gen-001', 'gen-017'],
                   primary_verdict='transfer', data_root=data)
    receipt = ledger.register(**options)
    previous = tmp_path / 'previous-index.json'
    shutil.copyfile(data / 'index.json', previous)
    index = json.loads(previous.read_text())
    design = data / 'bundles/P0001'
    (design / 'project.py').write_text('# design-only revision\n')
    index['cards'][0]['bundle_digest'] = tree_digest(design)
    if attack == 'holdout-entry':
        index['cards'][1]['instance'] = 3
    elif attack == 'add-generator':
        index['cards'].append(dict(card='extra', generator='gen-002', role='holdout'))
    elif attack == 'forged-previous':
        previous.write_text(previous.read_text() + '\n')
    write_json(data / 'index.json', index)
    with pytest.raises(ValueError, match='binding'):
        ledger.require_bundle_registration(receipt, design)
    if attack:
        with pytest.raises(ValueError, match='previous index|generator sets|protected holdout'):
            ledger.revise_design_index(receipt, previous_index=previous)
        return
    event = ledger.revise_design_index(receipt, previous_index=previous)
    assert event['event'] == 'design-index-revision'
    assert ledger.require_bundle_registration(receipt, design) == receipt
    assert ledger.register(**options) == receipt
    assert len([e for e in ledger.verify_chain() if e['event'] == 'registration']) == 1
    ledger.register(**dict(options, evaluation_id='second', candidate_digest='second'))
    with pytest.raises(ValueError, match='two-burn'):
        ledger.register(**dict(options, evaluation_id='third', candidate_digest='third'))
    with pytest.raises(ValueError, match='cannot be changed'):
        ledger.register(**dict(options, candidate_digest='mutated'))


def test_fingerprint_never_accesses_held_labels():
    class SealedSplit(dict):
        def __getitem__(self, key):
            pytest.fail('fingerprint lint must use visible rows only')
    x, y = make_classification(n_samples=1800, n_features=30, n_informative=8, n_redundant=2,
                               n_clusters_per_class=3, random_state=12)
    data = Instance({'train': {'x': x, 'y': y}, 'test': SealedSplit()}, 'classification', {})
    assert n04_fingerprint(data)


def test_cached_inference_and_programme_sensitivity_match_recorded_decisions():
    from study.practice.operating import decisions
    roles = {f'h{i}': 'holdout' for i in range(12)} | {f'd{i}': 'design' for i in range(6)}
    receipt = registration(roles)
    rng = np.random.default_rng(884)
    cache = {}
    for scale in (.5, 1., 1.5):
        draws = {g: rng.normal(.9, scale * rng.uniform(.3, 3.), (10, 4)) for g in roles}
        diagnostic = decisions(draws, roles, diagnostics=True, inference_cache=cache)
        repeated = decisions(draws, roles, diagnostics=True, inference_cache=cache)
        assert np.array_equal(repeated['accepted'], diagnostic['accepted'])
        assert np.array_equal(repeated['primary_p'], diagnostic['primary_p'])
        for primaries in (1, 8):
            derived = diagnostic['accepted'] & (diagnostic['primary_p'] <= .05 / primaries)
            scalar = [guard.evaluate({g: v[i].tolist() for g, v in draws.items()}, roles, registration=receipt,
                      in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True,
                      primary_pvalues=[1.] * (primaries-1))['verdict'].startswith('accept') for i in range(10)]
            assert np.array_equal(derived, scalar)
