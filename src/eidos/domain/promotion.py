"""ADR 0047: pure preparation expansion and recorded candidate facts. No lock authority."""
from __future__ import annotations

import copy
from typing import Any, NoReturn

from eidos.domain import exploration as rules
from eidos.domain.errors import Invalid
from eidos.domain.turns import validate_refs

PORTFOLIO_REF = 'eidos.search.recipe-portfolio@1.0.0'


def expand(calls: list[dict[str, Any]], view: dict[str, Any], limits: dict[str, Any], *,
           proposed_by: str) -> list[dict[str, Any]]:
    """Replace a final prepare_candidate with four ordinary component calls."""
    positions = [i for i, c in enumerate(calls) if c['tool'] == 'prepare_candidate']
    if not positions:
        return calls
    def refuse(message: str) -> NoReturn:
        raise Invalid('prepare_candidate: ' + message)
    if positions != [len(calls) - 1]:
        refuse('must be the last call, once per decision')
    if len(calls) + 3 > int(limits['max_calls']):
        refuse(f"expanded decision has {len(calls) + 3} calls; turn allows {limits['max_calls']}")
    pol = view.get('policy') or {}
    if pol.get('policy') != 'exploration-policy/3':
        refuse('requires exploration-policy/3 opt-in')
    ref = pol.get('project_command_ref')
    if not ref or any(r not in view.get('releases', []) for r in (ref, PORTFOLIO_REF)):
        refuse('required project-command or portfolio release is not installed and active')
    if not view.get('baseline_predictions'):
        refuse('baseline has no validation_predictions')
    args = calls[-1]['args']
    seeds = args.get('seeds')
    if not isinstance(seeds, list) or len(seeds) != 2 or any(type(s) is not int or s < 0 for s in seeds) or seeds[0] == seeds[1]:
        refuse('seeds must be two distinct non-negative integers')
    source = args.get('source')
    st = view.get('exploration') or {}
    if st.get('incumbent'):
        if not isinstance(source, str):
            refuse('re-preparation requires an existing finished source artifact')
        metrics = view.get('source_metrics') or {}
        rule = st.get('rule') or {}
        value = metrics.get('validation_chosen')
        # Portfolio regression scores use a higher-is-better negative RMSE.
        if rule.get('direction') == 'lower_is_better' and value is not None:
            value = -value
        challenger = dict(value=value, se=metrics.get('se_chosen'))
        incumbent = st['incumbent'].get('evidence') or {}
        if value is None or not incumbent or not rules.clears_margin(challenger, incumbent, rule):
            refuse(f'margin pre-check failed: challenger {challenger}, incumbent {incumbent}, rule {rule}')
    if isinstance(source, dict):
        from eidos.domain.turns import parse_ref
        parsed = parse_ref(source.get('$ref', ''))
        if not parsed or parsed[1] != 'outputs.promotion_source' or parsed[0] >= len(calls) - 1:
            refuse('source reference must name an earlier portfolio promotion_source')
        producer = calls[parsed[0]]
        if producer['tool'] != 'run_component' or producer['args'].get('component_ref') != PORTFOLIO_REF:
            refuse('source reference must name the pinned portfolio release')
        config = producer['args'].get('config', {})
        if not isinstance(config, dict):
            refuse('portfolio config must be an object')
        basis = 'explicit-export' if config.get('mode') == 'export' else 'one-se-choice'
    elif isinstance(source, str):
        basis = str((view.get('source') or {}).get('basis') or '')
        if basis not in ('one-se-choice', 'explicit-export'):
            refuse('source must be a verified portfolio promotion_source bundle')
    else:
        refuse('source must be an artifact id or portfolio output reference')
    metrics = view.get('source_metrics') or {}
    fit = float(metrics.get('fit_seconds_chosen') or 0)
    if fit * 2 > 120 or float(metrics.get('checkpoint_bytes_chosen') or 0) > 64_000_000 or metrics.get('export_eligible') == 0:
        refuse('source exceeds evaluator fit/checkpoint export guards')
    hold = (st.get('allowance') or {}).get('holdback_seconds')
    if hold and 4 * (fit + 5) > .8 * float(hold):
        refuse('estimated preparation exceeds 0.8 of hold-back')
    inv = view['investigation']['body']
    inputs = inv.get('inputs') or {}
    if not all(inputs.get(k) for k in ('train', 'validation')):
        refuse('requires declared train and validation inputs')
    index = len(calls) - 1
    stamp = dict(tool='prepare_candidate', proposed_by=proposed_by, basis=basis)
    def output(slot: str) -> dict[str, str]:
        return {'$ref': f'call[{index}].outputs.{slot}'}
    def call(command: str, purpose: str, seed: int, bindings: dict[str, Any]) -> dict[str, Any]:
        return dict(tool='run_component', macro=dict(stamp), args=dict(component_ref=ref,
            environment=inv.get('environment', 'default'), purpose=purpose, inputs=dict(source=source, **bindings),
            config=dict(argv=['python', 'project.py', command, '--inputs', '{work}', '--out', '{out}', '--seed', '{seed}'],
                        seed=seed, timeout_seconds=120)))
    train = dict(train=inputs['train'], validation=inputs['validation'])
    result = copy.deepcopy(calls[:-1]) + [
        call('train', 'open-search', seeds[0], train),
        call('check', 'reproduction', seeds[0], dict(validation=inputs['validation'], model=output('model'), extra_a=output('validation_predictions'))),
        call('compare', 'measurement-development', seeds[0], dict(validation=inputs['validation'], extra_a=output('validation_predictions'), extra_b=view['baseline_predictions'])),
        call('train', 'reproduction', seeds[1], train),
    ]
    validate_refs(result)
    return result


def suggested_lock(view: dict[str, Any]) -> dict[str, Any] | None:
    """Project only successful, stamped preparation receipts; investigator copies or declines the lock."""
    jobs = view.get('jobs') or []
    trains = [j for j in jobs if j.get('status') == 'succeeded' and j.get('macro') and
              j.get('purpose') == 'open-search' and 'train' in (j.get('config') or {}).get('argv', [])]
    if not trains:
        return None
    job = trains[-1]
    group = [j for j in jobs if j.get('macro') == job['macro'] and j.get('status') == 'succeeded']
    metrics = (job.get('receipt') or {}).get('metrics') or {}
    metric = next((m for m in ('validation_accuracy', 'validation_rmse') if m in metrics), None)
    if metric is None or metric + '_se' not in metrics:
        return None
    se = metrics[metric + '_se']
    existing = (view.get('exploration') or {}).get('rule')
    rule = existing or rules.parse_rule(dict(metric=metric, split='validation',
        direction='higher_is_better' if metric.endswith('accuracy') else 'lower_is_better', scorer=job['component'],
        data=dict(inputs={k: job['inputs'][k] for k in ('train', 'validation')}),
        **({'margin': dict(metric=metric + '_se', multiple=view['policy']['selection_margin_multiple'])}
           if (view.get('policy') or {}).get('selection_margin_multiple') is not None else {}),
        reproduce_within=max(2 * se, .005)), policy_id='exploration-policy/3')
    def command_metrics(command: str) -> dict[str, Any]:
        found: dict[str, Any] = next((j for j in group if command in (j.get('config') or {}).get('argv', [])), {})
        metrics = dict((found.get('receipt') or {}).get('metrics') or {})
        if command == 'check' and 'readiness_max_abs_diff' in metrics:
            # The pinned producer excludes booleans from numeric receipt metrics.
            # A successful generated check raises on mismatch before it emits this value.
            metrics['readiness_ok'] = 1.
        return metrics
    readiness, paired = command_metrics('check'), command_metrics('compare')
    reproduction = rules.reproduction(jobs, job['id'], rule)
    if not (readiness.get('readiness_ok') == 1 and paired.get('paired_n', 0) > 0
            and all(k in paired for k in ('paired_delta', 'paired_delta_lo', 'paired_delta_hi'))
            and reproduction and reproduction.get('reproduced_by')):
        return None  # prepared lock text is offered only after the entire preparation succeeds
    return dict(lock=dict(job_id=job['id'], selection=rule), validation=dict(value=metrics[metric], se=se),
                readiness=readiness, paired=paired, reproduction=reproduction, macro=job['macro'],
                fit_seconds=metrics.get('fit_seconds'), checkpoint_bytes=metrics.get('checkpoint_bytes'))
