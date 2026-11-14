"""Differential round accounting on real recorded development ledgers and a larger portfolio."""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from eidos.domain import exploration as rules

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
spec = importlib.util.spec_from_file_location('rounds_before_r2', FIXTURES / 'step3_rounds_before_r2.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


@pytest.mark.parametrize('v3', [False, True])
def test_memoized_rounds_equal_old_implementation_on_recorded_ledger_prefixes(v3):
    snapshots = json.loads((FIXTURES / 'step3-round-ledgers.json').read_text())
    for ledger in snapshots:
        found = rules.rule_from_notes(ledger['notes'], policy_id='exploration-policy/3')
        rule = found[0]
        versions = rules.lock_versions(ledger['notes'], lambda n: rules.note_json(n).get('job_id'))
        note_turns = ledger['note_turns']
        excluded = rules.reproduction_jobs(ledger['jobs'], [v['job_id'] for v in versions])
        args = (ledger['round_log'], ledger['jobs'], note_turns[versions[0]['note_id']],
                note_turns[versions[-1]['note_id']], excluded, rule, set())
        assert rules.rounds(*args, v3=v3) == reference.rounds(*args, v3=v3)


def test_large_round_ledger_memoizes_evidence_once_per_job(monkeypatch):
    # Scored fits, evidence-consuming support, refine/rescore, diagnosis, failed and pending jobs.
    rule = dict(metric='accuracy', split='validation', direction='higher_is_better', scorer='fit',
                data=dict(inputs=dict(validation='v')), margin=dict(metric='se', multiple=1))
    jobs, log = [], []
    for i in range(64):
        group = [dict(id=f'fit-{i}', component='fit', purpose='open-search', inputs=dict(validation='v'),
                      outputs=[dict(artifact_id=f'm-{i}')], receipt=dict(metrics=dict(accuracy=.8, se=.02))),
                 dict(id=f'check-{i}', component='check', purpose='reproduction', inputs=dict(model=f'm-{i}'), outputs=[]),
                 dict(id=f'compare-{i}', component='compare', purpose='measurement-development', inputs=dict(model=f'm-{i}'), outputs=[]),
                 dict(id=f'refine-{i}', component='eidos.search.recipe-portfolio@1.0.0', purpose='open-search', inputs={},
                      outputs=[], receipt=dict(metrics=dict(round_evaluated=1))),
                 dict(id=f'rescore-{i}', component='eidos.search.recipe-portfolio@1.0.0', purpose='reproduction', inputs={},
                      outputs=[], receipt=dict(metrics=dict(round_evaluated=1))),
                 dict(id=f'diagnosis-{i}', component='eidos.diagnose.baseline-errors@1.1.0', purpose='open-search', inputs={}, outputs=[]),
                 dict(id=f'failed-{i}', component='fit', purpose='open-search', status='failed', inputs={}, outputs=[])]
        for j in group:
            j.setdefault('status', 'succeeded')
            j.setdefault('config', dict(index=i))
        jobs.extend(group)
        log.append(dict(turn=i + 2, jobs=[j['id'] for j in group]))
    jobs[-7]['status'] = 'running'
    # Compare to the frozen implementation too, with the production reproduction exclusion shape.
    excluded = rules.reproduction_jobs(jobs, ['fit-0'])
    started = time.perf_counter()
    expected = reference.rounds(log, jobs, 1, 1, excluded, rule, v3=True)
    old_seconds = time.perf_counter() - started
    original = rules.selection_evidence
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(rules, 'selection_evidence', counted)
    started = time.perf_counter()
    assert rules.rounds(log, jobs, 1, 1, excluded, rule, v3=True) == expected
    print(json.dumps(dict(jobs=len(jobs), before_seconds=old_seconds, after_seconds=time.perf_counter() - started)))
    assert len(calls) == len(jobs) == 448
    assert len(set(calls)) == 448
    # A new snapshot must see a completion rather than retain the prior pending result.
    jobs[-7]['status'] = 'succeeded'
    completed = rules.rounds(log, jobs, 1, 1, excluded, rule, v3=True)
    assert completed[0] == expected[0] and completed[1] == expected[1] + 1 and completed[2] == []
    assert len(calls) == 2 * len(jobs)
