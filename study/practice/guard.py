"""Generator-level inference and predeclared no-harm checks (standard library only)."""
import json
import math
import random
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


def random_effects(effects, variances):
    if not effects or len(effects) != len(variances):
        raise ValueError('one finite variance per generator required')
    if any(not math.isfinite(x) for x in [*effects, *variances]) or any(v < 0 for v in variances):
        raise ValueError('invalid effect or variance')
    weights = [1 / max(v, 1e-12) for v in variances]
    total = sum(weights)
    fixed = sum(w * x for w, x in zip(weights, effects, strict=False)) / total
    q = sum(w * (x - fixed) ** 2 for w, x in zip(weights, effects, strict=False))
    c = total - sum(w * w for w in weights) / total
    tau = max(0.0, (q - (len(effects) - 1)) / c) if c > 0 else 0.0
    weights = [1 / (max(v, 1e-12) + tau) for v in variances]
    mean = sum(w * x for w, x in zip(weights, effects, strict=False)) / sum(weights)
    se = math.sqrt(1 / sum(weights))
    return dict(mean=mean, se=se, tau_squared=tau, lower90=mean - 1.6448536269514722 * se,
                upper90=mean + 1.6448536269514722 * se, generators=len(effects))


def holm(pvalues):
    if len(pvalues) > 8 or any(not 0 <= p <= 1 for p in pvalues):
        raise ValueError('at most eight predeclared primary verdicts')
    adjusted, running = [0.0] * len(pvalues), 0.0
    for rank, index in enumerate(sorted(range(len(pvalues)), key=pvalues.__getitem__)):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def summarize(values, *, seed=99127, samples=10000):
    if not values or any(not math.isfinite(x) for x in values):
        raise ValueError('finite instance effects required')
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(samples))
    return dict(mean=statistics.mean(values), variance=statistics.variance(values) / n if n > 1 else 0.0,
                lower90=means[int(samples * 0.05)], upper90=means[min(samples - 1, int(samples * 0.95))], instances=n)


def check_in_fold(trace):
    """Require independently recorded fit-row IDs for every learned preprocessing step."""
    if not trace:
        return False
    for fold in trace:
        training, held = set(fold['train_ids']), set(fold['held_ids'])
        if not training or not held or training & held:
            return False
        for fitted in fold['transform_fit_ids']:
            if not set(fitted) or not set(fitted) <= training or set(fitted) & held:
                return False
    return True


def evaluate(effects, roles, *, epsilon=0.01, trace=None, primary_pvalues=None, null_delta=0.0,
             null_tolerance=0.0, limits_ok=True, declared_class_generators=()):
    if not effects or set(effects) - set(roles):
        raise ValueError('effects require declared generator roles')
    if trace is not None and not check_in_fold(trace):
        return dict(verdict='reject-in-fold', class_claim=False)
    summaries = {key: summarize(values) for key, values in sorted(effects.items())}
    other = [g for g in summaries if roles[g] not in ('design', 'calibration-only', 'control')]
    if any(summaries[g]['lower90'] <= -epsilon for g in summaries if roles[g] != 'calibration-only'):
        verdict = 'reject-no-harm'
    elif null_delta > null_tolerance:
        verdict = 'reject-false-positive-claims'
    elif not limits_ok:
        verdict = 'reject-limits'
    elif not any(summaries[g]['lower90'] > 0 for g in other):
        verdict = 'design-only'
    else:
        verdict = 'no regression on these named generators'
    holdouts = [g for g in other if roles[g] in ('holdout', 'holdout-class')]
    inference = None
    if holdouts:
        inference = random_effects([summaries[g]['mean'] for g in holdouts], [summaries[g]['variance'] for g in holdouts])
        inference['sign_test_p'] = sign_test([summaries[g]['mean'] for g in holdouts])
    adjusted = holm(primary_pvalues or [inference['sign_test_p'] if inference else 1.0])
    class_claim = bool(len(set(declared_class_generators) & set(holdouts)) >= 3
                       and set(holdouts) == set(declared_class_generators)
                       and inference['lower90'] > 0 and adjusted[0] <= 0.05
                       and verdict == 'no regression on these named generators')
    return dict(verdict='accept-class' if class_claim else verdict, class_claim=class_claim,
                generators=summaries, inference=inference, primary_holm_p=adjusted,
                secondary_results='descriptive; generators are the unit of inference')


def claim_metrics(rows, *, practical_bar):
    visible = false = verified = 0
    for row in rows:
        positive = bool(row['visible_claim'])
        gains = row['fresh_gains']
        passed = bool(positive and len(gains) == 3 and all(math.isfinite(x) and x >= practical_bar for x in gains)
                      and row['fresh_process_equal'] and row['export_eligible'] and row.get('scope_ok', True))
        visible += positive
        verified += passed
        false += positive and not passed
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
    roles = {'design': 'design', 'other': 'holdout'}
    clean = [dict(train_ids=[1, 2], held_ids=[3, 4], transform_fit_ids=[[1, 2]])]
    leaking = [dict(train_ids=[1, 2], held_ids=[3, 4], transform_fit_ids=[[1, 2, 3, 4]])]
    return dict(card_specific_patch=evaluate({'design': [0.5] * 4, 'other': [0] * 4}, roles, trace=clean)['verdict'],
                harmful_operator=evaluate({'design': [0.5] * 4, 'other': [-0.5] * 4}, roles, trace=clean)['verdict'],
                screen_before_cv=evaluate({'design': [0.5] * 4, 'other': [0.5] * 4}, roles, trace=leaking)['verdict'])


def register_burn(path, *, source_key, classes, primary_verdict):
    """Idempotent preregistration before holdout scoring; a changed source spends a burn."""
    path = Path(path)
    ledger = json.loads(path.read_text()) if path.exists() else {'burns': []}
    for existing in ledger['burns']:
        if existing['source_key'] == source_key:
            if existing['classes'] != sorted(set(classes)) or existing['primary_verdict'] != primary_verdict:
                raise ValueError('a registered verdict cannot be changed after scoring')
            return ledger
    for name in classes:
        if sum(name in item['classes'] for item in ledger['burns']) >= 2:
            raise ValueError('two-burn limit reached; independent replacement and review required')
    if len(ledger['burns']) >= 8:
        raise ValueError('at most eight primary verdicts per programme')
    ledger['burns'].append(dict(source_key=source_key, classes=sorted(set(classes)), primary_verdict=primary_verdict))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + '\n')
    return ledger
