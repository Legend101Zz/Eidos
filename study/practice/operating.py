"""Monte Carlo operating characteristics of the *implemented* generator guard.

Vectorisation changes arithmetic only; a parity test compares decisions with evaluate.
The seed and effect size are preregistered, not selected from simulated verdicts.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.stats import t

from study.practice.build_cards import write_json
from study.practice.guard import (
    CATASTROPHIC_ALPHA,
    CATASTROPHIC_MARGIN,
    FINITE_SOURCE_SD_FLOORS,
    LOSS_RISK_ALPHA,
    guard_selftest,
    sign_flip_pvalues,
)


def pooled_interval(mu, var):
    floor = np.maximum(.01, np.median(var, axis=1) * .1)
    var = np.maximum(var, floor[:, None])
    w = 1 / var
    total = w.sum(axis=1)
    fixed = (w * mu).sum(axis=1) / total
    q = (w * (mu - fixed[:, None]) ** 2).sum(axis=1)
    c = total - (w * w).sum(axis=1) / total
    tau = np.maximum(0, (q - mu.shape[1] + 1) / c)
    w = 1 / (var + tau[:, None])
    pooled = (w * mu).sum(axis=1) / w.sum(axis=1)
    hk = np.maximum(1, (w * (mu - pooled[:, None]) ** 2).sum(axis=1) / (mu.shape[1] - 1))
    lower = pooled - t.ppf(.95, mu.shape[1] - 1) * np.sqrt(hk / w.sum(axis=1))
    upper = pooled + t.ppf(.95, mu.shape[1] - 1) * np.sqrt(hk / w.sum(axis=1))
    return pooled, lower, upper


def decisions(draws, roles, *, epsilon=.5, other_primaries=(), diagnostics=False, inference_cache=None):
    keys = sorted(draws)
    values = np.stack([draws[g] for g in keys], axis=1)
    means, variances = values.mean(axis=2), values.var(axis=2, ddof=1) / values.shape[2]
    effective = np.array([1 if g in ('gen-008', 'gen-009', 'gen-015') else values.shape[2] for g in keys])
    variances *= values.shape[2] / effective[None, :]
    source_floors = np.array([FINITE_SOURCE_SD_FLOORS.get(g, 1.04) ** 2 for g in keys])
    variances[:, effective == 1] = np.maximum(variances[:, effective == 1], source_floors[None, effective == 1])
    dfs = np.maximum(1, effective - 1)
    named = [i for i, g in enumerate(keys) if roles[g] not in ('calibration-only', 'control')]
    holdouts = [i for i, g in enumerate(keys) if roles[g] == 'holdout']
    if len(holdouts) < 5:
        return np.zeros(len(values), dtype=bool)
    harm = t.cdf((means[:, named] + epsilon) / np.sqrt(np.maximum(variances[:, named], .01)), dfs[named])
    harm = np.sort(harm, axis=1) * np.arange(len(named), 0, -1)
    no_harm = harm[:, 0] > .05
    catastrophic = means[:, named] + t.ppf(1 - CATASTROPHIC_ALPHA, dfs[named]) * np.sqrt(variances[:, named]) < -CATASTROPHIC_MARGIN
    no_harm &= ~catastrophic.any(axis=1)
    groups = [[i for i in named if roles[keys[i]] in ('design', 'holdout-class')]]
    groups += [[i for i in named if roles[keys[i]] == role] for role in ('design', 'holdout-class')]
    for group in groups:
        if len(group) >= 2:
            mean, _, upper = pooled_interval(means[:, group], variances[:, group])
            se = (upper - mean) / t.ppf(.95, len(group) - 1)
            no_harm &= mean + t.ppf(1 - LOSS_RISK_ALPHA, len(group) - 1) * se >= 0.
    mu, var = means[:, holdouts], variances[:, holdouts]
    key = hashlib.sha256(str(mu.shape).encode() + mu.tobytes() + var.tobytes()).digest() if inference_cache is not None else None
    if inference_cache is not None and key in inference_cache:
        pooled, lower, p = inference_cache[key]
    else:
        pooled, lower, _ = pooled_interval(mu, var)
        p = sign_flip_pvalues(mu, var)
        if inference_cache is not None:
            inference_cache[key] = pooled, lower, p
    # Conservative Bonferroni is exact Holm for a current p smaller than all
    # equally/higher preregistered p values; programme family rows use p_other=1.
    adjusted = np.minimum(1., p * (len(other_primaries) + 1))
    accepted = no_harm & (lower > 0) & (pooled >= epsilon) & (adjusted <= .05)
    return dict(accepted=accepted, primary_p=p, catastrophic_reject=catastrophic.any(axis=1)) if diagnostics else accepted


def wilson(accepted):
    n, count = len(accepted), int(accepted.sum())
    rate, z = count / n, 1.959963984540054
    center = (rate + z*z/(2*n)) / (1+z*z/n)
    half = z*np.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/(1+z*z/n)
    return dict(trials=n, accepted=count, acceptance_rate=rate,
                mc_lower95=float(center-half), mc_upper95=float(center+half))


def simulate(observed, roles, *, registration, trials=20000, seed=20261114, effect=.9):
    from study.practice.generators import registry
    from study.practice.ledger import require_registration
    require_registration(registration, classes=roles, exact=True, candidate=True)
    rng = np.random.default_rng(seed)
    observed_sd = {g: float(np.std(v, ddof=1)) for g, v in observed.items()}
    profiles = {f'homoskedastic-{sd}': {g: sd for g in roles} for sd in (.1, .3, .66, .8, 1.04)}
    profiles.update({f'observed-x{mult:.1f}': {g: sd * mult for g, sd in observed_sd.items()} for mult in (.5, 1., 1.5)})
    profiles['observed-fixed-source-correlated'] = observed_sd
    templates = {}
    for entry in registry():
        if entry['id'] in roles and entry['role'] == 'holdout':
            templates.setdefault(entry['nuisance_template'], []).append(entry['id'])
    named = [g for g in sorted(roles) if roles[g] in ('design', 'holdout', 'holdout-class')]
    rows, catastrophic_neutral = [], []
    for profile, sds in profiles.items():
        inference_cache = {}
        noise = {g: rng.normal(0, max(sds[g], .01), (trials, 1 if profile.endswith('correlated') and g in ('gen-008', 'gen-009', 'gen-015') else 4)) for g in roles}
        noise = {g: np.repeat(v, 4, axis=1) if v.shape[1] == 1 else v for g, v in noise.items()}
        scenarios = [('neutral', 0.), ('harmful', -.5), ('helpful', effect), ('helpful-0.8-sensitivity', .8),
                     ('neutral-elsewhere', 0.), ('card-specific', 0.)]
        scenarios += [(f'mixed-{loss:.1f}', loss) for loss in (-.5, -1., -2.)]
        scenarios += [(f'nuisance-specific/{template}', 0.) for template in templates]
        # Every named generator, including noisy finite sources: never hide a hard cell.
        scenarios += [(f'single-loss-4/{g}', -4.) for g in named]
        for name, shift in scenarios:
            shifts = {g: shift if roles[g] not in ('calibration-only', 'control') else 0. for g in roles}
            if name == 'card-specific':
                shifts[next(g for g in sorted(roles) if roles[g] == 'holdout')] = 2.
            elif name.startswith('mixed-') or name == 'neutral-elsewhere':
                shifts = {g: effect if roles[g] == 'holdout' else shift if roles[g] in ('design', 'holdout-class') else 0. for g in roles}
            elif name.startswith('nuisance-specific/'):
                shifts = {g: effect if g in templates[name.split('/', 1)[1]] else 0. for g in roles}
            elif name.startswith('single-loss-4/'):
                target_generator = name.split('/', 1)[1]
                shifts = {g: -4. if g == target_generator else effect if g in named else 0. for g in roles}
            draws = {g: v + shifts[g] for g, v in noise.items()}
            diagnostic = decisions(draws, roles, diagnostics=True, inference_cache=inference_cache)
            for primaries in (1, 8):
                accepted = diagnostic['accepted'] & (diagnostic['primary_p'] <= .05 / primaries)
                power_required = profile not in ('observed-x0.5', 'observed-x1.5')
                target = (.8 if name == 'helpful' and power_required else .1 if name == 'card-specific' or name.startswith('nuisance-specific/') or name == 'mixed--1.0'
                          else .05 if name in ('neutral', 'harmful', 'mixed--2.0') or name.startswith('single-loss-4/') else None)
                bounds = wilson(accepted)
                met = None if target is None else bounds['mc_lower95'] >= target if name == 'helpful' else bounds['mc_upper95'] <= target
                rows.append(dict(profile=profile, scenario=name, primaries=primaries, **bounds, target=target, target_met=met))
                if name == 'neutral' and primaries == 1:
                    rejection = wilson(diagnostic['catastrophic_reject'])
                    catastrophic_neutral.append(dict(profile=profile, trials=trials, rejected=rejection['accepted'],
                        false_rejection_rate=rejection['acceptance_rate'], mc_lower95=rejection['mc_lower95'],
                        mc_upper95=rejection['mc_upper95'], target=.05, target_met=rejection['mc_upper95'] <= .05))
            print(json.dumps(dict(profile=profile, scenario=name, complete=True)), flush=True)
    primary = [r for r in rows if r['primaries'] == 1 and r['target'] is not None]
    core = [r for r in primary if not r['scenario'].startswith('single-loss-4/')]
    single = [r for r in primary if r['scenario'].startswith('single-loss-4/')]
    return dict(schema='practice-operating/4', seed=seed, trials=trials, effect_se=effect,
                effect_justification='The orchestrator prospectively fixed +0.9 paired-test-SE as the one-primary minimum detectable effect before any v2 candidate evaluation. +0.8 is sensitivity only; high-variance x1.5 helpful power is descriptive.',
                observed_instance_sd=observed_sd, rows=rows, targets_met=all(r['target_met'] for r in primary + catastrophic_neutral),
                core_targets_met=all(r['target_met'] for r in core),
                single_generator_targets_met=all(r['target_met'] for r in single + catastrophic_neutral),
                catastrophic_neutral=catastrophic_neutral,
                unmet_targets=[r for r in primary + catastrophic_neutral if not r['target_met']],
                nuisance_groups=templates, pooled_harm_margin_se=0., finite_source_sd_floors=FINITE_SOURCE_SD_FLOORS,
                mechanical_checks='Condition on valid in-fold, null-claim and resource checks; flags isolate statistical error rates.',
                scope='Exposed calibration, not independent validation. Exact sign-flip and modified HK use independent generators. Pooled design/class and each-role zero-margin loss-risk screens use one-sided alpha .25 (upper 50% interval), calibrated on the previously exposed r3 SD profile including x1.5. Conventional .05 credible-loss tests remain separate. Any single named generator with a one-sided 97.5% upper t bound below -3 SE rejects; alpha .025 was calibrated on exposed r3 neutral draws (alpha .05 missed the x1.5 Wilson false-rejection bound). Its neutral familywise false-rejection rate is measured separately. Mixed cells help holdouts +0.9 and harm design/class generators; -0.5 is descriptive. Single-loss cells harm the named generator -4 and help all others +0.9, including every high-variance source. Four instances cannot guarantee detection of those losses; every failed target remains visible. All rates have Wilson 95% bounds. Eight-primary sensitivity carries no 80% power promise.',
                controls=guard_selftest(registration=registration))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--evaluation-id', required=True)
    parser.add_argument('--candidate-digest', required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.family).read_text())
    roles = {r['generator']: r['role'] for r in report['cards']}
    registration = report['preregistration']
    if (registration['evaluation_id'], registration['candidate_digest']) != (args.evaluation_id, args.candidate_digest):
        raise ValueError('Monte Carlo must use the registered family candidate identity')
    result = simulate(report['comparator_effects_test_se'], roles, registration=registration)
    write_json(args.out, result)
    print(json.dumps(dict(targets_met=result['targets_met'])))


if __name__ == '__main__':
    main()
