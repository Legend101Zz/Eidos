"""Review regressions: mechanism independence, IDs and behavioural fold leakage."""
import hashlib

import numpy as np
import pytest
from sklearn.feature_selection import SelectKBest, f_classif

from study.practice.generators import generate, registry
from study.practice.guard import evaluate


def test_dev_recipe_lint_observes_actual_factories(monkeypatch):
    from study.practice.independence import recipe_violations
    assert recipe_violations() == []
    from study.practice import generators
    original = generators.datasets.make_hastie_10_2
    def disguised(*args, **kwargs):
        return generators.datasets.make_classification(n_samples=kwargs['n_samples'], n_features=30,
            n_informative=8, n_redundant=4, n_clusters_per_class=3, class_sep=.8, flip_y=.02,
            random_state=kwargs['random_state'])
    monkeypatch.setattr(generators.datasets, 'make_hastie_10_2', disguised)
    assert recipe_violations()
    monkeypatch.setattr(generators.datasets, 'make_hastie_10_2', original)


@pytest.mark.parametrize('entry', registry(), ids=lambda g: g['id'])
def test_ids_are_random_assignments_independent_of_labels(entry):
    from scipy.stats import kruskal, spearmanr
    data = generate(entry, 0)
    ids = np.concatenate([s['ids'] for s in data.splits.values()])
    y = np.concatenate([s['y'] for s in data.splits.values()])
    old = {int.from_bytes(hashlib.sha256(f"practice:{entry['seed_stream']}:{i}".encode()).digest()[:8], 'big')
           & ((1 << 63) - 1) for i in range(len(ids))}
    assert not set(ids) & old
    if entry['task_type'] == 'classification':
        assert kruskal(*(ids[y == label] for label in np.unique(y))).pvalue > .0001
    else:
        assert spearmanr(ids, y).pvalue > .0001


def test_grouped_control_is_not_majority_saturated():
    entry = next(g for g in registry() if g['id'] == 'gen-010')
    for i in range(4):
        data = generate(entry, i)
        for split in data.splits.values():
            assert max(np.bincount(split['y'])) / len(split['y']) < .85


def test_defaults_fail_closed():
    result = evaluate({'g': [.8, .4, 1.2, .6]}, {'g': 'holdout'})
    assert result['verdict'].startswith('reject')


def test_real_supervised_pre_cv_screen_is_detected(tmp_path):
    from study.practice.build_cards import write_json, write_npz
    from study.practice.pipelines import compute_table
    rng = np.random.default_rng(991)
    bundle = tmp_path / 'bundle'
    write_json(bundle / 'task.json', {'task_type': 'classification'})
    for i, split in enumerate(('train', 'validation', 'test')):
        write_npz(bundle / f'{split}.npz', x=rng.normal(size=(200, 400)),
                  y=rng.integers(0, 2, 200), ids=np.arange(200) + i * 200)
    specs = [dict(id='linear', family='linear', complexity=0, pipelines=['A0', 'A1', 'A2', 'A3'])]
    args = (bundle, bundle / 'test.npz', bundle / 'test.npz')
    clean = compute_table(*args, specs=specs)
    leak = compute_table(*args, specs=specs, pre_cv_transform=SelectKBest(f_classif, k=5))
    assert clean['in_fold_ok']
    assert not leak['in_fold_ok']
    assert leak['rows'][0]['cv_score'] > clean['rows'][0]['cv_score'] + .05


def test_vectorised_operating_characteristics_match_real_decisions():
    from study.practice.operating import decisions
    rng = np.random.default_rng(8712)
    roles = {f'g{i}': 'holdout' for i in range(12)}
    for shift in (0, .8, -.5):
        draws = {g: rng.normal(shift, .8, (15, 4)) for g in roles}
        expected = [evaluate({g: v[i].tolist() for g, v in draws.items()}, roles,
                             in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)['verdict'].startswith('accept')
                    for i in range(15)]
        assert np.array_equal(decisions(draws, roles), expected)


def test_cached_replay_alone_cannot_certify_determinism():
    from study.practice.acceptance import determinism_evidence
    family = dict(semantic_digest='same', data_digest='same', cards=[dict(generator='g')])
    cold = {'instances': [dict(generator='g', passed=True, cache_hit=True)]}
    assert not determinism_evidence(family, family, cold, 'same')['passed']


def test_reference_criterion_cannot_pass_on_one_easy_ceiling():
    from study.practice.acceptance import reference_evidence
    family = {'cards': [dict(generator='g', role='holdout', pipelines={'A1': dict(test_score=1., test_se=.01), 'A3': dict(test_score=.99)})]}
    references = {'generators': {'g': dict(headroom=0., reference_score=1., reference_uncertainty_se=0.)}}
    assert not reference_evidence(family, references)['passed']


def test_missing_claim_checks_and_actual_false_claims_reject():
    effects = {str(i): [.5, .8, 1.2, .7] for i in range(12)}
    roles = {g: 'holdout' for g in effects}
    checks = dict(in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
    assert evaluate(effects, roles, **checks)['verdict'] == 'accept-named'
    for field in ('in_fold_ok', 'null_delta', 'null_tolerance', 'limits_ok'):
        bad = dict(checks, **{field: None})
        assert evaluate(effects, roles, **bad)['verdict'].startswith('reject')
    assert evaluate(effects, roles, **dict(checks, null_delta=.125))['verdict'] == 'reject-false-positive-claims'
    assert evaluate(effects, roles, **dict(checks, limits_ok=False))['verdict'].startswith('reject')


def test_burn_file_is_protected_and_candidate_bound(tmp_path):
    from study.practice.guard import register_burn
    path = tmp_path / 'burns.json'
    options = dict(evaluation_id='step2-c-vs-A2', classes=['g'], primary_verdict='transfer', candidate_digest='abc')
    first = register_burn(path, **options)
    # Changing evidence directory or unrelated source is absent from the identity.
    assert register_burn(path, **options) == first
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='cannot be changed'):
        register_burn(path, **dict(options, candidate_digest='changed'))


def test_vectorised_guard_keeps_holm_stepdown_stopping_rule():
    # All eight design harm p values are just above the first Holm cutoff.
    # A later smaller multiplier cannot reject after the first test did not reject.
    from scipy.stats import t

    from study.practice.operating import decisions
    roles = {f'h{i}': 'holdout' for i in range(12)} | {f'd{i}': 'design' for i in range(8)}
    centered = np.array([-.3, -.1, .1, .3])
    se = centered.std(ddof=1) / 2
    mean = -.5 + t.ppf(.003, 3) * max(se, .1)
    draws = {g: (centered + (.8 if role == 'holdout' else mean))[None, :] for g, role in roles.items()}
    result = evaluate({g: v[0].tolist() for g, v in draws.items()}, roles,
                      in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
    assert np.array_equal(decisions(draws, roles), [result['verdict'].startswith('accept')])


def test_effects_use_each_instances_own_paired_uncertainty():
    from study.practice.run_gate import effect_tables
    results = [dict(generator='g', role='holdout', paired_test_comparison={'se': se},
                    pipelines={'A1': {'test_score': .5}, 'A3': {'test_score': .52}}) for se in (.01, .02)]
    effects, _, _ = effect_tables(results)
    assert effects['g'] == pytest.approx([2., 1.])


def test_identical_finite_source_resplits_do_not_imply_precision():
    from study.practice.guard import summarize
    result = summarize([.8] * 4, effective_instances=1)
    assert result['variance'] >= 1.04 ** 2
    assert result['lower90'] < 0
    from study.practice.operating import decisions
    roles = {'gen-008': 'holdout'} | {f'g{i}': 'holdout' for i in range(11)}
    draws = {g: np.array([[.8] * 4]) for g in roles}
    assert decisions(draws, roles)[0]
    expected = evaluate({g: v[0].tolist() for g, v in draws.items()}, roles,
                        in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True,
                        effective_instances={'gen-008': 1})
    assert expected['verdict'] == 'accept-named'


def test_vectorised_full_family_and_multiplicity_match_evaluate():
    from study.practice.operating import decisions
    rng = np.random.default_rng(66789)
    roles = {g['id']: g['role'] for g in registry()}
    for shift in (0., .8):
        draws = {g: rng.normal(shift, rng.uniform(.1, 3.), (8, 4)) for g in roles}
        for primaries in (1, 8):
            expected = [evaluate({g: v[i].tolist() for g, v in draws.items()}, roles,
                                 in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True,
                                 primary_pvalues=[1.] * (primaries - 1),
                                 effective_instances={g: 1 for g in ('gen-008', 'gen-009', 'gen-015')})['verdict'].startswith('accept')
                        for i in range(8)]
            assert np.array_equal(decisions(draws, roles, other_primaries=[1.] * (primaries - 1)), expected)
