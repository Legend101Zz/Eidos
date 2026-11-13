"""Independent arithmetic and adversarial controls for the practice gate."""
import math

import pytest
from scipy.stats import t

from study.practice.guard import (
    claim_metrics,
    holm,
    normalized_gain,
    random_effects,
    sign_test,
)
from study.tests.practice_support import evaluate, guard_selftest


def test_generator_level_arithmetic():
    assert sign_test([1, 2, 3, 4]) == pytest.approx(1 / 16)
    assert sign_test([0, 0]) == 1
    result = random_effects([1, 2, 3], [0.25, 0.25, 0.25])
    assert result['mean'] == pytest.approx(2)
    assert result['tau_squared'] == pytest.approx(0.75)
    assert result['lower90'] == pytest.approx(2 - t.ppf(.95, 2) / math.sqrt(3))
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert normalized_gain(0.8, 0.8, 0.8, 0.02) == 0
    assert normalized_gain(0.82, 0.8, 0.8, 0.02) == pytest.approx(0.5)


def checked(effects, roles, **kwargs):
    return evaluate(effects, roles, in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True, **kwargs)


def test_noisy_controls_include_accepted_positive():
    result = guard_selftest()
    assert result['helpful_operator'] == 'accept-named'
    assert all(v.startswith('reject') for k, v in result.items() if k != 'helpful_operator')


def test_one_generator_cannot_make_a_class_claim():
    result = checked({'g': [.05] * 4}, {'g': 'holdout'})
    assert result['verdict'] == 'reject-insufficient-transfer'
    assert result['class_claim'] is False


def test_named_generator_harm_cannot_be_hidden_by_pooled_instances():
    result = checked({'a': [1] * 100, 'b': [-5.] * 4}, {'a': 'holdout', 'b': 'holdout'})
    assert result['verdict'] == 'reject-no-harm'


@pytest.mark.parametrize('visible,gains,equal,eligible', [
    (False, [1, 1, 1], True, True), (True, [0.1, 0.2, 0.3], True, True),
    (True, [0.1, 0.01, 0.3], True, True), (True, [1, 1, 1], False, True),
    (True, [1, 1, 1], True, False),
])
def test_claim_arithmetic_matches_grader(visible, gains, equal, eligible):
    from study.grade_block import success_rule
    result = claim_metrics([dict(visible_claim=visible, fresh_gains=gains,
                                fresh_process_equal=equal, export_eligible=eligible)], practical_bar=0.05)
    expected = success_rule({'id': 'P0001', 'min_reference_gain': 0.05},
                            {'status': 'improvement_claimed' if visible else 'no_improvement_found'},
                            {'gain': min(gains)}, reproduces=equal, guardrails=eligible, scope_ok=True)
    assert result['false_positive_claim_rate'] == float(expected['false_positive_claim'])
    assert result['verified_success_rate'] == float(expected['verified_success'])


def test_missing_fresh_seeds_fail_closed():
    result = claim_metrics([dict(visible_claim=True, fresh_gains=[], fresh_process_equal=True,
                                export_eligible=True)], practical_bar=0.05)
    assert result['false_positive_claim_rate'] == 1


def test_claim_audit_requires_traceable_visible_paired_evidence():
    from study.practice.claim_audit import audit
    evidence = {'score': {'accuracy': 0.9}, 'paired': {'split': 'validation', 'lower95': 0.01}}
    claim = dict(text='Visible accuracy improved.', status='improvement_claimed', paired_evidence_ref='paired',
                 measurements=[dict(evidence_ref='score', metric='accuracy', value=0.9)])
    assert audit(claim, evidence)['ok']
    assert not audit(dict(claim, text='Confirmed improvement'), evidence)['ok']
    assert not audit(claim, {'score': evidence['score']})['ok']
    assert not audit(claim, {'paired': evidence['paired']})['ok']


def test_unrelated_holdouts_cannot_be_pooled_into_a_class_claim():
    effects = {str(i): [0.1] * 4 for i in range(6)}
    result = checked(effects, {key: 'holdout' for key in effects})
    assert result['class_claim'] is False


def test_trace_dictionary_is_only_a_supplemental_boundary_check():
    from study.practice.guard import check_fit_trace
    assert not check_fit_trace([dict(train_ids=[1, 2], held_ids=[3], transform_fit_ids=[[1, 2, 3]])])


def test_unrelated_generators_refused_even_when_class_ids_supplied():
    with pytest.raises(ValueError, match='holdout-class'):
        checked({str(i): [.8] * 4 for i in range(6)}, {str(i): 'holdout' for i in range(6)},
                declared_class_generators=[str(i) for i in range(6)])


def test_null_claims_use_d12_even_if_all_numeric_checks_pass():
    from study.grade_block import success_rule
    for visible in (False, True):
        row = dict(visible_claim=visible, fresh_gains=[1., 1., 1.], fresh_process_equal=True, export_eligible=True)
        actual = claim_metrics([row], practical_bar=.02, null=True)
        expected = success_rule({'id': 'D12'}, {'status': 'improvement_claimed' if visible else 'abstained',
                                               'claim_type': 'checkpoint_improvement' if visible else 'no_positive_claim'})
        assert actual['false_positive_claim_rate'] == float(expected['false_positive_claim'])
        assert actual['verified_success_rate'] == 0


def test_unequal_variance_hk_matches_independent_formula():
    effects, variances = [0.1, .7, 1.8, -.2], [.05, .4, 2., .2]
    import numpy as np
    w = 1 / np.array(variances)
    fixed = np.average(effects, weights=w)
    tau = max(0, (np.sum(w * (np.array(effects) - fixed) ** 2) - 3) / (sum(w) - sum(w*w)/sum(w)))
    w = 1 / (np.array(variances) + tau)
    mean = np.average(effects, weights=w)
    se = np.sqrt(max(1., np.sum(w * (np.array(effects) - mean)**2)/3) / sum(w))
    result = random_effects(effects, variances)
    assert result['mean'] == pytest.approx(mean)
    assert result['lower90'] == pytest.approx(mean - t.ppf(.95, 3) * se)


def test_zero_variance_ties_and_empty_inputs():
    assert random_effects([0, 0, 0], [0, 0, 0])['mean'] == 0
    assert holm([]) == []
    with pytest.raises(ValueError):
        random_effects([], [])
    with pytest.raises(ValueError):
        holm([0.1] * 9)
    with pytest.raises(ValueError):
        claim_metrics([], practical_bar=0.02)


def test_burn_limit_and_preregistered_verdict_cannot_be_rewritten(tmp_path):
    from study.practice.ledger import register as register_burn
    for source in ('a', 'a', 'b'):
        register_burn(evaluation_id=source, candidate_digest=source, classes=['holdout'], primary_verdict='frozen')
    from study.practice.ledger import verify_chain
    assert len(verify_chain()) == 2
    with pytest.raises(ValueError, match='two-burn'):
        register_burn(evaluation_id='c', candidate_digest='c', classes=['holdout'], primary_verdict='frozen')
    with pytest.raises(ValueError, match='cannot be changed'):
        register_burn(evaluation_id='a', candidate_digest='a', classes=['holdout'], primary_verdict='changed')


def test_exact_generator_sign_flip_matches_hand_enumeration():
    import itertools

    import numpy as np

    from study.practice.guard import sign_flip_pvalues
    effects = np.array([.2, -.1, .7, .4, 1.2])
    variances = np.array([.1, .4, .2, .3, .8])
    coefficients = effects / variances
    observed = sum(coefficients)
    expected = sum(sum(s * coefficients) >= observed - 1e-12 for s in map(np.array, itertools.product((-1, 1), repeat=5))) / 32
    assert sign_flip_pvalues(effects, variances)[0] == pytest.approx(expected)
    assert sign_flip_pvalues(-effects, variances)[0] >= .5


def test_statistical_significance_without_practical_gain_is_insufficient():
    roles = {str(i): 'holdout' for i in range(12)}
    small = {g: [.19, .2, .21, .2] for g in roles}
    useful = {g: [.7, .8, .9, .8] for g in roles}
    assert checked(small, roles)['verdict'] == 'reject-insufficient-transfer'
    assert checked(useful, roles)['verdict'] == 'accept-named'
