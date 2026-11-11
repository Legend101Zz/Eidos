"""D3 N01-r2: six valid read replies at 249/233/221 seconds must admit their follow-ups.

Fixture shape follows D3's terminal usage receipts; no pilot files or provider are accessed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.investigator.pydantic_ai_runtime import DURABLE_WRITE_MARGIN_SECONDS, PydanticAIRuntime
from eidos.investigator.runtime import DecisionAbort, DecisionRequest


@pytest.mark.fixture_llm
def test_n01_r2_six_read_responses_admit_followup_decisions():
    fixtures = json.loads((Path(__file__).parents[1] / 'fixtures/deadline/n01-r2-d3.json').read_text())
    for item in fixtures['slots']:
        _exercise_slot(item, fixtures)


def _exercise_slot(item, fixtures):
    now = [0.0]
    settings = []
    reads = []

    def provider(messages, info):
        settings.append(info.model_settings)
        if len(settings) == 1:
            now[0] += item['read_elapsed_seconds']
            reply = fixtures['read_response']
        else:
            reply = {'rationale': 'report incumbent from receipts', 'calls': [],
                     'control': {'action': 'conclude', 'summary': 'Locked candidate; validation only.'}}
        return ModelResponse(parts=[TextPart(json.dumps(reply))],
                             usage=RequestUsage(input_tokens=111, output_tokens=22))

    runtime = PydanticAIRuntime({'_model_object': FunctionModel(provider), 'model': 'openai-compatible:fake',
                                'output_mode': 'json', 'request_timeout': 300, 'max_requests_per_decision': 8})
    abort = DecisionAbort(deadline=item['deadline_seconds'], clock=lambda: now[0])
    request = DecisionRequest('turn_inv_06e6e82552e9_' + str(item['turn']), item['slot'], 'existing evidence', [],
                              {'deadline_seconds': item['deadline_seconds']}, abort=abort,
                              read_tools={'get_job': lambda job_id: reads.append(job_id) or {'id': job_id},
                                          'inspect_evidence': lambda artifact_id, max_bytes=8192: {'artifact_id': artifact_id}})
    result = runtime.decide(request)
    assert result.runtime_outcome is None
    assert result.proposal['control']['action'] == 'conclude'
    assert len(settings) == result.usage['calls'] == 2
    assert reads == ['job_1b0fc88b1f454622']
    assert 0 < settings[1]['timeout'] < settings[0]['timeout'] < 300
    assert settings[1]['timeout'] == pytest.approx(item['deadline_seconds'] - item['read_elapsed_seconds']
                                                  - DURABLE_WRITE_MARGIN_SECONDS)
    report = result.usage['structured_output']
    assert report['read_requests'] == 1 and report['errors'] == [] and report['repairs'] == []
    assert all(r['executed'] and r['available'] for r in report['reads'])
    assert result.usage['input_tokens'] == 222 and result.usage['output_tokens'] == 44

@pytest.mark.fixture_llm
def test_deadline_margin_returns_runtime_outcome_without_empty_wait_or_provider_call():
    def provider(messages, info):
        raise AssertionError('no time to start a request')
    runtime = PydanticAIRuntime({'_model_object': FunctionModel(provider), 'model': 'openai-compatible:fake',
                                'output_mode': 'json', 'request_timeout': 300})
    result = runtime.decide(DecisionRequest('t', 0, 'context', [], {},
                            abort=DecisionAbort(1.0, clock=lambda: 0.0)))
    assert result.proposal == {} and result.runtime_outcome == 'deadline-exhausted'
    assert result.usage['runtime_outcome'] == 'deadline-exhausted'
    assert result.usage['calls'] == 0 and not result.usage.get('structured_output', {}).get('errors')
