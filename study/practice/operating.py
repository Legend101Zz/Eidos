"""Monte Carlo operating characteristics of the *implemented* generator guard.

Vectorisation changes arithmetic only; a parity test compares decisions with evaluate.
The seed and effect size are preregistered, not selected from simulated verdicts.
"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.stats import t

from study.practice.build_cards import write_json
from study.practice.guard import FINITE_SOURCE_SD_FLOORS, guard_selftest, sign_flip_pvalues


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


def decisions(draws, roles, *, epsilon=.5, other_primaries=()):
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
    groups = [[i for i in named if roles[keys[i]] in ('design', 'holdout-class')]]
    groups += [[i for i in named if roles[keys[i]] == role] for role in ('design', 'holdout-class')]
    for group in groups:
        if len(group) >= 2:
            mean, _, upper = pooled_interval(means[:, group], variances[:, group])
            se = (upper - mean) / t.ppf(.95, len(group) - 1)
            no_harm &= mean + t.ppf(.85, len(group) - 1) * se >= 0.
    mu, var = means[:, holdouts], variances[:, holdouts]
    pooled, lower, _ = pooled_interval(mu, var)
    p = sign_flip_pvalues(mu, variances[:, holdouts])
    # Conservative Bonferroni is exact Holm for a current p smaller than all
    # equally/higher preregistered p values; programme family rows use p_other=1.
    adjusted = np.minimum(1., p * (len(other_primaries) + 1))
    return no_harm & (lower > 0) & (pooled >= epsilon) & (adjusted <= .05)


def wilson(accepted):
    n, count = len(accepted), int(accepted.sum())
    rate, z = count / n, 1.959963984540054
    center = (rate + z*z/(2*n)) / (1+z*z/n)
    half = z*np.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/(1+z*z/n)
    return dict(trials=n, accepted=count, acceptance_rate=rate,
                mc_lower95=float(center-half), mc_upper95=float(center+half))


def simulate(observed, roles, *, registration, trials=20000, seed=20261113, effect=.8):
    from study.practice.generators import registry
    from study.practice.ledger import require_registration
    require_registration(registration, classes=roles)
    rng = np.random.default_rng(seed)
    observed_sd = {g: float(np.std(v, ddof=1)) for g, v in observed.items()}
    profiles = {f'homoskedastic-{sd}': {g: sd for g in roles} for sd in (.1, .3, .66, .8, 1.04)}
    profiles['observed-heteroskedastic'] = observed_sd
    profiles['observed-fixed-source-correlated'] = observed_sd
    templates = {}
    for entry in registry():
        if entry['id'] in roles and entry['role'] == 'holdout':
            templates.setdefault(entry['nuisance_template'], []).append(entry['id'])
    rows = []
    for profile, sds in profiles.items():
        noise = {g: rng.normal(0, max(sds[g], .01), (trials, 1 if profile.endswith('correlated') and g in ('gen-008', 'gen-009', 'gen-015') else 4)) for g in roles}
        noise = {g: np.repeat(v, 4, axis=1) if v.shape[1] == 1 else v for g, v in noise.items()}
        scenarios = [('neutral', 0.), ('harmful', -.5), ('helpful', effect), ('card-specific', 0.)]
        scenarios += [(f'mixed-{loss:.1f}', loss) for loss in (-.5, -1., -2.)]
        scenarios += [(f'nuisance-specific/{template}', 0.) for template in templates]
        for name, shift in scenarios:
            draws = {g: v + (shift if roles[g] not in ('calibration-only', 'control') else 0.) for g, v in noise.items()}
            if name == 'card-specific':
                target = next(g for g in sorted(roles) if roles[g] == 'holdout')
                draws[target] = draws[target] + 2.
            elif name.startswith('mixed-'):
                draws = {g: v + (effect if roles[g] == 'holdout' else shift if roles[g] in ('design', 'holdout-class') else 0.) for g, v in noise.items()}
            elif name.startswith('nuisance-specific/'):
                members = templates[name.split('/', 1)[1]]
                draws = {g: v + (effect if g in members else 0.) for g, v in noise.items()}
            for primaries in (1, 8):
                accepted = decisions(draws, roles, other_primaries=[1.] * (primaries - 1))
                target = .8 if name == 'helpful' else .1 if name == 'card-specific' or name.startswith('nuisance-specific/') or name == 'mixed--1.0' else .05 if name in ('neutral', 'harmful', 'mixed--2.0') else None
                bounds = wilson(accepted)
                met = None if target is None else bounds['mc_lower95'] >= target if name == 'helpful' else bounds['mc_upper95'] <= target
                rows.append(dict(profile=profile, scenario=name, primaries=primaries, **bounds,
                                 target=target, target_met=met))
    primary = [r for r in rows if r['primaries'] == 1 and r['target'] is not None]
    return dict(schema='practice-operating/3', seed=seed, trials=trials, effect_se=effect,
                effect_justification='0.8 paired-test-SE is below exposed r2 moons +1.064. This is a stated generic effect target, not a claim that the comparator improves everywhere.',
                observed_instance_sd=observed_sd, rows=rows, targets_met=all(r['target_met'] for r in primary),
                unmet_targets=[r for r in primary if not r['target_met']],
                nuisance_groups=templates, pooled_harm_margin_se=0., finite_source_sd_floors=FINITE_SOURCE_SD_FLOORS,
                mechanical_checks='Condition on valid in-fold, null-claim and resource checks; flags isolate statistical error rates.',
                scope='Exact sign-flip plus modified HK over independent generators. Additional one-sided 15% HK loss-risk screens pooled over design/class and separately within each role; any negative upper 70% interval rejects. Conventional 5% credible-loss tests are retained and labelled separately. The sensitive 15% risk screen was selected in exposed r2-variance construction probes (.05 accepted about 20% of -1 mixed harm; .15 about 6%); this is calibration, not independent validation. A subsequent exposed r2 fully-correlated probe found that the generic 1.04 SD floor understates digits (~4.0); source-specific floors rounded up from prior observed SDs (wine 1.04, digits 4.1, offset 32) restore the -1 mixed-cell acceptance to about 6% with about 88% helper power. These are frozen before this final r3 table. Individual material-loss tests retain Holm control at a -0.5 margin. Pooled harm tests are conservative screens, not a simultaneous equivalence guarantee. Mixed cells help machinery +0.8 and harm all design/class generators; -0.5 is descriptive. Each nuisance cell helps every machinery generator sharing that actual template. One-primary targets assessed with Wilson bounds; eight-primary sensitivity fully disclosed.',
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
    from study.practice.ledger import register
    registration = register(evaluation_id=args.evaluation_id, candidate_digest=args.candidate_digest,
                            classes=sorted(roles), primary_verdict='Monte Carlo construction checks', kind='construction')
    result = simulate(report['comparator_effects_test_se'], roles, registration=registration)
    write_json(args.out, result)
    print(json.dumps(dict(targets_met=result['targets_met'])))


if __name__ == '__main__':
    main()
