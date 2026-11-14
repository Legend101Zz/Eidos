"""Preparation is pure expansion, never an investigator lock."""
import copy

import pytest

from eidos.domain.errors import Invalid
from eidos.domain.promotion import PORTFOLIO_REF, expand, suggested_lock


def view():
    return dict(investigation=dict(id='i', body=dict(inputs=dict(train='t', validation='v'), environment='default')),
                policy=dict(policy='exploration-policy/3', project_command_ref='eidos.project.command@1.0.9'),
                releases=['eidos.project.command@1.0.9', PORTFOLIO_REF],
                baseline_predictions='b', source=dict(basis='explicit-export'), jobs=[], notes=[], exploration={})


def proposal(source='source'):
    return dict(tool='prepare_candidate', args=dict(source=source, seeds=[11, 23]))


def test_four_checked_calls_keep_indices_stamp_and_never_lock():
    v = view()
    calls = [dict(tool='run_component', args=dict(component_ref=PORTFOLIO_REF, config={})), proposal({'$ref': 'call[0].outputs.promotion_source'})]
    result = expand(calls, v, dict(max_calls=5), proposed_by='dec')
    assert len(result) == 5
    assert [c['args']['purpose'] for c in result[1:]] == ['open-search', 'reproduction', 'measurement-development', 'reproduction']
    assert all(c['tool'] == 'run_component' for c in result)
    assert all(c['macro'] == dict(tool='prepare_candidate', proposed_by='dec', basis='one-se-choice') for c in result[1:])
    assert result[2]['args']['inputs']['model'] == {'$ref': 'call[1].outputs.model'}
    assert calls[-1]['tool'] == 'prepare_candidate'


@pytest.mark.parametrize('change,match', [
    ('count', 'allows'), ('last', 'last'), ('release', 'release'), ('baseline', 'baseline'), ('seeds', 'seeds'),
])
def test_named_refusals(change, match):
    v, calls, limits = view(), [proposal()], dict(max_calls=4)
    if change == 'count':
        limits['max_calls'] = 3
    if change == 'last':
        calls += [dict(tool='record_note', args=dict(text='x'))]
    if change == 'release':
        v['releases'] = []
    if change == 'baseline':
        v['baseline_predictions'] = None
    if change == 'seeds':
        calls[0]['args']['seeds'] = [11, 11]
    with pytest.raises(Invalid, match='prepare_candidate.*' + match):
        expand(calls, v, limits, proposed_by='dec')


def test_margin_precheck_before_compute_and_existing_source_required():
    v = view()
    v['exploration'] = dict(incumbent=dict(evidence=dict(value=.8, se=.02)), rule=dict(
        direction='higher_is_better', margin=dict(metric='validation_accuracy_se', multiple=1.)))
    v['source_metrics'] = dict(validation_chosen=.81, se_chosen=.01, fit_seconds_chosen=.2,
                               checkpoint_bytes_chosen=100, export_eligible=1.)
    with pytest.raises(Invalid, match='prepare_candidate.*margin'):
        expand([proposal()], v, dict(max_calls=4), proposed_by='dec')
    with pytest.raises(Invalid, match='prepare_candidate.*existing'):
        expand([proposal({'$ref': 'call[0].outputs.promotion_source'})], v, dict(max_calls=4), proposed_by='dec')
    v['source_metrics']['validation_chosen'] = .9
    assert len(expand([proposal()], v, dict(max_calls=4), proposed_by='dec')) == 4


def test_suggested_lock_uses_shipped_validation_and_recognises_reproduction():
    v = view()
    stamp = dict(tool='prepare_candidate', proposed_by='dec', basis='one-se-choice')
    base = dict(component='eidos.project.command@1.0.9', status='succeeded', purpose='open-search',
                inputs=dict(source='s', train='t', validation='v'), config=dict(argv=['python', 'project.py', 'train'], seed=11),
                receipt=dict(metrics=dict(validation_accuracy=.8, validation_accuracy_se=.02, fit_seconds=.1, checkpoint_bytes=100)),
                outputs=[], macro=stamp)
    v['jobs'] = [dict(base, id='train'), dict(base, id='check', config=dict(argv=['python', 'project.py', 'check']),
                   inputs=dict(model='m'), receipt=dict(metrics=dict(readiness_ok=1, readiness_max_abs_diff=0))),
                 dict(base, id='compare', config=dict(argv=['python', 'project.py', 'compare']),
                      receipt=dict(metrics=dict(paired_delta=.1, paired_delta_lo=.05, paired_delta_hi=.15, paired_n=100))),
                 dict(copy.deepcopy(base), id='second', purpose='reproduction', config=dict(argv=['python', 'project.py', 'train'], seed=23))]
    fact = suggested_lock(v)
    assert fact['lock']['job_id'] == 'train'
    assert fact['lock']['selection']['reproduce_within'] == .04
    assert fact['reproduction']['reproduced_by'] == 'second'
    assert fact['readiness']['readiness_ok'] == 1
    assert fact['paired']['paired_delta_lo'] == .05
    for job in v['jobs'][1:]:
        job['status'] = 'running'
        assert suggested_lock(v) is None
        job['status'] = 'succeeded'


def test_margin_arithmetic_both_directions_and_legacy_property():
    from eidos.domain.exploration import better, clears_margin, parse_rule
    raw = dict(metric='score', split='validation', direction='higher_is_better', scorer='scorer', data=dict(inputs=dict(validation='v')))
    for direction in ('higher_is_better', 'lower_is_better'):
        raw['direction'] = direction
        legacy = parse_rule(raw)
        rule = parse_rule(dict(raw, margin=dict(metric='se', multiple=1)), policy_id='exploration-policy/3')
        for a in (-1., 0., .1, .11, 1.):
            for b in (-1., 0., .1, .11, 1.):
                assert clears_margin(dict(value=a), dict(value=b), legacy) == better(a, b, direction)
                signed = (a - b) * (1 if direction == 'higher_is_better' else -1)
                assert clears_margin(dict(value=a, se=.1), dict(value=b, se=.02), rule) == (signed > .1)
    for invalid in (dict(metric='se', multiple=-1), dict(metric='se', multiple=float('nan')), dict(metric='', multiple=1)):
        with pytest.raises(ValueError):
            parse_rule(dict(raw, margin=invalid), policy_id='exploration-policy/3')


def test_ledger_shaped_refine_only_and_macro_support_rounds():
    from eidos.domain.exploration import rounds
    rule = dict(metric='validation_accuracy', split='validation', direction='higher_is_better',
                scorer='eidos.project.command@1.0.9', data=dict(inputs=dict(train='t', validation='v')))
    def job(jid, component, inputs, outputs, metrics, purpose='open-search'):
        return dict(id=jid, component=component, purpose=purpose, status='succeeded', config={}, inputs=inputs,
                    outputs=[dict(slot=k, artifact_id=v) for k, v in outputs.items()], receipt=dict(metrics=metrics))
    jobs = [job('fit', rule['scorer'], dict(train='t', validation='v'), dict(model='m', validation_predictions='p'), dict(validation_accuracy=.8)),
            job('check', rule['scorer'], dict(model='m'), {}, dict(readiness_max_abs_diff=0), 'reproduction'),
            job('compare', rule['scorer'], dict(extra_a='p'), {}, dict(paired_delta=.1), 'measurement-development'),
            job('refine', PORTFOLIO_REF, dict(train='t', validation='v'), dict(portfolio_table='table'), dict(round_evaluated=1)),
            job('diag', 'eidos.diagnose.baseline-errors@1.1.0', dict(train='t', validation='v'), {}, {}),
            job('rescore', PORTFOLIO_REF, dict(train='t', validation='v'), {}, dict(round_evaluated=1), 'reproduction')]
    log = [dict(turn=2, jobs=['fit', 'check', 'compare', 'diag']), dict(turn=3, jobs=['refine']), dict(turn=4, jobs=['rescore'])]
    assert rounds(log, jobs, 1, 1, rule=rule, v3=True) == ([2, 3, 4], 3, [])
    assert rounds(log, jobs, 1, 1, rule=rule)[2] == ['compare', 'diag', 'refine']


def test_same_decision_measured_guards_run_before_admission():
    from eidos.investigator.activities import InvestigatorActivities
    class Client:
        def query(self, name, params, **kwargs):
            if name == 'get_artifact':
                return dict(producer_job='portfolio')
            if name == 'get_job':
                return dict(terminal=dict(receipt=dict(metrics=dict(fit_seconds_chosen=61))))
            return dict(spec=dict(exploration=dict(allowance=dict(holdback_seconds=270))))
        def command(self, *args, **kwargs):
            pytest.fail('guard must refuse before a command is sent')
    acts = InvestigatorActivities(Client(), Client())
    result = acts._run_effect('p', 'i', 'turn', 'dec', dict(tool='run_component', index=0, command_key='k', macro=dict(tool='prepare_candidate')),
        dict(purpose='open-search', inputs=dict(source='source')), None)
    assert result['code'] == 'PREPARATION_GUARD' and not result['ok']


@pytest.mark.parametrize('path', ['eidos_recipes/cv.py', 'EIDOS_RECIPES/CV.PY', 'eidos_recipes', 'eidos_recipes/cv.py/child'])
def test_library_inclusion_refuses_filesystem_collisions(path):
    from eidos.application.handlers.promotion import collisions
    assert collisions({'eidos_recipes/cv.py': 'library'}, {path: 'authored'})
    assert not collisions({'eidos_recipes/cv.py': 'library'}, {'project.py': 'authored'})


@pytest.mark.parametrize('stray', [.01, 'one-se', {'metric': 'se'}, True, [],
                                  {'metric': 'se', 'multiple': float('inf')},
                                  {'metric': 'se', 'multiple': 1, 'extra': 0}])
def test_legacy_policy_ignores_stray_margin_but_v3_refuses_it(stray):
    from eidos.domain.exploration import parse_rule
    raw = dict(metric='accuracy', scorer='scorer', data=dict(inputs=dict(validation='v')))
    assert parse_rule(dict(raw, margin=stray)) == parse_rule(raw)
    with pytest.raises(ValueError, match='margin'):
        parse_rule(dict(raw, margin=stray), policy_id='exploration-policy/3')


@pytest.mark.parametrize('failure', ['paired-empty', 'readiness-failed', 'reproduction-unscored'])
def test_suggested_lock_withholds_incomplete_preparations(failure):
    # Reuse a real-shaped train/check/compare/reproduction group with no synthetic readiness_ok.
    v = view()
    base = dict(component=v['policy']['project_command_ref'], status='succeeded', purpose='open-search',
                inputs=dict(source='s', train='t', validation='v'), config=dict(argv=['python', 'project.py', 'train'], seed=11),
                receipt=dict(metrics=dict(validation_accuracy=.8, validation_accuracy_se=.02)), outputs=[],
                macro=dict(tool='prepare_candidate', proposed_by='d', basis='one-se-choice'))
    v['jobs'] = [dict(base, id='train'),
                 dict(base, id='check', inputs=dict(model='m'), config=dict(argv=['python', 'project.py', 'check']),
                      receipt=dict(metrics=dict(readiness_max_abs_diff=0))),
                 dict(base, id='compare', config=dict(argv=['python', 'project.py', 'compare']),
                      receipt=dict(metrics=dict(paired_delta=.1, paired_delta_lo=.05, paired_delta_hi=.15, paired_n=100))),
                 dict(base, id='reproduce', purpose='reproduction', config=dict(argv=['python', 'project.py', 'train'], seed=23))]
    assert suggested_lock(v)['readiness']['readiness_ok'] == 1
    if failure == 'paired-empty':
        v['jobs'][2]['receipt']['metrics']['paired_n'] = 0
    elif failure == 'readiness-failed':
        v['jobs'][1]['status'] = 'failed'
    else:
        v['jobs'][3]['receipt'] = {}
    assert suggested_lock(v) is None


def test_a_valid_margin_is_active_only_for_policy_three_in_recorded_notes():
    import json

    from eidos.domain.exploration import clears_margin, rule_from_notes
    raw = dict(metric='accuracy', scorer='scorer', data=dict(inputs=dict(validation='v')),
               margin=dict(metric='se', multiple=1))
    notes = [dict(id='lock', kind='candidate_lock', text=json.dumps(dict(job_id='j', selection=raw)))]
    legacy = rule_from_notes(notes)[0]
    opted_in = rule_from_notes(notes, policy_id='exploration-policy/3')[0]
    assert 'margin' not in legacy and opted_in['margin'] == raw['margin']
    assert clears_margin(dict(value=.81), dict(value=.8), legacy)
    assert not clears_margin(dict(value=.81, se=.02), dict(value=.8, se=.02), opted_in)
