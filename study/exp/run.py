"""Run a prospective treatment/build/provider on public or sealed development cards."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from study.common import ENVELOPE, FAMILIES, ROOT, read_json, write_json
from study.eidos_arms import lab
from study.evals.mini_eval import configure_lab
from study.evals.provenance import EVAL_ROOT, controller_record, disk_guard
from study.exp.treatment import resolve
from study.private_card_registry import (
    ALL_PRIVATE_CARDS,
    PROFILES,
    SEAL_ROOT_ENVS,
    PoolProfile,
    load_pool_manifest,
    profile_for_card,
)

PROGRAMME = Path('/Volumes/Mrigesh SSD/eidos-v1/improvement')


def utc():
    return datetime.now(UTC).isoformat()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, stderr=subprocess.PIPE).strip()


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return value


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError('must be nonnegative')
    return value


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
    p.add_argument('--card-limit', type=positive)
    p.add_argument('--replicates', type=positive, default=1)
    p.add_argument('--arms', choices=['4', '2,4'], default='4')
    p.add_argument('--concurrency', type=positive, default=2)
    p.add_argument('--out', type=Path, required=True)
    for name in ('references', 'autopilot-scorecard', 'budget', 'quota-file'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--payg-approved')
    p.add_argument('--retry-incidents', type=nonnegative, default=1)
    p.add_argument('--keep-lab-running', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--smoke', action='store_true')
    p.add_argument('--wall-seconds', type=positive)
    p.add_argument('--tier', choices=['T0', 'T1', 'T2', 'T3'])
    p.add_argument('--pool-role', choices=['legacy-development', 'D', 'E1', 'E2', 'E3', 'H', 'evaluation_rotation'])
    p.add_argument('--look', choices=['initial', 'extension'])
    p.add_argument('--mode', choices=['live', 'autopilot', 'stub', 'fixture', 'smoke'])
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
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError('invalid ledger provider')
    return provider.lower().split('-', 1)[0]


def quota_time(quota):
    value = datetime.fromisoformat(quota['utc'].replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('quota UTC must include a timezone')
    return value


def read_quota(path):
    path = Path(path)
    if path.is_dir():
        readings = [read_quota(file) for file in path.glob('quota-*.json')]
        if not readings:
            raise ValueError('quota directory has no quota-*.json readings')
        return max(readings, key=lambda q: (quota_time(q), q['source']))
    raw = read_json(path)
    if 'observed_utc' in raw:
        windows = [w for w in raw['model_windows'] if w['model_name'] == 'general']
        if len(windows) != 1:
            raise ValueError('quota requires exactly one general model window')
        window = windows[0]
        percentages = [window[k] for k in ('current_weekly_remaining_percent', 'current_interval_remaining_percent')]
        if any(not _number(v) or v > 100 for v in percentages):
            raise ValueError('invalid quota percentages')
        quota = dict(utc=raw['observed_utc'], weekly_remaining_fraction=percentages[0] / 100,
                     interval_remaining_fraction=percentages[1] / 100)
    else:
        quota = dict(raw)
    quota_time(quota)
    if any(not _number(quota[k]) or quota[k] > 1
           for k in ('weekly_remaining_fraction', 'interval_remaining_fraction')):
        raise ValueError('invalid quota fractions')
    quota['source'] = str(path.resolve())
    return quota


def quota_stale(quota):
    age = (datetime.now(UTC) - quota_time(quota)).total_seconds()
    return not 0 <= age < 600 or time.time() - Path(quota['source']).stat().st_mtime >= 600


class QuotaFloorRefused(ValueError):
    def __init__(self, quota, stale):
        super().__init__('MiniMax remaining quota is below its floor')
        self.quota = quota
        self.stale = stale


def budget_check(args, selected, plan, *, admission=True):
    live = [v for v in selected if not v['stub'] and not v['fixture']]
    if not live:
        return dict(estimated_usd=0, live=False)
    if not args.budget:
        raise ValueError('live runs require --budget')
    budget = read_json(args.budget)
    ledger = Path(budget['spend_ledger'])
    if not ledger.is_absolute():
        ledger = args.budget.resolve().parent / ledger
    for cap in budget['providers'].values():
        if (not _number(cap['usd_cap']) or not _number(cap['stop_fraction'])
                or not 0 < cap['stop_fraction'] <= .8):
            raise ValueError('invalid provider cap/stop fraction')
    settings = budget['minimax']
    if any(not _number(settings[key]) or not 0 < settings[key] <= 1
           for key in ('weekly_floor', 'interval_floor', 'weekly_share')):
        raise ValueError('invalid MiniMax budget fraction')
    spent = {}
    live_families = {provider_family(v['provider']['id']) for v in live}
    for line in ledger.read_text().splitlines():
        row = json.loads(line)
        if 'provider' not in row or 'usd' not in row:
            raise ValueError('spend ledger requires provider and usd')
        if row.get('usd') is not None:
            if not _number(row['usd']):
                raise ValueError('invalid spend ledger USD amount')
            family = provider_family(row['provider'])
            spent[family] = spent.get(family, 0) + row['usd']
        elif provider_family(row['provider']) != 'minimax' and provider_family(row['provider']) in live_families:
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
            quota = read_quota(args.quota_file)
            stale = quota_stale(quota)
            if admission and stale:
                raise ValueError('quota reading must be younger than ten minutes')
            settings = budget['minimax']
            weekly = quota['weekly_remaining_fraction']
            interval = quota['interval_remaining_fraction']
            if any(not _number(x) or x > 1 for x in (weekly, interval)):
                raise ValueError('invalid quota fractions')
            if weekly < max(.15, settings['weekly_floor']) or interval < max(.20, settings['interval_floor']):
                raise QuotaFloorRefused(quota, stale)
            if admission and 1 - weekly >= min(.50, settings['weekly_share']):
                raise ValueError('MiniMax weekly share exhausted')
        else:
            limit = provider.model_usd_per_run
            if not _number(limit):
                raise ValueError('USD provider requires a per-run worst-case limit')
            family = provider_family(provider.id)
            estimates[family] = estimates.get(family, 0) + runs * limit
    for provider, estimate in estimates.items():
        caps = [cap for name, cap in budget['providers'].items() if provider_family(name) == provider]
        if not caps:
            raise ValueError('missing provider cap')
        cap = dict(usd_cap=min(c['usd_cap'] for c in caps), stop_fraction=min(c['stop_fraction'] for c in caps))
        if spent.get(provider, 0) + estimate >= min(20, cap['usd_cap']) * cap['stop_fraction']:
            raise ValueError('provider spend plus worst-case plan reaches the stop cap')
    return dict(live=True, estimated_usd=sum(estimates.values()), estimates=estimates,
                spent=spent, spend_ledger=str(ledger), quota_before=quota,
                quota_stale=quota_stale(quota) if quota is not None else False, payg_approved=args.payg_approved)


def preflight(args, selected, plan, *, private_root=None):
    disk_guard()
    out = output_root(args.out, private_root=private_root)
    active = PROGRAMME / 'PILOT_ACTIVE'
    if os.environ.get('EIDOS_EXP_TEST_ROOT'):
        active = Path(os.environ['EIDOS_EXP_TEST_ROOT']) / 'PILOT_ACTIVE'
    if active.exists() or (any(not v['stub'] and not v['fixture'] for v in selected)
                           and (PROGRAMME / 'PILOT_ACTIVE').exists()):
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


def ensure_lab(v, *, dry_run=False, private_root=None):
    root = Path(v['lab_root'])
    test_root = os.environ.get('EIDOS_EXP_TEST_ROOT')
    if test_root and v['stub']:
        root = Path(private_root or test_root) / 'labs' / ('ip-' + v['commit'][:12])
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
            build_env = {key: value for key, value in os.environ.items() if key not in (*SEAL_ROOT_ENVS, 'EIDOS_STUDY_POOL_PROFILES')
                         and not key.endswith(('_API_KEY', '_AUTH_TOKEN', '_OAUTH_TOKEN'))}
            subprocess.run(['nice', '-n', '10', 'bash', str(ROOT / 'study/evals/build_lab.sh'),
                            v['commit'], root.name], cwd=ROOT, env=build_env, check=True)
    home = root / 'home'
    if dry_run and not python.exists():
        return dict(home=str(home), python=str(python), build_required=True)
    explicit_lab(home)
    receipt = configure_lab(home, python, test_root is not None and v['stub'], v['commit'])
    if private_root is not None and not (test_root and v['stub']):
        # Verify the shared immutable build first; service state uses a separate private HOME.
        ports = read_json(root / 'ports.json')
        home = Path(private_root) / 'labs' / root.name / 'home'
        explicit_lab(home)
        os.environ['EIDOS_STUDY_ROOT'] = str(home.parent)
        if not (home / 'config/lab.json').exists():
            init_args = ['init', '--home', home, '--api-port', ports['api'], '--pg-port', ports['pg'],
                         '--temporal-port', ports['temporal']]
            source = os.environ.get('EIDOS_STUDY_TEMPORAL_SOURCE')
            if not source and (ROOT / 'a8lab/services/temporal/bin/temporal-server').exists():
                source = str(ROOT / 'a8lab/services/temporal/bin')
            if source:
                init_args += ['--temporal-source', source]
            lab.cli(*init_args, timeout=600)
        if read_json(home / 'config/lab.json').get('lab_python') != str(python):
            raise ValueError('private lab interpreter differs from verified build')
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
    if not args.pool and any(card in ALL_PRIVATE_CARDS and profile_for_card(card).startswith('pilot-')
                             for card in cards):
        raise ValueError('sealed Pilot cards are not programme development cards')
    if any(card not in FAMILIES for card in cards):
        raise ValueError('unknown card')
    return profile, cards


def load_references(args, profile, cards):
    from study.exp.scorecard import canonical, number, sha256

    if not profile and 'D03' in cards and args.references is None:
        raise ValueError('D03 requires an explicit --references file; reference not established')
    if profile:
        root = profile.pool_root()
        references = {card: read_json(root / 'controller' / card / 'reference.json') for card in cards}
    else:
        references = read_json(args.references or ROOT / 'experiments/pools/legacy-references.json')
        references = {card: references[card] for card in cards}
    for ref in references.values():
        if ref.get('reference_established') is False:
            raise ValueError('card reference not established; supply an explicit --references file')
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


def redact_grading_paths(value, grading):
    if isinstance(value, dict):
        return {k: redact_grading_paths(v, grading) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_grading_paths(v, grading) for v in value]
    if isinstance(value, str) and str(grading) in value:
        return 'private-grading-sha256:' + hashlib.sha256(value.encode()).hexdigest()
    return value


def merge_results(path, grades):
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = read_json(path) if path.exists() else []
        def identity(row):
            return tuple(row[k] for k in ('family', 'task', 'run', 'arm'))
        keys = {identity(row) for row in grades}
        write_json(path, [r for r in rows if identity(r) not in keys] + grades)


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
    grading = root / 'grading'
    private_grading = request.get('private_grading')
    if private_grading and not Path(private_grading).is_absolute():
        private_grading = str(Path(os.environ['EIDOS_IMP_PRIVATE_ROOT']) / private_grading)
    if private_grading:
        grading = Path(private_grading)
    attempts = []
    for attempt in range(args['retry_incidents'] + 1):
        try:
            run_block(rows, root / 'runs', stub=request['stub'], replace_incident=attempt > 0)
            # Controller context is distinct from the build evidence produced by a run.
            if 2 in item['arms'] and request.get('build_commit'):
                path = root / 'runs' / item['block_id'] / 'arm2/run_record.json'
                record = read_json(path)
                record['build_commit_asserted_by_controller'] = request['build_commit']
                write_json(path, record)
            grades = grade_block(root / 'runs' / item['block_id'] / 'block_record.json',
                                 grading, (grading / item['block_id'] / 'results.json') if private_grading else root / 'results.json')
            if private_grading:
                grades = redact_grading_paths(grades, Path(private_grading))
                merge_results(root / 'results.json', grades)
                for arm, summary in zip(item['arms'], grades, strict=True):
                    write_json(root / 'grading' / item['block_id'] / f'arm{arm}/grade.json', summary)
            incident = any(row['status'] == 'incident' for row in grades)
        except RunTerminated:
            raise
        except Exception as exc:
            incident = True
            print('block attempt failed: ' + type(exc).__name__, flush=True)
        attempts.append(dict(attempt=attempt + 1, incident=incident))
        if not incident:
            break
    write_json(root / 'attempts' / (item['block_id'] + '.json'), attempts)
    return int(incident)


@contextlib.contextmanager
def spend_lock(budget):
    ledger = Path(budget['spend_ledger'])
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield ledger


def write_ledger(ledger, rows):
    temporary = ledger.with_suffix('.tmp')
    with temporary.open('w') as stream:
        for row in rows:
            stream.write(json.dumps(row, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(ledger)


def reserve_spend(args, budget, selected, plan, item, reservations, *, private_root=None, private_out=None):
    if not budget['live']:
        return
    with spend_lock(budget) as ledger:
        v = selected[item['variant']]
        if v['stub'] or v['fixture']:
            return
        provider = resolve(v['treatment']['id'], provider=v['provider']['id']).provider
        reserved_plan = plan if private_root is not None and not reservations else [item]
        # The existing H reservation already covers this plan's USD exposure.
        check_plan = [] if private_root is not None and reservations else reserved_plan
        current = budget_check(args, selected, check_plan, admission=False)
        if private_root is not None and private_out is not None:
            # Per-dispatch reservations and their ordering never enter the shared H ledger.
            destination = Path(private_out) / 'spend.jsonl'
            destination.parent.mkdir(parents=True, exist_ok=True)
            private_rows = [json.loads(line) for line in destination.read_text().splitlines()] if destination.exists() else []
            limit = provider.model_usd_per_run
            private_rows.append(dict(status='reserved', provider=provider.id, variant=v['label'], block=item['block_id'],
                                     usd=len(item['arms']) * (args.retry_incidents + 1) * limit if _number(limit) else None))
            write_ledger(destination, private_rows)
        if private_root is not None and reservations:
            return current  # One aggregate covers the complete H plan before any live dispatch.
        amount = current['estimated_usd']
        key = uuid.uuid4().hex
        row = dict(provider=provider.id, usd=amount if provider_family(provider.id) != 'minimax' else None,
                   status='reserved', reservation=key, source='study.exp.run')
        if private_root is None:
            row['expected_records'] = sum(len(block['arms']) for block in reserved_plan)
        if private_root is not None:
            row['pool'] = 'held-out'
        else:
            row.update(utc=utc(), block=item['block_id'], variant=v['label'])
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        write_ledger(ledger, [*rows, row])
        reservations.append(key)
        if private_root is not None:
            budget['_heldout_expected'] = sum(len(block['arms']) for block in plan
                                              if not selected[block['variant']]['stub'] and not selected[block['variant']]['fixture'])
        return current


def account(args, budget, roots, selected, run_id, *, private_root=None, out=None, reservations=(), status='complete'):
    destination = Path(out) if out is not None else args.out
    if private_root is not None and not destination.resolve().is_relative_to(Path(private_root).resolve()):
        raise ValueError('held-out usage must remain under the private root')
    try:
        quota_after = read_quota(args.quota_file) if args.quota_file and args.quota_file.exists() else None
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        # An invalid refresh cannot erase already incurred charges or usage.
        quota_after = None
    quota_before = budget.get('quota_before')
    quota_percent = None
    if quota_before is not None and quota_after is not None:
        before, after = (q['weekly_remaining_fraction'] for q in (quota_before, quota_after))
        if (not quota_stale(quota_after) and quota_time(quota_after) > quota_time(quota_before)
                and all(_number(x) and x <= 1 for x in (before, after)) and after <= before):
            quota_percent = (before - after) * 100
    detail = []
    attempts = []
    per_variant = {}
    for v, root in zip(selected, roots, strict=True):
        model_free = v['stub'] or v['fixture']
        provider = resolve(v['treatment']['id'], provider=v['provider']['id']).provider
        variant_attempts = []
        for path in sorted((root / 'runs').rglob('run_record.json')):
            record = read_json(path)
            tokens = record.get('tokens') or {}
            usd = 0 if model_free else record.get('billed_usd')
            if (usd is None and provider.price_per_mtok is not None
                    and all(type(tokens.get(k)) is int for k in ('input', 'output'))):
                usd = (tokens['input'] * provider.price_per_mtok['input'] +
                       tokens['output'] * provider.price_per_mtok['output']) / 1e6
            variant_attempts.append(dict(usd=usd, wall_s=record.get('wall_s')))
            if model_free:
                continue
            detail.append(dict(utc=utc(), provider=provider.id, model=provider.model,
                               purpose='development evidence', run_id=run_id, variant=v['label'],
                               block=record.get('block_id', path.parent.parent.name),
                               current=path.relative_to(root / 'runs').parts[0] != 'incidents',
                               tokens_in=tokens.get('input'), tokens_out=tokens.get('output'), usd=usd,
                               source='study.exp.run', quota_before=budget.get('quota_before'),
                               quota_after=quota_after))
        attempts.extend(variant_attempts)
        per_variant[v['label']] = dict(runs=len(variant_attempts), usd=_sum_known(a['usd'] for a in variant_attempts))
    write_json(destination / 'usage.json', dict(
        schema='eidos-exp-usage/1', status=status, complete=status == 'complete', runs=len(attempts), usd=_sum_known(a['usd'] for a in attempts),
        quota_weekly_percent=quota_percent, time_seconds=_sum_known(a['wall_s'] for a in attempts),
        per_variant=per_variant))
    if not budget['live']:
        return
    if private_root is not None:
        destination = Path(out) / 'spend.jsonl'
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_ledger(destination, detail)
        providers = {row['provider'] for row in detail} or {
            v['provider']['id'] for v in selected if not v['stub'] and not v['fixture']}
        if len(providers) != 1:
            raise ValueError('held-out accounting requires one provider')
        public = [dict(provider=next(iter(providers)), pool='held-out', n_runs=len(detail),
                       tokens_in=sum(row['tokens_in'] for row in detail) if all(type(row['tokens_in']) is int for row in detail) else None,
                       tokens_out=sum(row['tokens_out'] for row in detail) if all(type(row['tokens_out']) is int for row in detail) else None,
                       usd=sum(row['usd'] for row in detail) if all(row['usd'] is not None for row in detail) else None,
                       status='settled', source='study.exp.run')]
    else:
        public = detail
    with spend_lock(budget) as ledger:
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        reserved = [row for row in rows if row.get('reservation') in reservations]
        if private_root is not None:
            if reservations and sum(row['current'] for row in detail) < budget.get('_heldout_expected', 1):
                public[0].update(usd=None, status='uncertain')
            rows = [row for row in rows if row.get('reservation') not in reservations]
        else:
            for row in reserved:
                observed = [entry for entry in detail if entry['variant'] == row['variant']
                            and entry['block'] == row['block']]
                if sum(entry['current'] for entry in observed) >= row['expected_records']:
                    row.update(usd=0, status='settled')
                else:
                    # Keep the unobserved worst case charged; a missing receipt cannot release budget.
                    charge = sum(entry['usd'] or 0 for entry in observed)
                    row.update(usd=max(0, row['usd'] - charge) if row['usd'] is not None else None,
                               status='uncertain')
        write_ledger(ledger, [*rows, *public])


def _sum_known(values):
    values = list(values)
    return sum(values) if all(_number(value) for value in values) else None


class RunTerminated(RuntimeError):
    pass


def dispatch_process(command, *, cancel, **kwargs):
    if cancel.is_set():
        raise RunTerminated('dispatch cancelled')
    with subprocess.Popen(command, **kwargs) as process:
        while True:
            if cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise RunTerminated('dispatch terminated')
            try:
                return subprocess.CompletedProcess(command, process.wait(timeout=.2))
            except subprocess.TimeoutExpired:
                continue


@contextlib.contextmanager
def termination_handlers(cancel=None):
    def terminate(signum, frame):
        if cancel is not None and cancel.is_set():
            return
        if cancel is not None:
            cancel.set()
        raise RunTerminated('termination requested: ' + signal.Signals(signum).name)
    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def ignore_cleanup_signals():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)


def execute(args, *, private_root=None):
    started = time.monotonic()
    selected = variants(args)
    resolved_at = time.monotonic()
    profile, cards = pool_selection(args)
    arms = tuple(map(int, args.arms.split(',')))
    plan = make_plan(cards, args.replicates, arms, selected)
    checked = preflight(args, selected, plan, private_root=private_root)
    if args.quota_file and checked['budget'].get('quota_before') is None:
        checked['budget']['quota_before'] = read_quota(args.quota_file)
    if profile is not None and profile.pool != 'D' and not (private_root or os.environ.get('EIDOS_IMP_PRIVATE_ROOT')):
        raise ValueError('non-D grading requires EIDOS_IMP_PRIVATE_ROOT')
    references, reference_digest = load_references(args, profile, cards)
    out = Path(checked['output'])
    manifest = dict(schema='eidos-experiment-run/1', started_utc=utc(), plan=plan, variants=selected,
                    preflight=checked, controller_commit=checked['controller']['git_sha'], smoke=args.smoke,
                    references_sha256=reference_digest,
                    exit_status=1, status='preflight', labs=[],
                    timings=dict(resolve_s=resolved_at - started, preflight_s=time.monotonic() - resolved_at))
    manifest.update(tier=args.tier, pool_role=args.pool_role, look=args.look, mode=args.mode,
                    scorecards=[], score_errors=[], paired={}, usage='../usage.json' if private_root is not None else 'usage.json')
    if args.dry_run:
        with environment({}):
            manifest['labs'] = [ensure_lab(v, dry_run=True) for v in selected]
        manifest.update(status='dry-run', exit_status=0)
        print(json.dumps(manifest, indent=2))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    roots = [out / v['label'] if len(selected) > 1 else out for v in selected]
    for root in roots:
        root.mkdir(exist_ok=True)
    write_json(out / 'manifest.json', manifest)
    status = 1
    reservations = []
    grading_id = uuid.uuid4().hex
    cancel = threading.Event()
    dispatch_stopped = threading.Event()
    dispatch_lock = threading.Lock()
    manifest['dispatch_checks'] = []
    with termination_handlers(cancel), (out / 'run.log').open('w', buffering=1) as log:
        def event(message):
            log.write(utc() + ' ' + message + '\n')
        event('Resolve and preflight passed')
        try:
            lab_started = time.monotonic()
            with environment({}):
                for v in selected:
                    event('Resolve lab ' + v['label'])
                    v['lab'] = ensure_lab(v, private_root=private_root) if private_root is not None else ensure_lab(v)
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
                               EIDOS_STUDY_PROVIDER=v['provider']['id'])
                    started_labs[key] = v  # stop even when startup partially fails
                    with environment(env):
                        os.environ.pop('EIDOS_STUDY_POOL_PROFILES', None)
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
                                   build_commit=v['commit'],
                                   options=dict(wall_seconds=args.wall_seconds, retry_incidents=args.retry_incidents))
                    if profile is not None and profile.pool != 'D':
                        private = private_root or os.environ.get('EIDOS_IMP_PRIVATE_ROOT')
                        if not private:
                            raise ValueError('non-D grading requires EIDOS_IMP_PRIVATE_ROOT')
                        request['private_grading'] = str((out / 'grading-scratch' if private_root is not None
                                                           else Path('grading') / grading_id) / v['label'])
                    if private_root is not None:
                        request['pool_profiles'] = json.loads(safe_pools)
                    path = root / 'requests' / (item['block_id'] + '.json')
                    write_json(path, request)
                    env = dict(os.environ, **v['lab']['env'], EIDOS_STUDY_TREATMENT=v['treatment']['id'],
                               EIDOS_STUDY_PROVIDER=v['provider']['id'], EIDOS_PILOT_CHARTER_PATH=charter,
                               EIDOS_PILOT_CHARTER_COMMIT=charter_commit)
                    if private_root is None:
                        env['EIDOS_STUDY_POOL_PROFILES'] = safe_pools
                    else:
                        env.pop('EIDOS_STUDY_POOL_PROFILES', None)
                    env.pop('EIDOS_CARD_PROFILE', None)
                    with locks[v['lab']['home']]:
                        if barrier is not None:
                            barrier.wait(timeout=30)
                        with dispatch_lock:
                            if cancel.is_set():
                                raise RunTerminated('dispatch cancelled')
                            if dispatch_stopped.is_set():
                                return 1
                            try:
                                current = reserve_spend(args, checked['budget'], selected, plan, item, reservations,
                                                        private_root=private_root,
                                                        private_out=out.parent if private_root is not None else None)
                            except ValueError as exc:
                                if isinstance(exc, QuotaFloorRefused):
                                    manifest['dispatch_checks'].append(dict(variant=v['label'], block=item['block_id'],
                                                                            quota=exc.quota, refused=True))
                                    if exc.stale:
                                        event('Warning: stale quota reading at dispatch; remaining floors checked')
                                dispatch_stopped.set()
                                manifest['dispatch_stop_reason'] = str(exc)
                                event('Dispatch stopped: ' + str(exc))
                                return 1
                            if current is not None:
                                manifest['dispatch_checks'].append(dict(variant=v['label'], block=item['block_id'],
                                                                        quota=current.get('quota_before')))
                                if current.get('quota_stale'):
                                    event('Warning: stale quota reading at dispatch; remaining floors checked')
                            manifest['status'] = 'started'
                            write_json(out / 'manifest.json', manifest)
                        event('Run/grade ' + v['label'] + ' ' + item['block_id'])
                        with path.with_suffix('.log').open('w') as block_log:
                            result = dispatch_process([sys.executable, '-m', 'study.exp.run', '--_block', str(path)],
                                                      cancel=cancel, cwd=ROOT, env=env, stdout=block_log, stderr=block_log)
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
                ignore_cleanup_signals()
                stop_errors = []
                if not args.keep_lab_running or cancel.is_set():
                    for v in started_labs.values():
                        try:
                            with environment(v['lab']['env']):
                                explicit_lab(Path(v['lab']['home']))
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
            scores = {arm: [] for arm in arms}
            for v, root in zip(selected, roots, strict=True):
                if not (root / 'results.json').exists():
                    continue
                for arm in arms:
                    event('Score ' + v['label'] + f' arm{arm}')
                    try:
                        score = build(root, pool_root=scoring_pool, references=references, treatment=v['treatment'], provider=v['provider'],
                                      build_record=dict(commit=v['commit']), replicates=args.replicates, arm=arm,
                                      autopilot=args.autopilot_scorecard, tier=args.tier, pool_role=args.pool_role,
                                      look=args.look, mode=args.mode)
                    except ValueError as exc:
                        manifest['score_errors'].append(dict(variant=v['label'], arm=arm, reason=str(exc)))
                        event('Score refused ' + v['label'] + f' arm{arm}: ' + str(exc))
                        continue
                    if profile:
                        score['pool'] = dict(id=profile.pool, commitment=profile.commitment())
                    if dispatch_stopped.is_set():
                        score['incomplete'] = True
                        score['dispatch_stop_reason'] = manifest['dispatch_stop_reason']
                    score['smoke'] = args.smoke
                    if args.smoke:
                        # Existing compare rejects this distinct connectivity-only schema.
                        score['schema'] = 'eidos-exp-smoke-scorecard/1'
                    if args.payg_approved:
                        score['payg_approved'] = args.payg_approved
                    score_root = root / f'arm{arm}'
                    write_json(score_root / 'scorecard.json', score)
                    rendered = markdown(score)
                    (score_root / 'scorecard.md').write_text(rendered)
                    entry = dict(variant=v['label'], arm=arm,
                                 json=str((score_root / 'scorecard.json').relative_to(out)),
                                 markdown=str((score_root / 'scorecard.md').relative_to(out)))
                    if len(arms) == 1:
                        write_json(root / 'scorecard.json', score)
                        (root / 'scorecard.md').write_text(rendered)
                        entry['compatibility_json'] = str((root / 'scorecard.json').relative_to(out))
                    manifest['scorecards'].append(entry)
                    scores[arm].append(score)
            if len(selected) == 2:
                descriptions = []
                for arm, pair in scores.items():
                    if len(pair) != 2:
                        continue
                    description = ('Dispatch stopped; incomplete pair, no comparison.\n'
                                   if dispatch_stopped.is_set() else descriptive_pair(*pair))
                    target = out / f'arm{arm}/paired.md'
                    target.parent.mkdir(exist_ok=True)
                    target.write_text(description)
                    manifest['paired'][str(arm)] = str(target.relative_to(out))
                    descriptions.append(f'## Arm {arm}\n\n' + description)
                (out / 'paired.md').write_text('\n'.join(descriptions))
            grades = [row for root in roots if (root / 'results.json').exists() for row in read_json(root / 'results.json')]
            expected = len(plan) * len(arms)
            status = int(any(exits) or len(grades) != expected or any(r['status'] == 'incident' for r in grades))
            if manifest['score_errors']:
                status = 2
                manifest['status'] = 'scoring failed'
            elif dispatch_stopped.is_set():
                manifest['status'] = 'dispatch stopped'
            else:
                manifest['status'] = 'complete' if status == 0 else 'unresolved incident'
            manifest['timings']['score_s'] = time.monotonic() - score_started
        except Exception as exc:
            status = 2
            manifest.update(status='failed', error_type=type(exc).__name__)
            event('Failed: ' + type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            ignore_cleanup_signals()
            try:
                account_started = time.monotonic()
                account(args, checked['budget'], roots, selected, out.name, private_root=private_root,
                        out=out.parent if private_root is not None else out, reservations=reservations,
                        status=manifest['status'])
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
        args = parser().parse_args(request['argv'])
        try:
            return execute(args, private_root=request['private_root'])
        except Exception:
            if not (args.out / 'manifest.json').exists():
                write_json(args.out / 'manifest.json', dict(status='preflight', exit_status=2))
            raise
    if argv and argv[0] == '--_block':
        request = read_json(argv[1])
        for data in request.get('pool_profiles', []):
            if 'pool:' + data['pool'] not in PROFILES:
                load_pool_manifest(data, PROFILES)
                for item in data['cards']:
                    FAMILIES[item['id']] = item['id']
        with termination_handlers():
            return block_worker(request)
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = 'reference or input file unavailable' if isinstance(exc, FileNotFoundError) else str(exc)
        print('experiment runner failed: ' + detail, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
