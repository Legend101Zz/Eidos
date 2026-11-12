"""D3 N01-r2: six valid read replies at 249/233/221 seconds must admit their follow-ups.

Fixture shape follows D3's terminal usage receipts; no pilot files or provider are accessed.
"""
from __future__ import annotations

import hashlib
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
    assert hashlib.sha256(item['response_text'].encode()).hexdigest() == item['response_text_sha256']
    assert item['receipt']['calls'] == 1 and item['receipt']['structured_output']['read_requests'] == 1
    now = [0.0]
    settings = []
    time_notes = []
    reads = []

    def provider(messages, info):
        settings.append(info.model_settings)
        time_notes.append(info.instructions)
        if len(settings) == 1:
            now[0] += item['read_elapsed_seconds']
            reply = json.loads(item['response_text'])
        else:
            reply = {'rationale': 'report incumbent from receipts', 'calls': [],
                     'control': {'action': 'conclude', 'summary': 'Locked candidate; validation only.'}}
        return ModelResponse(parts=[TextPart(item['response_text'] if len(settings) == 1 else json.dumps(reply))],
                             usage=RequestUsage(input_tokens=item['receipt']['input_tokens'] if len(settings) == 1 else 111,
                                                output_tokens=item['receipt']['output_tokens'] if len(settings) == 1 else 22))

    runtime = PydanticAIRuntime({'_model_object': FunctionModel(provider), 'model': 'openai-compatible:fake',
                                'output_mode': 'json', 'request_timeout': 300, 'max_requests_per_decision': 8})
    abort = DecisionAbort(deadline=item['deadline_seconds'], clock=lambda: now[0])
    request = DecisionRequest('turn_inv_06e6e82552e9_' + str(item['turn']), item['slot'], 'existing evidence', [],
                              {'deadline_seconds': item['deadline_seconds']}, abort=abort,
                              read_tools={'get_job': lambda job_id: reads.append({'tool': 'get_job', 'args': {'job_id': job_id}}) or {'id': job_id},
                                          'inspect_evidence': lambda artifact_id, max_bytes=8192: reads.append(
                                              {'tool': 'inspect_evidence', 'args': {'artifact_id': artifact_id, 'max_bytes': max_bytes}}) or {'artifact_id': artifact_id}})
    result = runtime.decide(request)
    assert result.runtime_outcome is None
    assert "Runtime time left:" in time_notes[0] and "usable request budget:" in time_notes[1]
    assert time_notes[0] != time_notes[1]
    assert result.proposal['control']['action'] == 'conclude'
    assert len(settings) == result.usage['calls'] == 2
    assert reads == json.loads(item['response_text'])['read']
    assert 0 < settings[1]['timeout'] < settings[0]['timeout'] < 300
    assert settings[1]['timeout'] == pytest.approx(item['deadline_seconds'] - item['read_elapsed_seconds']
                                                  - DURABLE_WRITE_MARGIN_SECONDS)
    report = result.usage['structured_output']
    assert report['read_requests'] == 1 and report['errors'] == [] and report['repairs'] == []
    assert all(r['executed'] and r['available'] for r in report['reads'])
    assert result.usage['input_tokens'] == item['receipt']['input_tokens'] + 111
    assert result.usage['output_tokens'] == item['receipt']['output_tokens'] + 22
    return result

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


@pytest.mark.fixture_llm
@pytest.mark.parametrize("wrapped", [False, True])
def test_sdk_timeout_at_the_runtime_boundary_is_runtime_exhaustion_with_uncertain_usage(monkeypatch, wrapped):
    import httpx
    from openai import APITimeoutError
    from pydantic_ai.exceptions import ModelTimeoutError

    from eidos.investigator import pydantic_ai_runtime as runtime_module

    now = [0.0]
    monkeypatch.setattr(runtime_module, 'time', type('Clock', (), {'monotonic': staticmethod(lambda: now[0])}))

    def provider(messages, info):
        assert info.model_settings['timeout'] == 1.0
        now[0] = 1.0
        if wrapped:
            raise ModelTimeoutError('fixture', 'request timed out at its deadline clip')
        raise APITimeoutError(request=httpx.Request('POST', 'https://fixture.invalid'))

    runtime = PydanticAIRuntime({'_model_object': FunctionModel(provider), 'model': 'openai-compatible:fake',
                                'output_mode': 'json', 'request_timeout': 300})
    result = runtime.decide(DecisionRequest('t', 0, 'context', [], {},
                            abort=DecisionAbort(3.0, clock=lambda: now[0])))
    assert result.runtime_outcome == 'deadline-exhausted' and result.proposal == {}
    assert result.usage['uncertain'] is True and result.usage['aborted']['in_flight'] is True
    assert result.usage['in_flight_estimate']['input_tokens'] > 0
    assert 'failure' not in result.usage
