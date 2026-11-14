"""Blind, limited held-out consultation; only a coarse receipt leaves private scratch."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def rounded(value):
    return round(round(float(value) / .05) * .05, 2) if value is not None else None


def novelty_total(score):
    wins = 0
    for row in score['individual']:
        observed = row.get('novelty_win')
        if type(observed) is bool:
            wins += int(observed)
        elif row.get('verified') is False:
            continue  # A novelty win requires independent verification.
        elif row.get('novelty', {}).get('class') in {'catalog', 'unknown'}:
            continue  # These classes receive zero novelty credit by definition.
        else:
            return None
    return wins


def receipt(scores, *, utc, note, consultation, threshold):
    from study.exp.stats import signflip_p

    a, b = scores
    if a.get('smoke') or b.get('smoke') or a.get('incomplete') or b.get('incomplete'):
        raise ValueError('incomplete held-out evidence')
    if a['pool'] != b['pool'] or a['references_sha256'] != b['references_sha256']:
        raise ValueError('held-out pair differs in pool/references')
    if set(a['per_card']) != set(b['per_card']):
        raise ValueError('held-out pair differs in roster')
    deltas = [b['per_card'][card]['vrg'] - row['vrg'] for card, row in a['per_card'].items()
              if not row['null_card'] and row['vrg'] is not None and b['per_card'][card]['vrg'] is not None]
    if not deltas:
        raise ValueError('held-out pair has no primary cards')
    mean = sum(deltas) / len(deltas)
    aggregates = [score['aggregate'] for score in scores]
    return dict(utc=utc, note=note, consultation=f'{consultation} of 3', threshold=threshold,
                variants=[dict(treatment=s['treatment']['id'], commit=s['build']['commit']) for s in scores],
                n_cards=len(a['per_card']), **{'pass': mean >= threshold and signflip_p(deltas) <= .10},
                mean_delta_vrg=rounded(mean), verified_rate=[rounded(s['verified_rate']) for s in aggregates],
                false_claims=[s['false_claims'] for s in aggregates], dead_ends=[s['dead_ends'] for s in aggregates],
                novelty_wins=[novelty_total(score) for score in scores],
                tokens=[s['tokens']['total'] for s in aggregates], usd=[s['usd'] for s in aggregates])


def consult(args):
    if not args.note.strip() or not math.isfinite(args.threshold) or args.threshold < 0:
        raise ValueError('note and finite nonnegative threshold required')
    private = args.private_root
    if private is None:
        raise ValueError('private root is required')
    private = private.resolve(strict=True)
    # Reject unsafe caller prose as well as unsafe generated fields.
    manifest = json.loads((private / 'H/pool.json').read_text())
    card_ids = [c.get('id', c.get('card_id')) for c in manifest['cards']]
    if str(private) in args.note or any(card in args.note for card in card_ids):
        raise ValueError('note contains private identifiers')
    counter = private / 'consultations.json'
    with (private / '.consultations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        count = json.loads(counter.read_text())['count'] if counter.exists() else 0
        if type(count) is not int or count < 0 or count >= 3:
            raise ValueError('consultation limit')
        count += 1
        # Reserve a consultation before starting; failed attempts also consume one.
        temporary = counter.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(count=count)) + '\n')
        temporary.replace(counter)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    scratch = private / 'runs' / stamp
    scratch.mkdir(parents=True, mode=0o700)
    out = scratch / 'pair'
    argv = ['--pair', *args.pair, '--pool', 'H', '--out', str(out)]
    for name in ('provider', 'budget', 'quota_file', 'payg_approved'):
        value = getattr(args, name)
        if value is not None:
            argv.extend(['--' + name.replace('_', '-'), str(value)])
    request = scratch / 'request.json'
    request.write_text(json.dumps(dict(argv=argv, private_root=str(private), threshold=args.threshold, note=args.note)))
    env = dict(os.environ)
    env['EIDOS_EXTRA_POOL_MANIFESTS'] = os.pathsep.join(filter(None, [
        env.get('EIDOS_EXTRA_POOL_MANIFESTS'), str(private / 'H/pool.json')]))
    env['EIDOS_POOLS_ROOT'] = os.pathsep.join(filter(None, [env.get('EIDOS_POOLS_ROOT'), str(private)]))
    # OS-level redirection also captures native CLI/build output and tracebacks.
    with (scratch / 'controller.log').open('w') as stream:
        process = subprocess.run([sys.executable, '-m', 'study.exp.run', '--_heldout', str(request)],
                                 env=env, stdout=stream, stderr=stream)
    if process.returncode:
        raise RuntimeError('private run failed')
    run_manifest = json.loads((out / 'manifest.json').read_text())
    scores = [json.loads((out / v['label'] / 'scorecard.json').read_text()) for v in run_manifest['variants']]
    result = receipt(scores, utc=stamp, note=args.note, consultation=count, threshold=args.threshold)
    encoded = json.dumps(result, allow_nan=False)
    if str(private) in encoded or any(card in encoded for card in card_ids):
        raise ValueError('unsafe receipt')
    receipts = private.parent / 'receipts'
    receipts.mkdir(exist_ok=True)
    with (receipts / (stamp + '.json')).open('x') as stream:
        stream.write(encoded + '\n')
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pair', nargs=2, required=True)
    p.add_argument('--threshold', type=float, required=True)
    p.add_argument('--note', required=True)
    p.add_argument('--private-root', type=Path, default=os.environ.get('EIDOS_IMP_PRIVATE_ROOT'))
    p.add_argument('--provider')
    for name in ('budget', 'quota-file'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--payg-approved')
    # Argument errors may contain caller paths: suppress argparse diagnostics too.
    try:
        with open(os.devnull, 'w') as null:
            import contextlib
            with contextlib.redirect_stderr(null):
                args = p.parse_args(argv)
        result = consult(args)
        print(json.dumps(result, allow_nan=False))
        return 0
    except BaseException as exc:
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        print(type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
