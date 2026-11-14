"""Run a prospective treatment/build/provider on public or sealed development cards."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from study.common import ENVELOPE, FAMILIES, ROOT, read_json, write_json
from study.eidos_arms import lab
from study.evals.mini_eval import configure_lab
from study.evals.provenance import EVAL_ROOT, controller_record, disk_guard
from study.exp.treatment import resolve
from study.private_card_registry import PROFILES, PoolProfile

PROGRAMME = Path('/Volumes/Mrigesh SSD/eidos-v1/improvement')


def utc():
    return datetime.now(UTC).isoformat()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--treatment')
    selection.add_argument('--pair', nargs=2, metavar='T@REF')
    p.add_argument('--ref', default='HEAD')
    p.add_argument('--provider')
    cards = p.add_mutually_exclusive_group(required=True)
    cards.add_argument('--pool')
    cards.add_argument('--cards')
    p.add_argument('--card-limit', type=int)
    p.add_argument('--replicates', type=int, default=1)
    p.add_argument('--arms', choices=['4', '2,4'], default='4')
    p.add_argument('--concurrency', type=int, default=2)
    p.add_argument('--out', type=Path, required=True)
    for name in ('references', 'autopilot-scorecard', 'budget', 'quota-file'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--payg-approved')
    p.add_argument('--retry-incidents', type=int, default=1)
    p.add_argument('--keep-lab-running', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--smoke', action='store_true')
    p.add_argument('--wall-seconds', type=int)
    return p


def variants(args):
    specs = args.pair or [args.treatment + '@' + args.ref]
    result = []
    for index, spec in enumerate(specs):
        name, sep, ref = spec.partition('@')
        if not sep or not ref:
            raise ValueError('variant must be treatment@ref')
        treatment = resolve(name, provider=args.provider)
        commit = git('rev-parse', '--verify', ref + '^{commit}')
        if subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'HEAD'], cwd=ROOT,
                          capture_output=True).returncode:
            raise ValueError('build must be an ancestor of the controller commit')
        fixture = treatment.investigator.get('runtime') == 'fixture'
        if fixture and args.arms != '4':
            raise ValueError('fixture treatments require arm 4; arm 2 calls a provider')
        if name != 'stub' and args.arms == '2,4':
            treatment.provider.cli_settings()
        result.append(dict(index=index, label=f'v{index + 1}-{name}-{commit[:12]}', commit=commit,
                           treatment=treatment.record(), provider=treatment.provider.record(),
                           stub=name == 'stub', fixture=fixture,
                           lab_root=str(EVAL_ROOT / ('ip-' + commit[:12]))))
    return result


def make_plan(cards, replicates, arms, selected):
    if not cards or len(set(cards)) != len(cards):
        raise ValueError('cards must be nonempty and unique')
    # Stable order, paired variants adjacent for the same card/replicate.
    return [dict(card=card, replicate=k, block_id=f'{card}-r{k}', variant=v['index'], arms=list(arms), order=i)
            for i, (card, k, v) in enumerate(
                ((card, k, v) for card in cards for k in range(1, replicates + 1) for v in selected), 1)]


def output_root(out, *, private_root=None):
    out = Path(out).resolve()
    allowed = [PROGRAMME / 'runs']
    if os.environ.get('EIDOS_EXP_TEST_ROOT'):
        allowed.append(Path(os.environ['EIDOS_EXP_TEST_ROOT']).resolve())
    if private_root is not None:
        allowed = [Path(private_root).resolve() / 'runs']
    if not any(out.is_relative_to(root) and out != root for root in allowed):
        raise ValueError('output must be below the approved runs root')
    return out


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def provider_family(provider):
    return provider.split('-', 1)[0]


def budget_check(args, selected, plan):
    live = [v for v in selected if not v['stub'] and not v['fixture']]
    if not live:
        return dict(estimated_usd=0, live=False)
    if not args.budget:
        raise ValueError('live runs require --budget')
    budget = read_json(args.budget)
    ledger = Path(budget['spend_ledger'])
    if not ledger.is_absolute():
        ledger = args.budget.resolve().parent / ledger
    spent = {}
    for line in ledger.read_text().splitlines():
        row = json.loads(line)
        if row.get('usd') is not None:
            if not _number(row['usd']):
                raise ValueError('invalid spend ledger USD amount')
            family = provider_family(row['provider'])
            spent[family] = spent.get(family, 0) + row['usd']
        elif provider_family(row['provider']) != 'minimax':
            raise ValueError('USD spend ledger has an unknown charge')
    estimates = {}
    quota = None
    for v in live:
        provider = resolve(v['treatment']['id'], provider=v['provider']['id']).provider
        runs = sum(len(b['arms']) for b in plan if b['variant'] == v['index']) * (args.retry_incidents + 1)
        if provider.id.startswith('minimax-'):
            if args.payg_approved:
                if not args.payg_approved.strip():
                    raise ValueError('payg approval must name who, when and scope')
                continue
            if not args.quota_file:
                raise ValueError('MiniMax requires a fresh --quota-file or --payg-approved')
            quota = read_json(args.quota_file)
            age = (datetime.now(UTC) - datetime.fromisoformat(quota['utc'].replace('Z', '+00:00'))).total_seconds()
            if not 0 <= age < 600 or time.time() - args.quota_file.stat().st_mtime >= 600:
                raise ValueError('quota reading must be younger than ten minutes')
            settings = budget['minimax']
            weekly = quota['weekly_remaining_fraction']
            interval = quota['interval_remaining_fraction']
            if any(not _number(x) or x > 1 for x in (weekly, interval)):
                raise ValueError('invalid quota fractions')
            if weekly <= max(.15, settings['weekly_floor']) or interval <= max(.20, settings['interval_floor']):
                raise ValueError('MiniMax remaining quota is below its floor')
            if 1 - weekly >= min(.50, settings['weekly_share']):
                raise ValueError('MiniMax weekly share exhausted')
        else:
            limit = provider.model_usd_per_run
            if limit is None:
                raise ValueError('USD provider requires a per-run worst-case limit')
            family = provider_family(provider.id)
            estimates[family] = estimates.get(family, 0) + runs * limit
    for provider, estimate in estimates.items():
        caps = [cap for name, cap in budget['providers'].items() if provider_family(name) == provider]
        if not caps:
            raise ValueError('missing provider cap')
        cap = dict(usd_cap=min(c['usd_cap'] for c in caps), stop_fraction=min(c['stop_fraction'] for c in caps))
        if (not _number(cap['usd_cap']) or not _number(cap['stop_fraction'])
                or not 0 < cap['stop_fraction'] <= .8):
            raise ValueError('invalid provider cap/stop fraction')
        if spent.get(provider, 0) + estimate >= min(20, cap['usd_cap']) * cap['stop_fraction']:
            raise ValueError('provider spend plus worst-case plan reaches the stop cap')
    return dict(live=True, estimated_usd=sum(estimates.values()), estimates=estimates,
                spent=spent, spend_ledger=str(ledger), quota_before=quota, payg_approved=args.payg_approved)


def preflight(args, selected, plan, *, private_root=None):
    disk_guard()
    out = output_root(args.out, private_root=private_root)
    active = PROGRAMME / 'PILOT_ACTIVE'
    if os.environ.get('EIDOS_EXP_TEST_ROOT'):
        active = Path(os.environ['EIDOS_EXP_TEST_ROOT']) / 'PILOT_ACTIVE'
    if active.exists():
        raise ValueError('PILOT_ACTIVE: programme runs are disabled')
    if len(selected) == 2 and selected[0]['lab_root'] == selected[1]['lab_root'] and args.concurrency < 2:
        raise ValueError('paired blocks sharing a lab require concurrency at least two')
    if args.wall_seconds is not None and (not args.smoke or args.wall_seconds <= 0):
        raise ValueError('--wall-seconds requires --smoke and a positive duration')
    if (args.replicates < 1 or args.concurrency < 1 or args.retry_incidents < 0
            or (args.card_limit is not None and args.card_limit < 1)):
        raise ValueError('replicates, concurrency and card limit must be positive; retries nonnegative')
    if out.exists() and any(out.iterdir()):
        raise ValueError('output directory must be new or empty')
    test_stub = bool(os.environ.get('EIDOS_EXP_TEST_ROOT')) and all(v['stub'] for v in selected)
    controller = controller_record(ROOT, live=not test_stub)
    return dict(output=str(out), controller=controller, budget=budget_check(args, selected, plan))


def explicit_lab(home):
    if home is None or not Path(home).is_absolute():
        raise ValueError('runner refuses to start a lab without an explicit absolute home')
    os.environ['EIDOS_STUDY_HOME'] = str(home)


@contextlib.contextmanager
def environment(updates):
    old = dict(os.environ)
    os.environ.update(updates)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def ensure_lab(v, *, dry_run=False):
    root = Path(v['lab_root'])
    test_root = os.environ.get('EIDOS_EXP_TEST_ROOT')
    if test_root and v['stub']:
        root = Path(test_root) / 'labs' / ('ip-' + v['commit'][:12])
        python = root / 'venv/bin/python'
        if not dry_run:
            python.parent.mkdir(parents=True, exist_ok=True)
            if not python.exists():
                python.symlink_to(sys.executable)
            write_json(root / 'venv/pilot-build.json', dict(eidos_commit_sha=v['commit'], python=str(python),
                                                         test_fixture=True))
    else:
        python = root / 'venv/bin/python'
        if not root.exists():
            if dry_run:
                return dict(home=str(root / 'home'), python=str(python), build_required=True)
            subprocess.run(['nice', '-n', '10', 'bash', str(ROOT / 'study/evals/build_lab.sh'),
                            v['commit'], root.name], cwd=ROOT, check=True)
    home = root / 'home'
    if dry_run and not python.exists():
        return dict(home=str(home), python=str(python), build_required=True)
    explicit_lab(home)
    receipt = configure_lab(home, python, test_root is not None and v['stub'], v['commit'])
    return dict(home=str(home), python=str(python), receipt=receipt,
                env={key: os.environ[key] for key in ('EIDOS_STUDY_HOME', 'EIDOS_STUDY_ROOT', 'EIDOS_STUDY_PYTHON',
                                                     'EIDOS_STUDY_MCP_PORT') if key in os.environ})


def pool_selection(args):
    profile = PROFILES.get('pool:' + args.pool) if args.pool else None
    if args.pool and not isinstance(profile, PoolProfile):
        raise ValueError('unknown pool')
    cards = list(profile.cards) if profile else args.cards.split(',')
    if args.card_limit:
        cards = cards[:args.card_limit]
    if any(card not in FAMILIES for card in cards):
        raise ValueError('unknown card')
    return profile, cards


def load_references(args, profile, cards):
    from study.exp.scorecard import canonical, number, sha256

    if profile:
        root = profile.pool_root()
        references = {card: read_json(root / 'controller' / card / 'reference.json') for card in cards}
    else:
        references = read_json(args.references or ROOT / 'experiments/pools/legacy-references.json')
        references = {card: references[card] for card in cards}
    for ref in references.values():
        if (not all(number(ref.get(k)) for k in ('headroom', 'err_base', 'err_ref', 'se_paired', 'base_score'))
                or ref['headroom'] < 0 or (ref['headroom'] == 0 and ref.get('null_card') is not True)
                or ref['err_base'] < 0 or ref['err_ref'] < 0 or type(ref.get('null_card')) is not bool):
            raise ValueError('invalid card reference block')
    return references, sha256(canonical(references))


def descriptive_pair(a, b):
    """Single adapter for the scorecard team's descriptive compare API."""
    from study.exp.compare import compatible, paired_stats
    from study.exp.scorecard import METRICS
    if a.get('smoke') or b.get('smoke'):
        return 'Smoke connectivity run; scientific comparison refused.\n'
    compatible(a, b)
    def identity(row):
        return row['card_id'], row['arm'], row['replicate']
    old = {identity(row): row for row in a['individual']}
    pairs = [(old[identity(row)], row) for row in b['individual']]
    # compare(aa=True) currently requires identical treatment/build, so reuse its
    # descriptive statistics directly. Never synthesize equal variant identities.
    stats = {metric: paired_stats(pairs, metric) for metric in METRICS}
    return ('# Paired development description\n\nNo decision; apply a registered rule separately.\n\n'
            + '```json\n' + json.dumps(stats, indent=2, allow_nan=False) + '\n```\n')


def block_worker(request):
    # Separate processes isolate treatment/provider/build environments for concurrent pairs.
    from study.grade_block import grade_block
    from study.launcher import run_block

    item, args = request['block'], request['options']
    if args['wall_seconds'] is not None:
        ENVELOPE['wall_s'] = args['wall_seconds']
    rows = [dict(family=FAMILIES[item['card']], task=item['card'], run=item['replicate'], arm=arm,
                 primary_eligible=True, block_id=item['block_id'], block_order=item['order'], randomization_seed=0)
            for arm in item['arms']]
    root = Path(request['root'])
    attempts = []
    for attempt in range(args['retry_incidents'] + 1):
        try:
            run_block(rows, root / 'runs', stub=request['stub'], replace_incident=attempt > 0)
            grades = grade_block(root / 'runs' / item['block_id'] / 'block_record.json',
                                 root / 'grading', root / 'results.json')
            incident = any(row['status'] == 'incident' for row in grades)
        except Exception as exc:
            incident = True
            print('block attempt failed: ' + type(exc).__name__, flush=True)
        attempts.append(dict(attempt=attempt + 1, incident=incident))
        if not incident:
            break
    write_json(root / 'attempts' / (item['block_id'] + '.json'), attempts)
    return int(incident)


def account(args, budget, roots, selected, run_id):
    if not budget['live']:
        return
    ledger = Path(budget['spend_ledger'])
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        # Include every archived incident attempt, not just the final scorecard rows.
        for v, root in zip(selected, roots, strict=True):
            if v['stub'] or v['fixture']:
                continue
            provider = resolve(v['treatment']['id'], provider=v['provider']['id']).provider
            for path in sorted((root / 'runs').rglob('run_record.json')):
                record = read_json(path)
                tokens = record.get('tokens') or {}
                usd = record.get('billed_usd')
                if (usd is None and provider.price_per_mtok is not None
                        and all(type(tokens.get(k)) is int for k in ('input', 'output'))):
                    usd = (tokens.get('input', 0) * provider.price_per_mtok['input'] +
                           tokens.get('output', 0) * provider.price_per_mtok['output']) / 1e6
                stream.write(json.dumps(dict(utc=utc(), provider=provider.id, model=provider.model,
                                             purpose='development evidence', run_id=run_id,
                                             variant=v['label'], tokens_in=tokens.get('input'),
                                             tokens_out=tokens.get('output'), usd=usd, source='study.exp.run',
                                             quota_before=budget.get('quota_before'),
                                             quota_after=read_json(args.quota_file) if args.quota_file else None)) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def execute(args, *, private_root=None):
    started = time.monotonic()
    selected = variants(args)
    resolved_at = time.monotonic()
    profile, cards = pool_selection(args)
    arms = tuple(map(int, args.arms.split(',')))
    plan = make_plan(cards, args.replicates, arms, selected)
    checked = preflight(args, selected, plan, private_root=private_root)
    references, reference_digest = load_references(args, profile, cards)
    out = Path(checked['output'])
    manifest = dict(schema='eidos-experiment-run/1', started_utc=utc(), plan=plan, variants=selected,
                    preflight=checked, controller_commit=checked['controller']['git_sha'], smoke=args.smoke,
                    references_sha256=reference_digest,
                    exit_status=1, status='preflight', labs=[],
                    timings=dict(resolve_s=resolved_at - started, preflight_s=time.monotonic() - resolved_at))
    if args.dry_run:
        with environment({}):
            manifest['labs'] = [ensure_lab(v, dry_run=True) for v in selected]
        manifest.update(status='dry-run', exit_status=0)
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / 'manifest.json', manifest)
        (out / 'run.log').write_text('Resolve; preflight passed; dry-run: no lab or block started.\n')
        print(json.dumps(manifest, indent=2))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    roots = [out / v['label'] if len(selected) > 1 else out for v in selected]
    for root in roots:
        root.mkdir(exist_ok=True)
    write_json(out / 'manifest.json', manifest)
    status = 1
    with (out / 'run.log').open('w', buffering=1) as log:
        def event(message):
            log.write(utc() + ' ' + message + '\n')
        event('Resolve and preflight passed')
        try:
            lab_started = time.monotonic()
            with environment({}):
                for v in selected:
                    event('Resolve lab ' + v['label'])
                    v['lab'] = ensure_lab(v)
                    manifest['labs'].append(v['lab'])
            charter = 'experiments/CHARTER.md'
            charter_commit = git('log', '-1', '--format=%H', '--', charter)
            if not charter_commit:
                raise ValueError('standing charter must be committed before a run')
            if git('status', '--porcelain', '--', charter):
                raise ValueError('standing charter differs from its pinned commit')
            manifest['charter'] = dict(path=charter, commit=charter_commit)
            safe_pools = json.dumps([p.public_manifest() for p in PROFILES.values() if isinstance(p, PoolProfile)])
            # Start each distinct build once, always with an explicit home and selected provider.
            started_labs = {}
            try:
                for v in selected:
                    if v['stub']:
                        continue
                    key = v['lab']['home']
                    if key in started_labs:
                        if started_labs[key]['fixture'] != v['fixture']:
                            raise ValueError('cannot mix fixture and credentialled runtime on one lab')
                        continue
                    env = dict(v['lab']['env'], EIDOS_STUDY_TREATMENT=v['treatment']['id'],
                               EIDOS_STUDY_PROVIDER=v['provider']['id'], EIDOS_STUDY_POOL_PROFILES=safe_pools)
                    started_labs[key] = v  # stop even when startup partially fails
                    with environment(env):
                        explicit_lab(Path(key))
                        lab.start(v['provider']['id'], credentials=not v['fixture'])
                    event('Lab started ' + v['label'])
                manifest['timings']['lab_s'] = time.monotonic() - lab_started
                run_started = time.monotonic()
                locks = {v['lab']['home']: threading.Semaphore(args.concurrency) for v in selected}

                def dispatch(item, barrier=None):
                    v = selected[item['variant']]
                    root = roots[item['variant']]
                    request = dict(block=item, root=str(root), stub=v['stub'],
                                   options=dict(wall_seconds=args.wall_seconds, retry_incidents=args.retry_incidents))
                    path = root / 'requests' / (item['block_id'] + '.json')
                    write_json(path, request)
                    env = dict(os.environ, **v['lab']['env'], EIDOS_STUDY_TREATMENT=v['treatment']['id'],
                               EIDOS_STUDY_PROVIDER=v['provider']['id'], EIDOS_PILOT_CHARTER_PATH=charter,
                               EIDOS_PILOT_CHARTER_COMMIT=charter_commit, EIDOS_STUDY_POOL_PROFILES=safe_pools)
                    env.pop('EIDOS_CARD_PROFILE', None)
                    with locks[v['lab']['home']]:
                        if barrier is not None:
                            barrier.wait(timeout=30)
                        event('Run/grade ' + v['label'] + ' ' + item['block_id'])
                        with path.with_suffix('.log').open('w') as block_log:
                            result = subprocess.run([sys.executable, '-m', 'study.exp.run', '--_block', str(path)],
                                                    cwd=ROOT, env=env, stdout=block_log, stderr=block_log)
                        event('Finished ' + v['label'] + ' ' + item['block_id'] + f' exit={result.returncode}')
                    return result.returncode

                if len(selected) == 2:
                    per_lab = {key: sum(v['lab']['home'] == key for v in selected) for key in locks}
                    slots = min(args.concurrency // count for count in per_lab.values())
                    if slots < 1:
                        raise ValueError('paired blocks require concurrency at least two when sharing a lab')

                    def dispatch_pair(items):
                        barrier = threading.Barrier(2)
                        with ThreadPoolExecutor(max_workers=2) as pair_executor:
                            return list(pair_executor.map(lambda item: dispatch(item, barrier), items))

                    with ThreadPoolExecutor(max_workers=slots) as executor:
                        paired_exits = executor.map(dispatch_pair, [plan[i:i + 2] for i in range(0, len(plan), 2)])
                        exits = [code for pair in paired_exits for code in pair]
                else:
                    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                        exits = list(executor.map(dispatch, plan))
            finally:
                stop_errors = []
                if not args.keep_lab_running:
                    for v in started_labs.values():
                        try:
                            with environment(v['lab']['env']):
                                lab.stop()
                            event('Lab stopped ' + v['label'])
                        except Exception as exc:
                            stop_errors.append(type(exc).__name__)
                            event('Lab stop failed ' + v['label'] + ': ' + type(exc).__name__)
                    if stop_errors:
                        raise RuntimeError('lab shutdown was not confirmed: ' + ', '.join(stop_errors))
            manifest['timings']['run_grade_s'] = time.monotonic() - run_started
            score_started = time.monotonic()
            from study.exp.scorecard import build, markdown
            scoring_pool = None
            if profile:
                scoring_pool = out / 'score-inputs'
                write_json(scoring_pool / 'pool.json', dict(pool_id=profile.pool, commitment=profile.commitment()))
            scores = []
            for v, root in zip(selected, roots, strict=True):
                event('Score ' + v['label'])
                score = build(root, pool_root=scoring_pool, references=references, treatment=v['treatment'], provider=v['provider'],
                              build_record=dict(commit=v['commit']), replicates=args.replicates,
                              autopilot=args.autopilot_scorecard)
                if profile:
                    score['pool'] = dict(id=profile.pool, commitment=profile.commitment())
                score['smoke'] = args.smoke
                if args.smoke:
                    # Existing compare rejects this distinct connectivity-only schema.
                    score['schema'] = 'eidos-exp-smoke-scorecard/1'
                if args.payg_approved:
                    score['payg_approved'] = args.payg_approved
                write_json(root / 'scorecard.json', score)
                (root / 'scorecard.md').write_text(markdown(score))
                scores.append(score)
            if len(scores) == 2:
                (out / 'paired.md').write_text(descriptive_pair(*scores))
            grades = [row for root in roots for row in read_json(root / 'results.json')]
            expected = len(plan) * len(arms)
            status = int(any(exits) or len(grades) != expected or any(r['status'] == 'incident' for r in grades))
            manifest['status'] = 'complete' if status == 0 else 'unresolved incident'
            manifest['timings']['score_s'] = time.monotonic() - score_started
        except Exception as exc:
            status = 2
            manifest.update(status='failed', error_type=type(exc).__name__)
            event('Failed: ' + type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            try:
                account_started = time.monotonic()
                account(args, checked['budget'], roots, selected, out.name)
                manifest['timings']['account_s'] = time.monotonic() - account_started
                event('Account complete')
            except Exception as exc:
                status = 2
                manifest.update(status='accounting failed', error_type=type(exc).__name__)
                raise
            finally:
                manifest['timings']['total_s'] = time.monotonic() - started
                manifest.update(exit_status=status, ended_utc=utc())
                write_json(out / 'manifest.json', manifest)
    return status


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == '--_heldout':
        request = read_json(argv[1])
        return execute(parser().parse_args(request['argv']), private_root=request['private_root'])
    if argv and argv[0] == '--_block':
        return block_worker(read_json(argv[1]))
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('experiment runner failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
