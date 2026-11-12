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
from study.practice.guard import guard_selftest, sign_flip_pvalues


def decisions(draws, roles, *, epsilon=.5, other_primaries=()):
    keys = sorted(draws)
    values = np.stack([draws[g] for g in keys], axis=1)
    means, variances = values.mean(axis=2), values.var(axis=2, ddof=1) / values.shape[2]
    effective = np.array([1 if g in ('gen-008', 'gen-009', 'gen-015') else values.shape[2] for g in keys])
    variances *= values.shape[2] / effective[None, :]
    variances[:, effective == 1] = np.maximum(variances[:, effective == 1], 1.04 ** 2)
    dfs = np.maximum(1, effective - 1)
    named = [i for i, g in enumerate(keys) if roles[g] not in ('calibration-only', 'control')]
    holdouts = [i for i, g in enumerate(keys) if roles[g] == 'holdout']
    if len(holdouts) < 5:
        return np.zeros(len(values), dtype=bool)
    harm = t.cdf((means[:, named] + epsilon) / np.sqrt(np.maximum(variances[:, named], .01)), dfs[named])
    harm = np.sort(harm, axis=1) * np.arange(len(named), 0, -1)
    no_harm = harm[:, 0] > .05
    mu, var = means[:, holdouts], variances[:, holdouts]
    floor = np.maximum(.01, np.median(var, axis=1) * .1)
    var = np.maximum(var, floor[:, None])
    w = 1 / var
    total = w.sum(axis=1)
    fixed = (w * mu).sum(axis=1) / total
    q = (w * (mu - fixed[:, None]) ** 2).sum(axis=1)
    c = total - (w * w).sum(axis=1) / total
    tau = np.maximum(0, (q - len(holdouts) + 1) / c)
    w = 1 / (var + tau[:, None])
    pooled = (w * mu).sum(axis=1) / w.sum(axis=1)
    hk = np.maximum(1, (w * (mu - pooled[:, None]) ** 2).sum(axis=1) / (len(holdouts) - 1))
    lower = pooled - t.ppf(.95, len(holdouts) - 1) * np.sqrt(hk / w.sum(axis=1))
    p = sign_flip_pvalues(mu, variances[:, holdouts])
    # Conservative Bonferroni is exact Holm for a current p smaller than all
    # equally/higher preregistered p values; programme family rows use p_other=1.
    adjusted = np.minimum(1., p * (len(other_primaries) + 1))
    return no_harm & (lower > 0) & (pooled >= epsilon) & (adjusted <= .05)


def simulate(observed, roles, *, trials=20000, seed=20261113, effect=.8):
    rng = np.random.default_rng(seed)
    observed_sd = {g: float(np.std(v, ddof=1)) for g, v in observed.items()}
    profiles = {f'homoskedastic-{sd}': {g: sd for g in roles} for sd in (.1, .3, .66, .8, 1.04)}
    profiles['observed-heteroskedastic'] = observed_sd
    # Extra conservative stress: old fixed-source instances share all their noise.
    rows = []
    for profile, sds in profiles.items():
        noise = {g: rng.normal(0, max(sds[g], .01), (trials, 4)) for g in roles}
        for name, shift in [('neutral', 0.), ('harmful', -.5), ('helpful', effect), ('card-specific', 0.)]:
            draws = {g: v + (shift if roles[g] not in ('calibration-only', 'control') else 0.) for g, v in noise.items()}
            if name == 'card-specific':
                # Stronger than a design-only patch: help one held-out generator.
                target = next(g for g in sorted(roles) if roles[g] == 'holdout')
                draws[target] = draws[target] + 2.
            for primaries in (1, 8):
                accepted = decisions(draws, roles, other_primaries=[1.] * (primaries - 1))
                rate = float(accepted.mean())
                # Wilson two-sided 95% MC interval.
                z, n = 1.959963984540054, trials
                center = (rate + z*z/(2*n)) / (1+z*z/n)
                half = z*np.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/(1+z*z/n)
                rows.append(dict(profile=profile, scenario=name, primaries=primaries, trials=trials,
                                 accepted=int(accepted.sum()), acceptance_rate=rate, mc_lower95=float(center-half),
                                 mc_upper95=float(center+half)))
    # Fully correlated wine/digits instances are a separate observed-structure stress.
    noise = {g: rng.normal(0, max(observed_sd[g], .01), (trials, 1 if g in ('gen-008', 'gen-009', 'gen-015') else 4)) for g in roles}
    noise = {g: np.repeat(v, 4, axis=1) if v.shape[1] == 1 else v for g, v in noise.items()}
    for name, shift in [('neutral', 0.), ('harmful', -.5), ('helpful', effect), ('card-specific', 0.)]:
        draws = {g: v + (shift if roles[g] not in ('calibration-only', 'control') else 0.) for g, v in noise.items()}
        if name == 'card-specific':
            target = next(g for g in sorted(roles) if roles[g] == 'holdout')
            draws[target] = draws[target] + 2.
        accepted = decisions(draws, roles)
        rate = float(accepted.mean())
        z, n = 1.959963984540054, trials
        center = (rate + z*z/(2*n)) / (1+z*z/n)
        half = z*np.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/(1+z*z/n)
        rows.append(dict(profile='observed-fixed-source-correlated', scenario=name, primaries=1, trials=trials,
                         accepted=int(accepted.sum()), acceptance_rate=rate, mc_lower95=float(center-half), mc_upper95=float(center+half)))
    primary = [r for r in rows if r['primaries'] == 1]
    passed = all((r['mc_lower95'] >= .8 if r['scenario'] == 'helpful' else
                  r['mc_upper95'] <= (.1 if r['scenario'] == 'card-specific' else .05)) for r in primary)
    return dict(schema='practice-operating/2', seed=seed, trials=trials, effect_se=effect,
                effect_justification='0.8 paired-test-SE is below r2 moons gen-001 measured +1.064 (original marginal-SE estimate +0.896). This is a modest generic target grounded in a non-dev mechanism, not the much larger N04 calibration gap.',
                observed_instance_sd=observed_sd, rows=rows, targets_met=passed,
                mechanical_checks='Condition on valid in-fold, null-claim and resource checks; true flags isolate the statistical rule rather than obtaining false-rejection success from missing evidence.',
                scope='Exact conditional sign-flip test + modified HK; under independent symmetric neutral generators, false acceptance <= alpha and one arbitrary generator <= 2 alpha (5% / 10%). Single preregistered primary is powered; eight-primary rows disclose multiplicity cost. Normal Monte Carlo with measured SD; fixed-source dependence stress included.',
                controls=guard_selftest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.family).read_text())
    roles = {r['generator']: r['role'] for r in report['cards']}
    result = simulate(report['comparator_effects_test_se'], roles)
    write_json(args.out, result)
    print(json.dumps(dict(targets_met=result['targets_met'])))


if __name__ == '__main__':
    main()
