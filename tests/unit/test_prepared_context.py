"""Opt-in context, steering next and exact old prompt preservation."""
from eidos.investigator.context import POLICY_ID, POLICY_V4, pack
from eidos.investigator.playbook import PLAYBOOK, PLAYBOOK_V3
from eidos.investigator.pydantic_ai_runtime import PROMPT_REVISION, SYSTEM


def test_new_guide_is_opt_in_and_next_survives_old_job_window():
    view = dict(horizon=1, investigation=dict(id='i', status='active', body=dict(question='q', investigator={})),
                jobs=[dict(id='diagnosis', component='eidos.diagnose.baseline-errors@1.1.0', status='succeeded',
                           purpose='open-search', outputs=[], receipt=dict(next='inspect structure in the portfolio table', hypotheses=[]))])
    view['jobs'] += [dict(id=f'job_{i}', component='other', status='succeeded', purpose='open-search', outputs=[]) for i in range(60)]
    old = pack(view, [], policy=POLICY_ID)
    new = pack(view, [], policy=POLICY_V4)
    assert 'inspect structure in the portfolio table' in old['text']
    assert 'inspect structure in the portfolio table' in new['text']
    assert 'prepare_candidate {' not in old['text']
    assert 'prepare_candidate {' in new['text']
    assert new['policy'] == POLICY_V4 and new['chars'] <= 60000
    view['jobs'] += [dict(id=f'portfolio_{i}', component='eidos.search.recipe-portfolio@1.0.0', status='succeeded',
                         purpose='open-search', outputs=[], receipt=dict(metrics=dict(round_evaluated=1))) for i in range(10)]
    assert 'inspect structure in the portfolio table' in pack(view, [], policy=POLICY_V4)['text']
    assert PLAYBOOK in SYSTEM and PLAYBOOK_V3 not in SYSTEM and PROMPT_REVISION == 'investigator-prompt/5'


def test_runtime_selects_revision_set_without_any_model_call():
    import pytest

    from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
    from eidos.investigator.runtime import DecisionRequest, ModelCallFailed
    runtime = PydanticAIRuntime(dict(output_mode='invalid-test-mode'))
    request = DecisionRequest(turn_id='t', slot=0, context='', turn_so_far=[], limits={},
        view=dict(investigation=dict(body=dict(investigator=dict(exploration=dict(policy='exploration-policy/3'))))))
    with pytest.raises(ModelCallFailed, match='unknown output_mode'):
        runtime.decide(request)
    assert runtime.prompt_revision == 'investigator-prompt/6' and PLAYBOOK_V3 in runtime.system
    request.view = {}
    with pytest.raises(ModelCallFailed, match='unknown output_mode'):
        runtime.decide(request)
    assert runtime.prompt_revision == PROMPT_REVISION and runtime.system == SYSTEM


def test_candidate_lock_offer_is_suppressed_after_lock_or_finalization(monkeypatch):
    import copy

    from eidos.domain import promotion
    monkeypatch.setattr(promotion, 'suggested_lock', lambda view: dict(lock=dict(job_id='prepared', selection={})))
    view = dict(horizon=1, investigation=dict(id='i', status='active', body=dict(question='q')),
                exploration=dict(phase='explore', incumbent=None))
    assert 'investigator decides the separate lock' in pack(view, [], policy=POLICY_V4)['text']
    for stopped in ('locked', 'ordinary', 'autonomous'):
        current = copy.deepcopy(view)
        if stopped == 'locked':
            current['exploration']['incumbent'] = dict(job_id='prepared')
        elif stopped == 'ordinary':
            current['exploration']['phase'] = 'finalize'
        else:
            current['finalization'] = dict(enabled=True)
        assert 'investigator decides the separate lock' not in pack(current, [], policy=POLICY_V4)['text']
