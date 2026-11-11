"""G4: prospective rule preflight and a single read-only recovery, real ledger + fake provider."""
from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from test_cancel_through_acceptance import _env
from test_command_path import _publish
from test_f5_turn_authorization import RUN_A
from test_f5c_ticket_and_handoff import _begin_decision
from test_f5d_conclude_race import _complete
from test_incumbent_challengers import CODE, RULE, SCORER, Scene
from test_turn_recovery import ShimClient

from eidos.domain.errors import Invalid
from eidos.domain.turns import turn_boundary
from eidos.investigator.activities import InvestigatorActivities

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]


@pytest.fixture
def scene(lab):
    return Scene(lab)


def _entered(scene, inv, **extra):
    return scene.cmd('enter_finalization', {'investigation_id': inv, 'trigger': 'refused-proposals', **extra})


def _locked(scene, **settings):
    inv = scene.investigation(finalization='autonomous', **settings)
    scene.rule(inv)
    job = scene.job(inv, metrics={'accuracy': 0.91}, config={'seed': 11})
    scene.lock(inv, job)
    return inv, job


def test_refusals_finalize_the_locked_incumbent_with_provenance_and_separate_usage(scene):
    inv, job = _locked(scene, wall_seconds=1800)
    refusals = []
    for _ in range(3):
        turn = scene.begin(inv)
        for slot in range(2):
            execution = f'{RUN_A}/decide-slot-{slot}'
            epoch = _begin_decision(scene.lab, scene.pid, turn['turn_id'], execution, slot=slot)['epoch']
            result = scene.cmd('accept_decision', {'turn_id': turn['turn_id'], 'slot': slot,
                'proposal': {'rationale': 'invalid wait', 'calls': [], 'control': {'action': 'wait_for_jobs'}},
                'execution': execution, 'epoch': epoch, 'attempt': 1,
                'response_artifact_id': 'art_actual_reply', 'usage_recorded': True})
            assert not result['accepted'] and result['reason'] == 'invalid'
            persisted = scene.q('get_turn', {'turn_id': turn['turn_id']})['decisions'][-1]
            assert persisted['body']['response_artifact_id'] == 'art_actual_reply'
            refusals.append(result['error'])
        _complete(scene.lab, scene.pid, turn['turn_id'], 'invalid-proposals', {})
    entered = _entered(scene, inv, runtime_outcome='deadline-exhausted')
    assert len(entered['refusals']) == 6
    assert [r['error'] for r in entered['refusals']] == refusals
    assert _entered(scene, inv) == entered  # durable one-shot admission
    final = scene.begin(inv)
    spec = final['spec']
    assert spec['limits']['max_calls'] == 0 and spec['limits']['max_model_calls'] == 1
    assert turn_boundary(spec['completion']) == spec['completion']['deadline_unix']
    assert spec['finalization']['locked_candidate']['id'] == job
    assert spec['finalization']['incumbent']['reproduced_by'] is None
    assert spec['finalization']['runtime_outcome'] == 'deadline-exhausted'
    assert any(m['job_id'] == job and m['receipt']['metrics']['accuracy'] == 0.91
               for m in spec['finalization']['metrics'])
    seen = []

    def provider(messages, info):
        seen.append(messages)
        if len(seen) == 1:
            content = {'rationale': 'verify incumbent receipt', 'read': [{'tool': 'get_job', 'args': {'job_id': job}}]}
        else:
            content = {'rationale': 'report fixed incumbent', 'calls': [], 'control': {'action': 'conclude',
                'summary': f'Locked incumbent {job}: validation accuracy 0.91; unreproduced, no held-out confirmation.',
                'claim': {'disposition': 'candidate', 'point_estimate': 0.91}}}
        return ModelResponse(parts=[TextPart(json.dumps(content))], usage=RequestUsage(input_tokens=200, output_tokens=30))

    investigator = scene.lab.principal(f'investigator-{inv}', ['investigator'], [scene.pid])
    acts = InvestigatorActivities(ShimClient(scene.lab, investigator), ShimClient(scene.lab, scene.wf),
        runtime_overrides={'runtime': 'pydantic-ai', 'output_mode': 'json', 'model': 'openai-compatible:fake',
                          '_model_object': FunctionModel(provider), 'request_timeout': 300})
    base = {'project_id': scene.pid, 'investigation_id': inv, 'turn_id': final['turn_id']}
    env = _env(1)
    env.run(acts.build_context, base)
    decision = env.run(acts.decide, {**base, 'slot': 0})
    assert decision['accepted'] and decision['n_calls'] == 0
    env.run(acts.complete_turn, {**base, 'stop_reason': 'concluded', 'control': decision['control']})
    shown = scene.q('get_investigation', {'investigation_id': inv})
    assert shown['status'] == 'concluded' and shown['needs_input'] is None
    assert job in shown['body']['conclusion']['summary']
    assert shown['body']['conclusion']['incumbent']['job_id'] == job
    assert shown['body']['conclusion']['claim_state']['confirmed'] is False
    assert shown['finalization_export_receipt']['investigator_concluded'] is True
    usage = scene.events(inv, 'model.usage')[-1]['body']['usage']
    assert usage['phase'] == 'finalization' and usage['calls'] == 2
    assert usage['input_tokens'] == 400 and usage['output_tokens'] == 60
    assert scene.cmd('begin_turn', {'investigation_id': inv})['started'] is False


@pytest.mark.parametrize('call', [
    {'tool': 'run_component', 'args': {'component_ref': 'proj.trial@1.0.0'}},
    {'tool': 'author_source', 'args': {'files': {'project.py': 'x=1'}}},
    {'tool': 'record_note', 'args': {'kind': 'candidate_lock', 'text': '{"job_id":"other"}'}},
    {'tool': 'record_note', 'args': {'kind': 'selection_rule', 'text': '{}'}},
])
def test_finalization_cannot_run_jobs_author_relock_or_change_rules(scene, call):
    inv, job = _locked(scene)
    _entered(scene, inv)
    turn = scene.begin(inv)
    before = scene.q('list_jobs', {'investigation_id': inv})
    result = scene.propose(turn['turn_id'], 0, {'rationale': 'forbidden effect', 'calls': [call],
                                              'control': {'action': 'conclude', 'summary': 'done'}})
    assert result['accepted'] is False and result['reason'] == 'invalid'
    assert scene.q('list_jobs', {'investigation_id': inv}) == before
    assert scene.locked_job(inv) == job and len(scene.notes(inv, 'selection_rule')) == 1
    with pytest.raises(Invalid, match='finalization fixes'):
        scene.lock(inv, job)
    assert set(InvestigatorActivities(None, None)._read_tools('p', finalization=True)) == {
        'get_job', 'inspect_evidence', 'describe_component'}


def test_failed_finalization_has_structured_failure_and_export_receipt_and_never_repeats(scene):
    inv, job = _locked(scene)
    _entered(scene, inv)
    turn = scene.begin(inv)
    _complete(scene.lab, scene.pid, turn['turn_id'], 'runtime-bounded', {})
    shown = scene.q('get_investigation', {'investigation_id': inv})
    assert shown['status'] == 'active' and shown['needs_input']['code'] == 'finalization-failed'
    receipt = shown['finalization_export_receipt']
    assert receipt['investigator_concluded'] is False and receipt['incumbent']['job_id'] == job
    assert receipt['failure']['reason'] == 'runtime-bounded'
    assert scene.cmd('begin_turn', {'investigation_id': inv})['reason'] == 'finalization-exhausted'


def test_rule_wrong_scorer_port_is_refused_before_recording_and_correct_rule_stays_immutable(scene):
    scorer = _publish(scene.lab, scene.pid, {**SCORER, 'id': 'test.classification',
        'input': {'predictions': 'class-predictions/1', 'reference': 'class-labels/1'}}, CODE)['component_ref']
    inv = scene.investigation()
    for inline in (False, True):
        bad = {**RULE, 'scorer': scorer, 'data': {'inputs': {'validation': 'art_labels'}}}
        with pytest.raises(Invalid, match='reference') as caught:
            if inline:
                job = scene.job(inv, metrics={'accuracy': 0.9})
                scene.lock(inv, job, selection=bad)
            else:
                scene.note(inv, 'selection_rule', json.dumps(bad))
        assert caught.value.details['invalid_ports'] == ['validation']
        assert caught.value.details['input_ports'] == ['predictions', 'reference']
        assert scene.notes(inv, 'selection_rule') == [] and scene.notes(inv, 'candidate_lock') == []
    scene.note(inv, 'selection_rule', json.dumps({**bad, 'data': {'inputs': {'reference': 'art_labels'}}}))
    with pytest.raises(Invalid, match='fixed once recorded'):
        scene.note(inv, 'selection_rule', json.dumps(bad))
    assert len(scene.notes(inv, 'selection_rule')) == 1


def test_exploration_boundary_begins_one_finalization_with_holdback_available(scene, monkeypatch):
    inv, job = _locked(scene, wall_seconds=1800)
    wall = scene.q('get_investigation', {'investigation_id': inv})['body']['wall']
    monkeypatch.setattr('eidos.application.handlers.investigations.time.time', lambda: wall['deadline_unix'] - 400)
    turn = scene.begin(inv)
    assert turn['spec']['finalization']['incumbent']['job_id'] == job
    assert turn['spec']['finalization']['trigger'] == 'wall-holdback'
    assert turn['spec']['completion']['remaining_seconds'] == 400
    assert turn['spec']['limits']['deadline_seconds'] == 400
    assert 'exploration_deadline_unix' not in turn['spec']['completion']


def test_operator_policy_does_not_admit_autonomous_finalization(scene):
    inv = scene.investigation(finalization='operator')
    assert _entered(scene, inv)['enabled'] is False
    assert 'finalization' not in scene.begin(inv)['spec']


def test_no_locked_candidate_can_only_abstain_and_missing_evidence_never_passes(scene):
    inv = scene.investigation(finalization='autonomous')
    _entered(scene, inv)
    turn = scene.begin(inv)
    invented = scene.propose(turn['turn_id'], 0, {'rationale': 'no lock', 'calls': [], 'control': {
        'action': 'conclude', 'summary': 'pretend a candidate exists', 'claim': {'disposition': 'candidate'}}})
    assert invented['accepted'] is False and 'no eligible locked candidate' in invented['error']['message']
    # A new attempt of the same slot may give an honest abstention (one model decision activity can retry).
    control = {'action': 'conclude', 'summary': 'No eligible locked candidate; abstaining.',
               'claim': {'disposition': 'abstain'}}
    execution = f'{RUN_A}/decide-slot-0'
    epoch = _begin_decision(scene.lab, scene.pid, turn['turn_id'], execution, slot=0, attempt=2)['epoch']
    accepted = scene.cmd('accept_decision', {'turn_id': turn['turn_id'], 'slot': 0,
        'proposal': {'rationale': 'honest abstention', 'calls': [], 'control': control},
        'usage_recorded': True, 'execution': execution, 'epoch': epoch, 'attempt': 2})
    assert accepted['accepted'] is True
    _complete(scene.lab, scene.pid, turn['turn_id'], 'concluded', control)
    conclusion = scene.q('get_investigation', {'investigation_id': inv})['body']['conclusion']
    assert conclusion['incumbent'] is None and conclusion['claim_state']['confirmed'] is False


def test_unadmitted_finalization_at_original_wall_deadline_has_failure_export(scene, monkeypatch):
    inv, job = _locked(scene, wall_seconds=1800)
    _entered(scene, inv)
    wall = scene.q('get_investigation', {'investigation_id': inv})['body']['wall']
    monkeypatch.setattr('eidos.application.handlers.investigations.time.time', lambda: wall['deadline_unix'] + 1)
    assert scene.cmd('begin_turn', {'investigation_id': inv})['started'] is False
    shown = scene.q('get_investigation', {'investigation_id': inv})
    assert shown['needs_input']['code'] == 'finalization-failed'
    assert shown['finalization_export_receipt']['incumbent']['job_id'] == job
    assert shown['finalization_export_receipt']['investigator_concluded'] is False


def test_default_policy_is_autonomous_and_recorded_at_start(scene):
    inv = scene.cmd('start_investigation', {'question': 'report honestly',
        'charter_id': f'charter:s{scene.pid}',
        'investigator': {'wall_seconds': 30, 'conclude_threshold_seconds': 120}})['investigation_id']
    shown = scene.q('get_investigation', {'investigation_id': inv})
    assert shown['body']['investigator']['finalization'] == 'autonomous'
    turn = scene.begin(inv)
    assert turn['spec']['finalization']['enabled'] is True
    assert turn['spec']['limits']['max_calls'] == 0
