"""Prospective operator intent, committed before any investigator/model work."""
from __future__ import annotations

from .common import write_json

METRIC = 'paired-report/1:reference-accuracy'
RULE_ID = 'practical-accuracy-gain'


def charter(card):
    body = {'study_id': card.lower() + '-pilot-practical',
            'question': 'Reproduce the supplied baseline (seed 11), then evaluate the practical accuracy bar.',
            'known_exposures': [{'description': 'Public development task; no evaluator test data supplied'}],
            'resource_limits': {'wall_seconds': 1800, 'model_tokens': 8_000_000, 'threads': 1}}
    if card == 'D01':
        return {**body, 'question': 'Operational import and reproduction; no improvement requested.',
                'exploration_only': True}
    if card not in ('D02', 'D12', 'SYNTHETIC_LINEAR_SMOKE'):
        raise ValueError(f'no prospective charter policy for {card}')
    return {**body, 'exploration_only': False,
            'population': 'Evaluator-owned held-out examples from the task population. Visible validation '
                          'is selection evidence only; it cannot confirm this population.',
            'comparator': 'Supplied baseline trained from fresh initialization with seed 11. '
                          'Candidate recipe must also reproduce from fresh initialization.',
            'metrics': {METRIC: {'direction': 'increase', 'denominator': 'held-out examples',
                                'description': 'Ordinary per-example classification accuracy, encoded as '
                                               'reference correctness in paired-report rows'}},
            'success_rules': [{'id': RULE_ID, 'metric': METRIC, 'min_effect': 0.02,
                               'decision_standard': {'kind': 'paired-difference-ci', 'confidence': 0.95},
                               'comparison': {'baseline_arm_role': 'baseline', 'candidate_arm_role': 'candidate'},
                               'guardrails': []}],
            'notes': 'Use eidos.assessors.paired-ci. Its lower 95% confidence bound must reach 0.02. '
                     'Reference correctness must encode ordinary accuracy, including multiclass tasks. '
                     'Held-out status and fresh initialization are provenance obligations, not enforced '
                     'by this numeric rule. Never treat visible-validation passage as evaluator confirmation.'}


def commit(c, project, card, run_dir):
    body = charter(card)
    result = c.command('commit_study_charter', {'charter': body}, project=project,
                       key='pilot-practical-charter:' + project)['result']
    receipt = {**result, 'body': body}
    write_json(run_dir / 'charter.json', receipt)
    return {'charter_id': result['charter_id'], 'charter_revision': result['revision'],
            'charter_digest': result['digest']}
