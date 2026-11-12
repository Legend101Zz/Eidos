"""Independent arithmetic and adversarial controls for the practice gate."""
import math

import pytest

from study.practice.guard import (
    claim_metrics,
    evaluate,
    guard_selftest,
    holm,
    normalized_gain,
    random_effects,
    sign_test,
)


def test_generator_level_arithmetic():
    assert sign_test([1, 2, 3, 4]) == pytest.approx(1 / 16)
    assert sign_test([0, 0]) == 1
    result = random_effects([1, 2, 3], [0.25, 0.25, 0.25])
    assert result['mean'] == pytest.approx(2)
    assert result['tau_squared'] == pytest.approx(0.75)
    assert result['lower90'] == pytest.approx(2 - 1.6448536269514722 / math.sqrt(3))
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert normalized_gain(0.8, 0.8, 0.8, 0.02) == 0
    assert normalized_gain(0.82, 0.8, 0.8, 0.02) == pytest.approx(0.5)


def test_all_guard_controls_reject():
    assert guard_selftest() == {
        'card_specific_patch': 'design-only',
        'harmful_operator': 'reject-no-harm',
        'screen_before_cv': 'reject-in-fold',
    }


def test_one_generator_cannot_make_a_class_claim():
    result = evaluate({'g': [0.05] * 4}, {'g': 'holdout'}, epsilon=0.01)
    assert result['verdict'] == 'no regression on these named generators'
    assert result['class_claim'] is False


def test_named_generator_harm_cannot_be_hidden_by_pooled_instances():
    result = evaluate({'a': [1] * 100, 'b': [-0.5] * 4}, {'a': 'holdout', 'b': 'holdout'})
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
    result = evaluate(effects, {key: 'holdout' for key in effects})
    assert result['class_claim'] is False


def test_real_fold_boundary_catches_screen_fitted_before_cv():
    from study.practice.guard import check_in_fold
    assert not check_in_fold([dict(train_ids=[1, 2], held_ids=[3], transform_fit_ids=[[1, 2, 3]])])


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
    from study.practice.guard import register_burn
    path = tmp_path / 'burns.json'
    for source in ('a', 'a', 'b'):
        result = register_burn(path, source_key=source, classes=['holdout'], primary_verdict='frozen')
    assert len(result['burns']) == 2
    with pytest.raises(ValueError, match='two-burn'):
        register_burn(path, source_key='c', classes=['holdout'], primary_verdict='frozen')
    with pytest.raises(ValueError, match='cannot be changed'):
        register_burn(path, source_key='a', classes=['holdout'], primary_verdict='changed')
