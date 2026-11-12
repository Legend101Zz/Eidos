"""Generator-level inference and predeclared harm checks; research dependencies loaded lazily."""
import json
import math
import os
import re
import statistics
from pathlib import Path


def normalized_gain(candidate, baseline, reference, reference_se):
    denominator = max(reference - baseline, 2 * reference_se, 1e-12)
    return (candidate - baseline) / denominator


def sign_test(effects):
    values = [x for x in effects if x != 0]
    n, wins = len(values), sum(x > 0 for x in values)
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n if n else 1.0


def sign_flip_pvalues(effects, variances):
    """Exact conditional sign randomisation, one weighted mean per generator.

    Weights depend only on within-generator variances (invariant to sign flips).
    Under independent symmetric neutral effects this controls size conditionally;
    one arbitrary non-null generator can at most double the nominal size.
    """
    import numpy as np
    means = np.atleast_2d(np.asarray(effects, dtype=float))
    variances = np.atleast_2d(np.asarray(variances, dtype=float))
    k = means.shape[1]
    if means.shape != variances.shape or not 2 <= k <= 16:
        raise ValueError('exact sign randomisation requires 2..16 generators and matching variances')
    if not np.isfinite(means).all() or not np.isfinite(variances).all() or (variances < 0).any():
        raise ValueError('finite effects and nonnegative variances required')
    floor = np.maximum(.01, np.median(variances, axis=1) * .1)
    weights = 1 / np.maximum(variances, floor[:, None])
    coefficients = means * weights
    signs = ((np.arange(2**k)[:, None] >> np.arange(k)) & 1) * 2. - 1.
    pvalues = np.empty(len(means))
    for start in range(0, len(means), 128):
        block = coefficients[start:start + 128]
        observed = block.sum(axis=1)
        tolerance = 1e-12 * np.maximum(1., np.abs(observed))
        pvalues[start:start + len(block)] = np.mean(block @ signs.T >= (observed - tolerance)[:, None], axis=1)
    return pvalues


def random_effects(effects, variances):
    """DL heterogeneity with modified Hartung–Knapp t uncertainty, generators as units."""
    from scipy.stats import t
    if len(effects) < 2 or len(effects) != len(variances):
        raise ValueError('at least two generators with one variance each required')
    if any(not math.isfinite(x) for x in [*effects, *variances]) or any(v < 0 for v in variances):
        raise ValueError('invalid effect or variance')
    # Prevent a zero or four-observation variance estimate receiving infinite precision.
    floor = max(.01, statistics.median(variances) * .1)
    variances = [max(v, floor) for v in variances]
    weights = [1 / v for v in variances]
    total = sum(weights)
    fixed = sum(w * x for w, x in zip(weights, effects, strict=True)) / total
    q = sum(w * (x - fixed) ** 2 for w, x in zip(weights, effects, strict=True))
    c = total - sum(w * w for w in weights) / total
    tau = max(0., (q - len(effects) + 1) / c) if c > 0 else 0.
    weights = [1 / (v + tau) for v in variances]
    mean = sum(w * x for w, x in zip(weights, effects, strict=True)) / sum(weights)
    hk = max(1., sum(w * (x - mean) ** 2 for w, x in zip(weights, effects, strict=True)) / (len(effects) - 1))
    se = math.sqrt(hk / sum(weights))
    critical = float(t.ppf(.95, len(effects) - 1))
    return dict(mean=mean, se=se, tau_squared=tau, lower90=mean - critical * se,
                upper90=mean + critical * se, generators=len(effects), variance_floor=floor,
                method='modified Hartung-Knapp; t(k-1)')


def holm(pvalues):
    if len(pvalues) > 8 or any(not 0 <= p <= 1 for p in pvalues):
        raise ValueError('at most eight predeclared primary verdicts')
    adjusted, running = [0.0] * len(pvalues), 0.0
    for rank, index in enumerate(sorted(range(len(pvalues)), key=pvalues.__getitem__)):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def summarize(values, *, effective_instances=None):
    from scipy.stats import t
    if len(values) < 2 or any(not math.isfinite(x) for x in values):
        raise ValueError('at least two finite instance effects required')
    n = len(values)
    effective = n if effective_instances is None else effective_instances
    if not 1 <= effective <= n:
        raise ValueError('effective instance count outside observed sample')
    variance = statistics.variance(values) / effective
    # Re-splits cannot estimate between-source precision from one fixed dataset.
    # Retain at least the worst observed moderate instance SD, even if all
    # correlated repeats happen to agree exactly.
    if effective == 1:
        variance = max(variance, 1.04 ** 2)
    df = max(1, effective - 1)
    margin = float(t.ppf(.95, df)) * math.sqrt(variance)
    mean = statistics.mean(values)
    return dict(mean=mean, variance=variance, lower90=mean - margin, upper90=mean + margin, instances=n, effective_instances=effective, df=df)


def check_fit_trace(trace):
    """Supplemental ID boundary check; not evidence of actual transform behaviour."""
    return bool(trace) and all(
        set(fold['train_ids']) and set(fold['held_ids'])
        and not set(fold['train_ids']) & set(fold['held_ids'])
        and all(set(fitted) and set(fitted) <= set(fold['train_ids']) for fitted in fold['transform_fit_ids'])
        for fold in trace)


def check_in_fold(factory, x, y, train, held, *, fitted=None):
    """Poison held rows on the actual fit path, then compare fitted pipeline state.

    A factory must include every pre-CV operation. Neither estimator-supplied IDs nor
    a trace dict can certify this check. Scores and labels of test data are not used.
    """
    import pickle

    import numpy as np
    clean = fitted if fitted is not None else factory(x, y, train)
    poisoned_x, poisoned_y = x.copy(), y.copy()
    rng = np.random.default_rng(32941)
    poisoned_x[held] = rng.normal(10, 3, poisoned_x[held].shape)
    poisoned_y[held] = rng.permutation(poisoned_y[held])
    poisoned = factory(poisoned_x, poisoned_y, train)
    return pickle.dumps(clean, protocol=5) == pickle.dumps(poisoned, protocol=5)


def evaluate(effects, roles, *, epsilon=.5, in_fold_ok=None, primary_pvalues=None, null_delta=None,
             null_tolerance=None, limits_ok=None, declared_class_generators=(), effective_instances=None):
    from scipy.stats import t
    if not effects or set(effects) != set(roles):
        raise ValueError('complete effects require declared generator roles')
    if in_fold_ok is not True:
        return dict(verdict='reject-in-fold', class_claim=False)
    if null_delta is None or null_tolerance is None or limits_ok is not True:
        return dict(verdict='reject-missing-or-failed-checks', class_claim=False)
    if not all(math.isfinite(x) for x in (epsilon, null_delta, null_tolerance)) or epsilon < 0 or null_tolerance < 0:
        raise ValueError('finite nonnegative margins required')
    summaries = {key: summarize(values, effective_instances=(effective_instances or {}).get(key)) for key, values in sorted(effects.items())}
    named = [g for g in summaries if roles[g] not in ('calibration-only', 'control')]
    # Familywise harm detection (not an equivalence claim for each generator).
    harm_p = []
    for g in named:
        summary = summaries[g]
        se = math.sqrt(max(summary['variance'], .01))
        harm_p.append(float(t.cdf((summary['mean'] + epsilon) / se, summary['df'])))
    harm = holm_unlimited(harm_p)
    declared = set(declared_class_generators)
    if declared and (not declared <= set(named) or any(roles[g] != 'holdout-class' for g in declared)):
        raise ValueError('class inference may contain only explicitly declared holdout-class generators')
    holdouts = sorted(declared) if declared else [g for g in summaries if roles[g] == 'holdout']
    inference = None
    p = 1.
    if len(holdouts) >= 2:
        inference = random_effects([summaries[g]['mean'] for g in holdouts], [summaries[g]['variance'] for g in holdouts])
        inference['sign_test_p'] = sign_test([summaries[g]['mean'] for g in holdouts])
        p = inference['sign_flip_p'] = float(sign_flip_pvalues(
            [summaries[g]['mean'] for g in holdouts], [summaries[g]['variance'] for g in holdouts])[0])
    # Caller supplies the other preregistered programme primaries; current p cannot be overridden.
    adjusted = holm([p, *(primary_pvalues or [])])
    if null_delta > null_tolerance:
        verdict = 'reject-false-positive-claims'
    elif any(value <= .05 for value in harm):
        verdict = 'reject-no-harm'
    elif not inference or len(holdouts) < 5 or adjusted[0] > .05 or inference['lower90'] <= 0 or inference['mean'] < epsilon:
        verdict = 'reject-insufficient-transfer'
    else:
        verdict = 'accept-class' if declared else 'accept-named'
    return dict(verdict=verdict, class_claim=verdict == 'accept-class', generators=summaries,
                inference=inference, primary_holm_p=adjusted, harm_holm_p=dict(zip(named, harm, strict=True)),
                inference_generators=holdouts, minimum_pooled_gain=epsilon,
                limitation='Named acceptance is positive transfer plus no detected material harm; not per-generator equivalence.',
                secondary_results='descriptive; generators are the unit of inference')


def holm_unlimited(pvalues):
    adjusted, running = [0.] * len(pvalues), 0.
    for rank, index in enumerate(sorted(range(len(pvalues)), key=pvalues.__getitem__)):
        running = max(running, min(1., (len(pvalues) - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def claim_metrics(rows, *, practical_bar, null=False):
    """Use the grader itself, including D12's any-positive-claim null branch."""
    from study.grade_block import success_rule
    visible = false = verified = 0
    for row in rows:
        positive = bool(row['visible_claim'])
        gains = row['fresh_gains']
        complete = len(gains) == 3 and all(math.isfinite(x) for x in gains)
        graded = success_rule(
            {'id': 'D12' if null else 'practice-improvement', 'min_reference_gain': practical_bar},
            {'status': 'improvement_claimed' if positive else 'no_improvement_found',
             'claim_type': 'checkpoint_improvement' if positive else 'no_positive_claim'},
            {'gain': min(gains)} if complete else None,
            reproduces=complete and row['fresh_process_equal'], guardrails=row['export_eligible'],
            scope_ok=row.get('scope_ok', True))
        visible += positive
        verified += graded['verified_success']
        false += graded['false_positive_claim']
    n = len(rows)
    if not n:
        raise ValueError('claim denominator cannot be empty')
    return dict(instances=n, visible_claim_rate=visible / n, false_positive_claim_rate=false / n,
                verified_success_rate=verified / n)


def identifier_violations(paths, identifiers):
    pattern = re.compile(r'(?<![A-Za-z0-9_])(?:' + '|'.join(re.escape(x) for x in sorted(identifiers)) + r')(?![A-Za-z0-9_])')
    found = []
    for path in sorted(map(Path, paths)):
        if path.is_file() and path.suffix in ('.py', '.md', '.json', '.txt', '.toml'):
            for line, text in enumerate(path.read_text().splitlines(), 1):
                for match in pattern.finditer(text):
                    found.append((str(path), line, match.group()))
    return found


def guard_selftest():
    """Noisy effect controls; the end-to-end screen control is in controls.py."""
    import numpy as np
    rng = np.random.default_rng(91873)
    roles = {f'g{i}': 'holdout' for i in range(12)}
    noise = {g: rng.normal(0, .8, 4).tolist() for g in roles}
    options = dict(in_fold_ok=True, null_delta=0., null_tolerance=0., limits_ok=True)
    scenarios = {name: {g: [v + shift for v in values] for g, values in noise.items()}
                 for name, shift in [('helpful_operator', .8), ('neutral_operator', 0.), ('harmful_operator', -.5)]}
    scenarios['card_specific_patch'] = {g: [v + (2. if g == 'g0' else 0.) for v in values] for g, values in noise.items()}
    return {name: evaluate(effects, roles, **options)['verdict'] for name, effects in scenarios.items()}


def register_burn(path, *, evaluation_id, classes, primary_verdict, candidate_digest):
    """Locked, protected preregistration keyed to the evaluation, not harness edits."""
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        content = stream.read()
        ledger = json.loads(content) if content else {'burns': []}
        for existing in ledger['burns']:
            if existing['evaluation_id'] == evaluation_id:
                if (existing['classes'] != sorted(set(classes)) or existing['primary_verdict'] != primary_verdict
                        or existing['candidate_digest'] != candidate_digest):
                    raise ValueError('a registered verdict cannot be changed after scoring')
                return ledger
        for name in classes:
            if sum(name in item['classes'] for item in ledger['burns']) >= 2:
                raise ValueError('two-burn limit reached; independent replacement and review required')
        if len(ledger['burns']) >= 8:
            raise ValueError('at most eight primary verdicts per programme')
        ledger['burns'].append(dict(evaluation_id=evaluation_id, candidate_digest=candidate_digest,
                                   classes=sorted(set(classes)), primary_verdict=primary_verdict))
        stream.seek(0)
        stream.write(json.dumps(ledger, indent=2, sort_keys=True) + '\n')
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        return ledger
