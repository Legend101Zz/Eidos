"""Public experiment registration and append-only evidence chains (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

TYPES = {'register', 'result', 'review', 'decision', 'incident', 'exposure', 'budget', 'note', 'close'}
KINDS = {'performance', 'enabling', 'infra', 'measurement'}
FILES = ('HYPOTHESIS.md', 'plan.json', 'rule.json')
SECTIONS = ('Title', 'Mechanism and evidence', 'Change', 'Prediction', 'Falsifier',
            'Risks to the two invariants', 'Exposure')
EXP = re.compile(r'^(EXP-(\d{3,}))-[a-z0-9]+(?:-[a-z0-9]+)*$')


class LedgerError(ValueError):
    """A precise, user-facing integrity or registration error."""


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    if result.returncode:
        raise LedgerError(f"git {' '.join(args)}: {result.stderr.decode().strip()}")
    return result.stdout


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_bytes())


def require(condition: bool, message: str):
    if not condition:
        raise LedgerError(message)


def read_chain(path: Path, *, index: bool = False) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    require(not raw or raw.endswith(b'\n'), f'{path}: missing final newline')
    entries = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            entry = json.loads(line)
            require(isinstance(entry, dict), 'entry must be an object')
            fields = ({'seq', 'id', 'title', 'kind', 'decision', 'rule', 'chain_head',
                       'merged_commit', 'issue', 'utc', 'prev', 'sha256'} if index else
                      {'seq', 'type', 'utc', 'actor', 'data', 'prev', 'sha256'})
            require(set(entry) == fields, 'incorrect entry fields')
            require(type(entry['seq']) is int and entry['seq'] == number, 'sequence mismatch')
            require(entry['prev'] == (entries[-1]['sha256'] if entries else None), 'previous hash mismatch')
            unsigned = {k: v for k, v in entry.items() if k != 'sha256'}
            require(entry['sha256'] == digest(canonical(unsigned)), 'sha256 mismatch')
            require(isinstance(entry['utc'], str) and bool(entry['utc']), 'missing utc')
            if not index:
                require(entry['type'] in TYPES, 'unknown entry type')
                require(isinstance(entry['data'], dict), 'data must be an object')
                require(isinstance(entry['actor'], str) and bool(entry['actor']), 'missing actor')
                require(not entries or entries[-1]['type'] != 'close', 'entry after close')
            entries.append(entry)
        except (ValueError, TypeError, KeyError) as exc:
            raise LedgerError(f'{path}:{number}: {exc}') from exc
    return entries


def append(path: Path, entries: list[dict], fields: dict) -> dict:
    entry = dict(seq=len(entries) + 1, **fields, prev=entries[-1]['sha256'] if entries else None)
    entry['sha256'] = digest(canonical(entry))
    # One write; reject malformed existing chains before reaching here.
    with path.open('ab') as stream:
        stream.write(canonical(entry) + b'\n')
    return entry


def experiment_dirs(root: Path) -> list[Path]:
    area = root / 'experiments'
    return sorted(p for p in area.glob('EXP-*') if p.is_dir())


def experiment(root: Path, argument: str) -> Path:
    path = (root / argument).resolve()
    require(path.parent == (root / 'experiments').resolve() and bool(EXP.fullmatch(path.name)),
            'DIR must be an experiments/EXP-NNN-slug directory')
    require(path.is_dir(), f'{path}: experiment does not exist')
    return path


def validate_plan(path: Path) -> dict:
    p = read_json(path / 'plan.json')
    require(isinstance(p, dict), 'plan.json: expected object')
    require(p.get('schema') == 'eidos-exp-plan/1', 'plan.json: invalid schema')
    require(p.get('id') == EXP.fullmatch(path.name)[1], 'plan.json: id does not match directory')
    require(p.get('kind') in KINDS, 'plan.json: invalid kind')
    t = p.get('treatment')
    require(isinstance(t, dict) and all(isinstance(t.get(k), str) for k in ('incumbent', 'candidate')),
            'plan.json: treatment requires incumbent and candidate strings')
    require(isinstance(p.get('tiers'), list) and bool(p['tiers']), 'plan.json: tiers must be a nonempty list')
    for tier in p['tiers']:
        require(isinstance(tier, dict), 'plan.json: tier must be an object')
        require(tier.get('tier') in {'T0', 'T1', 'T2', 'T3'}, 'plan.json: unknown tier')
        require(all(isinstance(tier.get(k), str) for k in ('pool', 'what')), 'plan.json: pool/what must be strings')
        require(isinstance(tier.get('cards'), list) and all(isinstance(c, str) for c in tier['cards']),
                'plan.json: cards must be a list of strings')
        require(type(tier.get('replicates')) is int and tier['replicates'] > 0, 'plan.json: replicates must be positive')
        require('provider' in tier and (tier['provider'] is None or isinstance(tier['provider'], str)),
                'plan.json: provider must be string or null')
    budget = p.get('budget')
    require(isinstance(budget, dict), 'plan.json: budget must be an object')
    for key in ('live_runs', 'usd', 'quota_weekly_percent'):
        value = budget.get(key)
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                f'plan.json: budget.{key} must be a nonnegative number')
    require(type(budget['live_runs']) is int, 'plan.json: live_runs must be an integer')
    require(isinstance(p.get('stop_early'), list), 'plan.json: stop_early must be a list')
    require('second_provider' in p and (p['second_provider'] is None or isinstance(p['second_provider'], str)),
            'plan.json: second_provider must be string or null')
    return p


def validate_rule(root: Path, path: Path) -> dict:
    r = read_json(path / 'rule.json')
    require(isinstance(r, dict) and r.get('schema') == 'eidos-exp-rule-choice/1', 'rule.json: invalid schema')
    require(r.get('rule') in {'R-infra', 'R-mech', 'R-behav', 'R-disc', 'R-enable'}, 'rule.json: unknown rule')
    definition = read_json(root / 'experiments' / 'rules' / f"{r['rule']}.json")
    require(type(r.get('version')) is int and r['version'] == definition['version'], 'rule.json: unknown version')
    require(isinstance(r.get('overrides'), dict), 'rule.json: overrides must be an object')
    require(type(r.get('extension')) is bool, 'rule.json: extension must be boolean')
    return r


def validate_hypothesis(path: Path):
    text = (path / 'HYPOTHESIS.md').read_text()
    require('<!-- fill -->' not in text, 'HYPOTHESIS.md: remove every <!-- fill --> marker after filling sections')
    headings = list(re.finditer(r'^#{1,6}\s+(.+?)\s*$', text, re.MULTILINE))
    sections = {}
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections[heading[1]] = text[heading.end():end].strip()
    for title in SECTIONS:
        require(bool(sections.get(title)), f'HYPOTHESIS.md: section {title!r} needs text')


def check_registered(path: Path, entries: list[dict]):
    registrations = [e for e in entries if e['type'] == 'register']
    require(len(registrations) <= 1, f'{path}: multiple register entries')
    if registrations:
        require(entries[0]['type'] == 'register', f'{path}: register must be first')
        hashes = registrations[0]['data'].get('files', {})
        require(set(hashes) == set(FILES), f'{path}: register must hash all three registered files')
        for name in FILES:
            require((path / name).is_file() and digest((path / name).read_bytes()) == hashes[name],
                    f'{path / name}: registered file hash mismatch')


def verify(root: Path, against: str | None = None):
    area = root / 'experiments'
    index = read_chain(area / 'INDEX.jsonl', index=True)
    paths = experiment_dirs(root)
    by_id = {}
    for path in paths:
        match = EXP.fullmatch(path.name)
        require(bool(match), f'{path}: invalid experiment directory name')
        require(match[1] not in by_id, f'{path}: duplicate experiment id {match[1]}')
        entries = read_chain(path / 'ledger.jsonl')
        check_registered(path, entries)
        by_id[match[1]] = (path, entries)
    seen = set()
    for entry in index:
        ident = entry['id']
        require(ident not in seen, f'INDEX.jsonl: duplicate close for {ident}')
        seen.add(ident)
        require(ident in by_id, f'INDEX.jsonl: missing experiment {ident}')
        path, entries = by_id[ident]
        require(bool(entries) and entries[-1]['type'] == 'close', f'{path}: indexed experiment is not closed')
        require(entries[-1]['sha256'] == entry['chain_head'], f'{path}: index chain_head mismatch')
        close = entries[-1]['data']
        for key in ('merged_commit', 'issue'):
            require(entry[key] == close.get(key), f'{path}: index {key} mismatch')
        decisions = [e['data']['decision'] for e in entries if e['type'] == 'decision']
        require(bool(decisions) and decisions[-1] == entry['decision'], f'{path}: index decision mismatch')
        plan = read_json(path / 'plan.json')
        choice = read_json(path / 'rule.json')
        require(entry['title'] == plan.get('title', path.name) and entry['kind'] == plan['kind']
                and entry['rule'] == choice, f'{path}: index metadata mismatch')
    for ident, (path, entries) in by_id.items():
        require(not entries or entries[-1]['type'] != 'close' or ident in seen, f'{path}: close missing from index')
    if against:
        tracked = git(root, 'ls-tree', '-r', '--name-only', '-z', against, '--', 'experiments').decode().split('\0')
        protected = {'experiments/INDEX.jsonl': 'prefix'} if 'experiments/INDEX.jsonl' in tracked else {}
        old_dirs = {p.split('/')[1] for p in tracked if len(p.split('/')) > 2 and EXP.fullmatch(p.split('/')[1])}
        for name in old_dirs:
            for filename in (*FILES, 'ledger.jsonl'):
                relative = f'experiments/{name}/{filename}'
                if relative in tracked:
                    protected[relative] = 'prefix' if filename == 'ledger.jsonl' else 'equal'
            require((area / name).is_dir(), f'{name}: experiment deleted since {against}')
        head_paths = set(git(root, 'ls-tree', '-r', '--name-only', '-z', 'HEAD', '--', 'experiments').decode().split('\0'))
        for relative, mode in protected.items():
            old = git(root, 'show', f'{against}:{relative}')
            require(relative in head_paths, f'{relative}: deleted at HEAD since {against}')
            current = root / relative
            require(current.is_file(), f'{relative}: deleted since {against}')
            for location, new in [('HEAD', git(root, 'show', f'HEAD:{relative}')),
                                  ('working tree', current.read_bytes())]:
                require(new.startswith(old) if mode == 'prefix' else new == old,
                        f'{relative}: {"append-only prefix" if mode == "prefix" else "registered file"} violation against {against} ({location})')


def run(args, root: Path, *, clock=None, actor: str | None = None):
    """Run one command; tests can inject a UTC clock and actor without environment overrides."""
    area = root / 'experiments'
    def now():
        return (clock() if clock else datetime.now(UTC)).isoformat().replace('+00:00', 'Z')
    if args.cmd == 'verify':
        verify(root, args.against)
        print('Experiment ledger verified')
        return
    if args.cmd == 'status':
        verify(root)
        for path in experiment_dirs(root):
            entries = read_chain(path / 'ledger.jsonl')
            plan = read_json(path / 'plan.json')
            decisions = [e['data']['decision'] for e in entries if e['type'] == 'decision']
            print(plan['id'], plan.get('title', path.name), entries[-1]['type'] if entries else 'new',
                  decisions[-1] if decisions else '-')
        return
    if args.cmd == 'new':
        verify(root)
        require(bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', args.slug)), 'slug must be lowercase words separated by hyphens')
        require(bool(args.title.strip()), 'title must not be empty')
        numbers = [int(EXP.fullmatch(p.name)[2]) for p in experiment_dirs(root)]
        numbers += [int(e['id'].removeprefix('EXP-')) for e in read_chain(area / 'INDEX.jsonl', index=True)]
        ident = f'EXP-{max(numbers, default=-1) + 1:03d}'
        path = area / f'{ident}-{args.slug}'
        path.mkdir()
        for name in (*FILES, 'DECISION.md'):
            (path / name).write_bytes((area / 'templates' / name).read_bytes())
        plan = read_json(path / 'plan.json')
        plan.update(id=ident, title=args.title, kind=args.kind, issue=args.issue)
        (path / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
        (path / 'ledger.jsonl').touch()
        print(path.relative_to(root))
        return
    path = experiment(root, args.directory)
    verify(root)
    entries = read_chain(path / 'ledger.jsonl')
    require(not entries or entries[-1]['type'] != 'close', f'{path}: experiment already closed')
    kind = args.cmd
    data = {}
    if kind == 'register':
        require(not entries, 'register requires an empty chain')
        validate_hypothesis(path)
        validate_plan(path)
        validate_rule(root, path)
        changes = set()
        for command in [('diff', '--no-renames', '--name-only', '-z', f'{args.base}...HEAD'),
                        ('diff', '--no-renames', '--name-only', '-z', 'HEAD'), ('ls-files', '--others', '--exclude-standard', '-z')]:
            changes.update(p for p in git(root, *command).decode().split('\0') if p)
        outside = sorted(p for p in changes if not p.startswith('experiments/'))
        require(not outside, 'registration must precede code; paths outside experiments/: ' + ', '.join(outside))
        data = {'files': {name: digest((path / name).read_bytes()) for name in FILES},
                'base': git(root, 'rev-parse', args.base).decode().strip()}
    elif kind == 'record':
        kind = 'result'
        cards = []
        for filename in args.scorecard:
            raw = Path(filename).read_bytes()
            scorecard = json.loads(raw)
            require(isinstance(scorecard, dict), f'{filename}: scorecard must be an object')
            card = {'file': filename, 'sha256': digest(raw)}
            if 'aggregate' in scorecard:
                card['aggregate'] = scorecard['aggregate']
            cards.append(card)
        data = {'tier': args.tier, 'scorecards': cards, 'note': args.note}
        if args.cost is not None:
            data['cost'] = json.loads(args.cost)
    elif kind == 'review':
        data = {'kind': args.kind, 'verdict': args.verdict, 'commit': args.commit,
                'file': args.file, 'sha256': digest(Path(args.file).read_bytes())}
    elif kind == 'decide':
        kind = 'decision'
        if args.decision == 'keep':
            require(any(e['type'] == 'register' for e in entries), 'keep requires register')
            require(any(e['type'] == 'result' for e in entries), 'keep requires at least one result')
            plan = validate_plan(path)
            for review_kind in (('code',) if plan['kind'] == 'infra' else ('code', 'result')):
                reviews = [e for e in entries if e['type'] == 'review' and e['data'].get('kind') == review_kind]
                require(bool(reviews) and reviews[-1]['data'].get('verdict') == 'ACCEPT',
                        f'keep requires {review_kind} review ACCEPT')
        data = {'decision': args.decision, 'note': args.note}
        if args.decision_file:
            data.update(file=args.decision_file, sha256=digest(Path(args.decision_file).read_bytes()))
    elif kind == 'event':
        kind = args.type
        data = json.loads(args.data)
        require(isinstance(data, dict), 'event --data must be a JSON object')
    elif kind == 'close':
        require(any(e['type'] == 'register' for e in entries), 'close requires register')
        decisions = [e['data']['decision'] for e in entries if e['type'] == 'decision']
        require(bool(decisions), 'close requires a decision')
        plan = validate_plan(path)
        rule = validate_rule(root, path)
        index = read_chain(area / 'INDEX.jsonl', index=True)
        require(not any(e['id'] == plan['id'] for e in index), 'experiment already indexed')
        require(args.merged_commit == 'none' or bool(re.fullmatch(r'[0-9a-fA-F]{7,64}', args.merged_commit)),
                'merged commit must be a hex SHA or none')
        data = {'merged_commit': args.merged_commit, 'issue': args.issue}
    timestamp = now()
    who = actor if actor is not None else git(root, 'config', 'user.name').decode().strip()
    require(bool(who), 'configure git user.name for actor')
    entry = append(path / 'ledger.jsonl', entries, {'type': kind, 'utc': timestamp, 'actor': who, 'data': data})
    if kind == 'close':
        append(area / 'INDEX.jsonl', index, {'id': plan['id'], 'title': plan.get('title', path.name),
               'kind': plan['kind'], 'decision': decisions[-1], 'rule': rule, 'chain_head': entry['sha256'],
               'merged_commit': args.merged_commit, 'issue': args.issue, 'utc': timestamp})
    print(f'{path.relative_to(root)}: {kind} {entry["sha256"]}')
    if kind == 'register':
        directory = shlex.quote(str(path.relative_to(root)))
        print(f'git add -- {directory}\ngit commit -m {shlex.quote("Register " + path.name)}\ngit push -u origin HEAD')
        print('Commit and push this registration before changing code.')


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='cmd', required=True)
    new = commands.add_parser('new')
    new.add_argument('--title', required=True)
    new.add_argument('--slug', required=True)
    new.add_argument('--issue', type=int)
    new.add_argument('--kind', choices=sorted(KINDS), default='performance')
    for name in ('register', 'record', 'review', 'decide', 'event', 'close'):
        sub = commands.add_parser(name)
        sub.add_argument('directory', metavar='DIR')
        if name == 'register':
            sub.add_argument('--base', default='origin/main')
        elif name == 'record':
            sub.add_argument('--tier', choices=['T0', 'T1', 'T2', 'T3'], required=True)
            sub.add_argument('--scorecard', action='append', nargs='+', required=True)
            sub.add_argument('--cost')
            sub.add_argument('--note')
        elif name == 'review':
            sub.add_argument('--kind', choices=['code', 'result'], required=True)
            sub.add_argument('--verdict', choices=['ACCEPT', 'FIX-FIRST', 'REJECT'], required=True)
            sub.add_argument('--commit', required=True)
            sub.add_argument('--file', required=True)
        elif name == 'decide':
            sub.add_argument('--decision', choices=['keep', 'revert', 'extend', 'abandon'], required=True)
            sub.add_argument('--decision-file')
            sub.add_argument('--note')
        elif name == 'event':
            sub.add_argument('--type', choices=['incident', 'exposure', 'budget', 'note'], required=True)
            sub.add_argument('--data', required=True)
        else:
            sub.add_argument('--merged-commit', required=True)
            sub.add_argument('--issue', type=int, required=True)
    commands.add_parser('verify').add_argument('--against')
    commands.add_parser('status')
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == 'record':
        args.scorecard = [item for group in args.scorecard for item in group]
    try:
        root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel').decode().strip())
        run(args, root)
    except (LedgerError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'ledger: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
