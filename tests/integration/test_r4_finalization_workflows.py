"""G4 coordinator regression: one recovery, runtime/model separation, no skipped hold-back.

Scripted activities only. Temporal is test-owned; no provider or lab endpoint is used.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from temporalio import activity
from temporalio.worker import Worker
from test_workflows import FakeInvestigator, env  # noqa: F401 - fixture

from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, InvestigationWorkflow

pytestmark = pytest.mark.temporal


class Recovery(FakeInvestigator):
    def __init__(self, mode, fail_final=False):
        super().__init__()
        self.mode, self.fail_final = mode, fail_final
        self.final = False
        self.transitions = []
        self.decisions = []
        self.deadline = None
        self.marks = []

    @activity.defn(name='begin_turn')
    async def begin_turn(self, a):
        result = await super().begin_turn(a)
        if self.mode == 'deadline':
            if self.deadline is None:
                self.deadline = time.time() + 10
            result['completion'] = {'deadline_unix': self.deadline, 'conclude_threshold_seconds': 2,
                                    'remaining_seconds': self.deadline - time.time(), 'conclude_now': False}
            if not self.final:
                self.exploration_boundary = time.time() + 0.4
                result['completion']['exploration_deadline_unix'] = self.exploration_boundary
            else:
                result['completion']['finalization'] = True
        if self.final:
            result.update(finalization=True, max_model_calls=1)
        return result

    @activity.defn(name='build_context')
    async def build_context(self, a):
        if self.mode == 'deadline' and not self.final:
            await asyncio.sleep(0.5)  # the exploratory turn reaches its true boundary
        return {'context_artifact_id': 'fixture-context'}

    @activity.defn(name='decide')
    async def decide(self, a):
        self.decisions.append(a)
        if not self.final:
            if self.mode == 'runtime':
                return {'accepted': False, 'runtime_outcome': 'deadline-exhausted', 'reason': 'runtime-bounded'}
            return {'accepted': False, 'reason': 'invalid', 'decision_id': 'refusal'}
        if self.fail_final:
            return {'accepted': False, 'reason': 'invalid', 'decision_id': 'final-refusal'}
        return {'accepted': True, 'decision_id': 'final', 'n_calls': 0,
                'control': {'action': 'conclude', 'summary': 'Locked incumbent; validation only, no confirmation.'}}

    @activity.defn(name='enter_finalization')
    async def enter_finalization(self, a):
        self.transitions.append((time.time(), a))
        self.final = True
        return {'enabled': True}

    @activity.defn(name='mark_needs_input')
    async def mark_needs_input(self, a):
        self.marks.append(a)
        return {'already': False}


@pytest.mark.parametrize('mode, expected_turns', [('invalid', 4), ('runtime', 2), ('deadline', 2)])
async def test_coordinator_admits_one_finalization_and_preserves_holdback(env, mode, expected_turns):  # noqa: F811
    fake = Recovery(mode)
    wid = 'inv-r4-' + uuid.uuid4().hex
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        result = await asyncio.wait_for(env.client.execute_workflow(InvestigationWorkflow.run,
            {'investigation_id': wid, 'project_id': 'p', 'idle_poll_seconds': 1},
            id=wid, task_queue=WORKFLOW_QUEUE), timeout=20)
    assert result['reason'] == 'concluded' and result['turn_counter'] == expected_turns
    assert len(fake.transitions) == 1 and fake.marks == [] and fake.executed == []
    if mode == 'runtime':
        assert fake.completed[0]['stop_reason'] == 'runtime-bounded'
        assert fake.completed[0]['runtime_outcome'] == 'deadline-exhausted'
        assert len(fake.decisions) == 2  # no fake refusal/retry loop
    if mode == 'invalid':
        assert [r['stop_reason'] for r in fake.completed] == ['invalid-proposals'] * 3 + ['concluded']
    if mode == 'deadline':
        entered_at, trigger = fake.transitions[0]
        assert trigger['trigger'] == 'exploration-boundary'
        assert fake.exploration_boundary <= entered_at < fake.deadline - 5
        assert fake.completed[0]['stop_reason'] == 'deadline'


async def test_failed_finalization_is_not_retried_and_has_structured_input_needed(env):  # noqa: F811
    fake = Recovery('runtime', fail_final=True)
    wid = 'inv-r4-failure-' + uuid.uuid4().hex
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        result = await asyncio.wait_for(env.client.execute_workflow(InvestigationWorkflow.run,
            {'investigation_id': wid, 'project_id': 'p'}, id=wid, task_queue=WORKFLOW_QUEUE), timeout=20)
    assert result['status'] == 'needs_input' and result['turn_counter'] == 2
    assert len(fake.transitions) == 1 and len(fake.decisions) == 2
    assert fake.marks[0]['code'] == 'finalization-failed'
    assert fake.marks[0]['details']['export_receipt'] == {'kind': 'finalization_export_receipt', 'id': wid}


@pytest.mark.parametrize('mode', ['runtime', 'deadline'])
async def test_operator_runtime_outcome_waits_without_a_hot_loop(env, mode):  # noqa: F811
    class Operator(Recovery):
        @activity.defn(name='enter_finalization')
        async def enter_finalization(self, a):
            self.transitions.append((time.time(), a))
            return {'enabled': False}

        @activity.defn(name='check_news')
        async def check_news(self, a):
            return {'news': False}

    fake = Operator(mode)
    wid = 'inv-r4-operator-' + uuid.uuid4().hex
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        handle = await env.client.start_workflow(InvestigationWorkflow.run,
            {'investigation_id': wid, 'project_id': 'p', 'idle_poll_seconds': 1},
            id=wid, task_queue=WORKFLOW_QUEUE)
        try:
            for _ in range(50):
                if fake.marks:
                    break
                await asyncio.sleep(0.1)
            assert fake.marks[0]['code'] == 'runtime-bounded'
            assert len(fake.decisions) == (1 if mode == 'runtime' else 0)
            await asyncio.sleep(1.5)
            assert fake.turns == len(fake.transitions) == 1
        finally:
            await handle.terminate()
