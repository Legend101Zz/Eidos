"""Prospective operator intent, committed before any investigator/model work."""
from __future__ import annotations

from study.common import EXPORT_READINESS_CONTRACT
from study.private_card_registry import PRIVATE_CARD_BARS, PRIVATE_CLASSIFICATION, PRIVATE_REGRESSION

from .common import write_json

METRIC = 'paired-report/1:reference-accuracy'
RULE_ID = 'practical-accuracy-gain'
REPRODUCTION_CONTRACT = (
    'For every requested seed, `reproduce_command` must write the fresh checkpoint to '
    '`{out}/<basename of candidate.model_path>`. The evaluator passes that file to your '
    'infer command. Use compatible formats for the locked and fresh checkpoints.'
)


def charter(card):
    paired = card in ('D03', 'D06', 'N01', 'N04') or card in PRIVATE_CLASSIFICATION
    metric = 'classification-report/1:accuracy' if paired else METRIC
    exposure = (
        'Prospectively sealed development task. The bundle supplies training and validation only. '
        'The Pilot 4 charter records limited pre-run builder exposure.'
        if card in PRIVATE_CARD_BARS else 'Public development task; no evaluator test data supplied'
    )
    body = {'study_id': card.lower() + '-pilot-practical',
            'question': 'Reproduce the supplied baseline (seed 11), then evaluate the practical accuracy bar.',
            'known_exposures': [{'description': exposure}],
            'reproduction_contract': REPRODUCTION_CONTRACT,
            'export_readiness_contract': EXPORT_READINESS_CONTRACT,
            'resource_limits': {'wall_seconds': 1800, 'model_tokens': 8_000_000, 'threads': 1}}
    if card in ('D03', 'D04', 'D05', 'D06', 'N01', 'N02', 'N03', 'N04') or card in PRIVATE_CARD_BARS:
        body['pilot_2_setup'] = {
            'policy': 'P2S prospective parity corrections; pilot-1 records remain unchanged.',
            'environments': 'All selectable environments use the arms-1–3 research interpreter, '
                            'including lab infrastructure controls; only the packaged task is the project root.',
            'information': 'Arms 2 and 3 receive METHOD.md; arm 4 embeds the same METHOD.md with task text. '
                           'Arms 3 and 4 are told that baseline project-command needs no authored source.',
            'run_classification': 'All arms use final outcomes: recovered intermediate errors do not override '
                                  'a successful final result and valid answer. Incident, wall-timeout and '
                                  'token-budget rules retain precedence.',
            'arm4_timing': 'Lab startup, project import, charter, component installation, controls and prompt '
                           'assembly precede the investigation clock. Each run records setup_wall_s separately; '
                           'the launcher excludes it from the outer run deadline. Provider bridge startup, '
                           'investigation, export and accounting remain inside the timed interval. '
                           'adapter_wall_s includes setup and teardown.'}
    if card in PRIVATE_CARD_BARS:
        # Pilot 4 onward uses two arms; retain historical public-card charter bytes.
        body['pilot_2_setup'].update(
            environments='All selectable environments use the coding-agent research interpreter, '
                         'including lab infrastructure controls; only the packaged task is the project root.',
            information='Arm 2 receives METHOD.md; arm 4 embeds the same METHOD.md with task text. '
                        'Arm 4 is told that baseline project-command needs no authored source.')
    if card == 'D01':
        return {**body, 'question': 'Operational import and reproduction; no improvement requested.',
                'exploration_only': True}
    if card in ('D04', 'D05', 'N02', 'N03') or card in PRIVATE_REGRESSION:
        if card in PRIVATE_REGRESSION:
            rmse_bar = f"{PRIVATE_CARD_BARS[card][1]:.0%}"
        elif card.startswith('N'):
            # Pilot-3 addendum 1: read the frozen bar from JSON. Arms 3 and 4 run in the lab interpreter, which
            # has no NumPy, so the NumPy-based packaging module must not be imported here.
            import json as _json
            from pathlib import Path as _Path
            frozen = _json.loads((_Path(__file__).resolve().parents[1] / 'packaging' / 'p3_frozen.json').read_text())[card]
            if not frozen.get('qualified'):
                raise ValueError(f'{card} dropped by prospective qualification')
            rmse_bar = frozen['bar']
        else:
            rmse_bar = 0.5
        return {**body, 'question': f'Reduce held-out RMSE by at least {rmse_bar} versus baseline seed 11; reproduce from fresh initialization.', 'exploration_only': True, 'notes': 'No built-in RMSE assessor; see CHARTER_RULES.md. Independent evaluator enforces the practical bar.'}
    if card not in ('D03', 'D06', 'D02', 'D12', 'N01', 'N04', 'SYNTHETIC_LINEAR_SMOKE') and card not in PRIVATE_CLASSIFICATION:
        raise ValueError(f'no prospective charter policy for {card}')
    bar = PRIVATE_CARD_BARS[card][1] if card in PRIVATE_CLASSIFICATION else {'D03':0.01,'D06':0.30}.get(card,0.02)
    return {**body, 'exploration_only': False,
            'population': 'Evaluator-owned held-out examples from the task population. Visible validation '
                          'is selection evidence only; it cannot confirm this population.',
            'comparator': 'Supplied baseline trained from fresh initialization with seed 11. '
                          'Candidate recipe must also reproduce from fresh initialization.',
            'metrics': {metric: {'direction': 'increase', 'denominator': 'held-out examples',
                                'description': ('Ordinary multiclass accuracy from aligned classification rows' if paired else 'Ordinary per-example classification accuracy, encoded as reference correctness in paired-report rows')}},
            'success_rules': [{'id': RULE_ID, 'metric': metric, 'min_effect': bar,
                               'decision_standard': {'kind': 'paired-difference-ci', 'confidence': 0.95},
                               'comparison': {'baseline_arm_role': 'baseline', 'candidate_arm_role': 'candidate'},
                               'guardrails': []}],
            'notes': ('Use eidos.task.paired-classification-report and eidos.assessors.paired-ci@1.2.0. ' if paired else 'Use eidos.assessors.paired-ci. ') + f'Its lower 95% confidence bound must reach {bar}. ' +
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
