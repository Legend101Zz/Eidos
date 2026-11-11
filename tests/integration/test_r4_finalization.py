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
    assert set(InvestigatorActivities(None, None)._read_tools('p', conclude_only=True)) == {
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
    assert 'finalization' not in turn['spec']
    assert turn['spec']['finalization_phase']['mode'] == 'ordinary'
    assert turn['spec']['exploration']['incumbent']['job_id'] == job
    assert turn['spec']['limits']['max_calls'] > 0
    assert turn['spec']['completion']['remaining_seconds'] == 400
    assert turn['spec']['limits']['deadline_seconds'] == 220
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


def test_finalization_reports_the_exact_lock_when_its_job_predates_the_investigation(scene):
    job = scene.job(None, metrics={'accuracy': 0.91})
    inv = scene.investigation(finalization='autonomous')
    scene.rule(inv)
    scene.lock(inv, job)
    _entered(scene, inv)
    turn = scene.begin(inv)
    final = turn['spec']['finalization']
    assert final['locked_candidate']['id'] == job and final['locked_candidate']['status'] == 'succeeded'
    assert any(m['job_id'] == job for m in final['metrics'])
    control = {'action': 'conclude', 'summary': f'Existing locked recipe {job}; no held-out confirmation.',
               'claim': {'disposition': 'candidate'}}
    assert scene.propose(turn['turn_id'], 0, {'rationale': 'report existing lock', 'calls': [], 'control': control})['accepted']
    _complete(scene.lab, scene.pid, turn['turn_id'], 'concluded', control)
    claim = scene.q('get_investigation', {'investigation_id': inv})['body']['conclusion']['claim_state']
    assert claim['confirmed'] is False


@pytest.mark.parametrize('policy', ['autonomous', 'operator'])
def test_real_boundary_preserves_ordinary_finalization_across_turns(scene, monkeypatch, policy):
    inv = scene.investigation(finalization=policy, wall_seconds=1800)
    scene.rule(inv)
    job = scene.job(inv, metrics={'accuracy': 0.91}, config={'seed': 11})
    scene.lock(inv, job)
    wall = scene.q('get_investigation', {'investigation_id': inv})['body']['wall']
    monkeypatch.setattr('eidos.application.handlers.investigations.time.time', lambda: wall['deadline_unix'] - 400)
    admission = scene.cmd('enter_finalization', {'investigation_id': inv, 'trigger': 'exploration-boundary'})
    assert admission['enabled'] and admission['mode'] == 'ordinary'
    turn = scene.begin(inv)
    assert 'finalization' not in turn['spec']
    assert turn['spec']['finalization_phase']['mode'] == 'ordinary'
    assert turn['spec']['limits']['max_calls'] > 0
    assert 'exploration_deadline_unix' not in turn['spec']['completion']
    accepted = scene.propose(turn['turn_id'], 0, {'rationale': 'reproduce incumbent', 'purpose': 'reproduction',
        'calls': [{'tool': 'run_component', 'args': {'component_ref': scene.trial,
            'config': {'split': 'validation', 'seed': 23}}}]})
    assert accepted['accepted'], accepted
    fresh = scene.job(inv, purpose='reproduction', config={'seed': 23})
    principal = scene.lab.principal(f'inv-{scene.pid}', ['investigator'], [scene.pid])
    acts = InvestigatorActivities(ShimClient(scene.lab, principal), ShimClient(scene.lab, scene.wf))
    tools = acts._read_tools(scene.pid, finalization={'turn_id': turn['turn_id']})
    assert tools['get_job'](fresh)['id'] == fresh
    assert tools['inspect_evidence'](scene.output(fresh))['available']
    shown = scene.q('get_investigation', {'investigation_id': inv})
    assert shown['finalization'] is None and shown['finalization_phase']['mode'] == 'ordinary'
    scene.finish_turn(turn['turn_id'])
    next_turn = scene.begin(inv)
    assert next_turn['spec']['finalization_phase'] == turn['spec']['finalization_phase']
    assert 'finalization' not in next_turn['spec']


@pytest.mark.parametrize('failure_kind', ['child-failure', 'child-timeout', 'coordinator-restarted'])
def test_abandoned_finalization_is_closed_durably_before_retry_or_restart(scene, failure_kind):
    inv, job = _locked(scene)
    _entered(scene, inv)
    turn = scene.begin(inv)
    failure = {'error_type': failure_kind, 'message': 'child did not close', 'retryable': True}
    recovered = scene.cmd('reconcile_finalization', {'investigation_id': inv, 'failure': failure})
    assert recovered['finalization'] is True
    shown = scene.q('get_investigation', {'investigation_id': inv})
    receipt = shown['finalization_export_receipt']
    assert receipt['incumbent']['job_id'] == job and receipt['investigator_concluded'] is False
    assert receipt['failure']['error_type'] == failure_kind
    assert shown['needs_input']['code'] == 'finalization-failed'
    assert scene.q('get_turn', {'turn_id': turn['turn_id']})['status'] == 'failed'
    # A new coordinator/worker sees the same terminal recovery, without replaying a model turn.
    again = scene.cmd('reconcile_finalization', {'investigation_id': inv})
    assert again == recovered
    assert scene.cmd('begin_turn', {'investigation_id': inv})['reason'] == 'finalization-exhausted'
    assert scene.q('get_investigation', {'investigation_id': inv})['finalization_export_receipt'] == receipt


@pytest.mark.parametrize('recovery', [True, False])
def test_finalization_denies_direct_and_transitively_derived_held_out_reads_and_context(lab, recovery):
    import hashlib

    from eidos.client import LabError

    secret = b'{"secret-held-out": [1, 0, 1]}'
    sc = Scene(lab, held_out={'labels': [hashlib.sha256(secret).hexdigest()]})
    digest, size = lab.store.put_bytes(secret)
    held = sc.cmd('register_artifact', {'digest': digest, 'size': size, 'media_type': 'application/json',
                                      'schema': 'class-predictions/1', 'role': 'input'})['artifact_id']
    inv, safe = _locked(sc)
    direct = sc.job(inv, ref=sc.infer, inputs={'predictions': held}, metrics={'secret-direct': 1})
    derived = sc.job(inv, ref=sc.infer, inputs={'predictions': sc.output(direct)}, metrics={'secret-derived': 1})
    # The provenance chain can cross the investigation boundary.
    outside = sc.job(None, ref=sc.infer, inputs={'predictions': sc.output(derived)})
    if recovery:
        _entered(sc, inv)
    else:
        sc.cmd('enter_finalization', {'investigation_id': inv, 'trigger': 'exploration-boundary'})
    turn = sc.begin(inv)
    principal = lab.principal(f'inv-{sc.pid}', ['investigator'], [sc.pid])
    acts = InvestigatorActivities(ShimClient(lab, principal), ShimClient(lab, sc.wf))
    tools = acts._read_tools(sc.pid, finalization={'turn_id': turn['turn_id']}, conclude_only=recovery)
    assert tools['get_job'](safe)['id'] == safe
    assert tools['inspect_evidence'](sc.output(safe))['available']
    for aid in [held, sc.output(direct), sc.output(derived), sc.output(outside)]:
        with pytest.raises(LabError):
            tools['inspect_evidence'](aid)
    for jid in [direct, derived, outside]:
        with pytest.raises(LabError):
            tools['get_job'](jid)
    # A guessed id outside the durable evidence allowlist is also denied.
    unrelated = sc.job(None)
    with pytest.raises(LabError):
        tools['get_job'](unrelated)
    base = {'project_id': sc.pid, 'investigation_id': inv, 'turn_id': turn['turn_id']}
    _env(1).run(acts.build_context, base)
    context = sc.q('get_turn', {'turn_id': turn['turn_id']})['context']
    art = sc.q('get_artifact', {'artifact_id': context['context_artifact_id']})
    packed = lab.store.path_for(art['digest']).read_text()
    for forbidden in [direct, derived, 'secret-direct', 'secret-derived', held, sc.output(direct), sc.output(derived)]:
        assert forbidden not in packed
    assert safe in packed


def test_ordinary_finalization_can_read_a_lock_that_predates_the_investigation(scene):
    job = scene.job(None, metrics={'accuracy': .9})
    inv = scene.investigation()
    scene.rule(inv)
    scene.lock(inv, job)
    scene.cmd('enter_finalization', {'investigation_id': inv, 'trigger': 'exploration-boundary'})
    turn = scene.begin(inv)
    principal = scene.lab.principal(f'inv-{scene.pid}', ['investigator'], [scene.pid])
    acts = InvestigatorActivities(ShimClient(scene.lab, principal), ShimClient(scene.lab, scene.wf))
    tools = acts._read_tools(scene.pid, finalization={'turn_id': turn['turn_id']})
    assert tools['get_job'](job)['id'] == job
    assert tools['inspect_evidence'](scene.output(job))['available']
