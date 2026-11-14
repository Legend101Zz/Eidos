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
from datetime import UTC, datetime, timedelta
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


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(path: Path):
    return json.loads(path.read_bytes(), object_pairs_hook=unique_object)


def no_absolute_strings(value):
    if isinstance(value, str):
        require(not value.startswith('/'), 'absolute string value in ledger entry')
    elif isinstance(value, dict):
        for child in value.values():
            no_absolute_strings(child)
    elif isinstance(value, list):
        for child in value:
            no_absolute_strings(child)


def utc_time(value):
    require(isinstance(value, str), 'utc must be an ISO-8601 UTC timestamp')
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError('utc must be an ISO-8601 UTC timestamp') from exc
    require(stamp.utcoffset() == timedelta(0), 'utc must have an explicit UTC offset')
    return stamp


def require(condition: bool, message: str):
    if not condition:
        raise LedgerError(message)


def read_chain(path: Path, *, index: bool = False) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    require(not raw or raw.endswith(b'\n'), f'{path}: missing final newline')
    entries = []
    for number, line in enumerate(raw.split(b'\n')[:-1], 1):
        try:
            entry = json.loads(line, object_pairs_hook=unique_object)
            require(isinstance(entry, dict), 'entry must be an object')
            fields = ({'seq', 'id', 'title', 'kind', 'decision', 'rule', 'chain_head',
                       'merged_commit', 'issue', 'utc', 'prev', 'sha256'} if index else
                      {'seq', 'type', 'utc', 'actor', 'data', 'prev', 'sha256'})
            require(set(entry) == fields, 'incorrect entry fields')
            require(type(entry['seq']) is int and entry['seq'] == number, 'sequence mismatch')
            require(entry['prev'] == (entries[-1]['sha256'] if entries else None), 'previous hash mismatch')
            unsigned = {k: v for k, v in entry.items() if k != 'sha256'}
            require(entry['sha256'] == digest(canonical(unsigned)), 'sha256 mismatch')
            require(line == canonical(entry), 'non-canonical JSON line')
            no_absolute_strings(entry)
            timestamp = utc_time(entry['utc'])
            require(not entries or timestamp >= utc_time(entries[-1]['utc']), 'utc decreased')
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
    no_absolute_strings(entry)
    utc_time(entry['utc'])
    require(not entries or utc_time(entry['utc']) >= utc_time(entries[-1]['utc']), 'utc decreased')
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


def validate_rule(root: Path, path: Path, definition: dict | None = None) -> dict:
    r = read_json(path / 'rule.json')
    require(isinstance(r, dict) and r.get('schema') == 'eidos-exp-rule-choice/1', 'rule.json: invalid schema')
    require(r.get('rule') in {'R-infra', 'R-mech', 'R-behav', 'R-disc', 'R-enable'}, 'rule.json: unknown rule')
    definition = definition if definition is not None else read_json(root / 'experiments' / 'rules' / f"{r['rule']}.json")
    require(isinstance(definition, dict) and definition.get('id') == r['rule']
            and definition.get('schema') == 'eidos-exp-rule/1'
            and isinstance(definition.get('overridable'), dict)
            and isinstance(definition.get('keep_when'), list) and bool(definition['keep_when']),
            'rule.json: invalid rule definition')
    require(type(r.get('version')) is int and r['version'] == definition['version'], 'rule.json: unknown version')
    require(isinstance(r.get('overrides'), dict), 'rule.json: overrides must be an object')
    require(type(r.get('extension')) is bool, 'rule.json: extension must be boolean')
    plan = validate_plan(path)
    compatible = {'performance': {'R-mech', 'R-behav', 'R-disc'},
                  'enabling': {'R-enable', 'R-mech'}, 'infra': {'R-infra'}, 'measurement': {'R-infra'}}
    require(r['rule'] in compatible[plan['kind']], 'rule.json: incompatible kind and rule')
    bounds = definition.get('overridable', {})
    for key, value in r['overrides'].items():
        require(key in bounds, f'rule.json: unknown override {key}')
        require(type(value) in (int, float) and math.isfinite(value)
                and bounds[key]['min'] <= value <= bounds[key]['max'], f'rule.json: override {key} outside bounds')
    if r['rule'] == 'R-enable':
        target = plan.get('target')
        require(isinstance(target, dict) and all(k in target for k in ('metric', 'direction', 'amount', 'tier')),
                'plan.json: R-enable requires target metric, direction, amount and tier')
        require(isinstance(target['metric'], str) and bool(target['metric'])
                and target['direction'] in {'up', 'down'}
                and type(target['amount']) in (int, float) and math.isfinite(target['amount']) and target['amount'] > 0
                and target['tier'] in {t['tier'] for t in plan['tiers']}, 'plan.json: invalid target')
    require(type(plan.get('model_free_suffices', False)) is bool, 'plan.json: model_free_suffices must be boolean')
    require(isinstance(plan.get('named_cards', []), list)
            and all(isinstance(c, str) for c in plan.get('named_cards', [])), 'plan.json: named_cards must be strings')
    return r


def effective_rule(root: Path, path: Path, entries: list[dict]) -> dict:
    require(bool(entries) and entries[0]['type'] == 'register', 'rule requires register')
    data = entries[0]['data']
    rule = json.loads(canonical(data['rule_snapshot']))  # Detached from the immutable snapshot.
    choice = validate_rule(root, path, rule)
    plan = validate_plan(path)
    for clause in rule['keep_when']:
        key = clause.get('override')
        if key in choice['overrides']:
            clause['value'] = choice['overrides'][key] * clause.get('override_scale', 1)
        if rule['id'] == 'R-enable' and clause.get('metric') == 'vrg' and plan.get('model_free_suffices', False):
            clause.update(value=-0.01, tier='T1', mode='autopilot')
    rule.update(overrides=choice['overrides'], extension=choice['extension'], vetoes=data['vetoes_snapshot'])
    return rule


def keep_ready(path: Path, entries: list[dict]):
    require(bool(entries) and entries[0]['type'] == 'register', 'keep requires register')
    require(any(e['type'] == 'result' for e in entries), 'keep requires at least one result')
    plan = validate_plan(path)
    for kind in (('code',) if plan['kind'] == 'infra' else ('code', 'result')):
        reviews = [e for e in entries if e['type'] == 'review' and e['data'].get('kind') == kind]
        require(bool(reviews) and reviews[-1]['data'].get('verdict') == 'ACCEPT', f'keep requires {kind} review ACCEPT')


def replay(path: Path, entries: list[dict]):
    for i, entry in enumerate(entries):
        kind, data = entry['type'], entry['data']
        require(entries[0]['type'] == 'register', f'{path}: register must be first')
        if kind == 'register':
            require(i == 0, f'{path}: multiple register entries')
        elif kind == 'review':
            require(data.get('kind') in {'code', 'result'} and data.get('verdict') in {'ACCEPT', 'FIX-FIRST', 'REJECT'},
                    f'{path}: invalid review')
        elif kind == 'decision':
            require(data.get('decision') in {'keep', 'revert', 'extend', 'abandon'}, f'{path}: invalid decision')
            if data['decision'] == 'keep':
                keep_ready(path, entries[:i])
        elif kind == 'close':
            decisions = [e for e in entries[:i] if e['type'] == 'decision']
            require(bool(decisions), f'{path}: close requires a decision')
            require(decisions[-1]['data']['decision'] != 'extend', f'{path}: cannot close extend')
            require(data.get('decision_sha256') == decisions[-1]['sha256'], f'{path}: close must bind latest decision')
            if decisions[-1]['data']['decision'] == 'keep':
                keep_ready(path, entries[:i])


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
        data = registrations[0]['data']
        for name in ('rule', 'vetoes'):
            snapshot = data.get(f'{name}_snapshot')
            require(isinstance(snapshot, dict) and data.get(f'{name}_sha256') == digest(canonical(snapshot)),
                    f'{path}: {name} snapshot hash mismatch')
        hashes = data.get('files', {})
        require(set(hashes) == set(FILES), f'{path}: register must hash all three registered files')
        for name in FILES:
            require((path / name).is_file() and digest((path / name).read_bytes()) == hashes[name],
                    f'{path / name}: registered file hash mismatch')


def blob(root: Path, commit: str, relative: str) -> bytes | None:
    result = subprocess.run(['git', '-C', str(root), 'show', f'{commit}:{relative}'], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def contains_registration(raw: bytes | None) -> bool:
    return bool(raw) and any(json.loads(line).get('type') == 'register' for line in raw.splitlines())


def check_registration_history(root: Path, point: str, paths: list[Path]):
    commits = None
    for path in paths:
        relative = str(path.relative_to(root) / 'ledger.jsonl')
        if contains_registration(blob(root, point, relative)):
            continue
        if commits is None:
            commits = git(root, 'rev-list', '--reverse', '--topo-order', f'{point}..HEAD').decode().splitlines()
        registration = next((c for c in commits if contains_registration(blob(root, c, relative))), None)
        if registration is None:  # Registration is still only in the working tree.
            continue
        base = git(root, 'merge-base', point, registration).decode().strip()
        changes = git(root, 'diff', '--no-renames', '--name-only', '-z', base, registration).decode().split('\0')
        outside = [p for p in changes if p and not p.startswith('experiments/')]
        require(not outside, f'{path.name}: registration commit {registration} must precede code: ' + ', '.join(outside))
        for name in FILES:
            relative_file = str(path.relative_to(root) / name)
            original = blob(root, registration, relative_file)
            require(original is not None and original == blob(root, 'HEAD', relative_file)
                    and original == (path / name).read_bytes(),
                    f'{relative_file}: registered file changed since registration commit {registration}')


def review_commit(root: Path, reference: str) -> str:
    git(root, 'cat-file', '-e', f'{reference}^{{commit}}')
    commit = git(root, 'rev-parse', f'{reference}^{{commit}}').decode().strip()
    try:
        git(root, 'merge-base', '--is-ancestor', commit, 'HEAD')
    except LedgerError as exc:
        raise LedgerError(f'review commit {commit} must be HEAD or an ancestor of HEAD') from exc
    return commit


def copy_evidence(root: Path, path: Path, filename: str, folder: str) -> dict:
    source = Path(filename)
    raw = source.read_bytes()
    sha = digest(raw)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', source.name)
    destination = path / folder / f'{sha}-{safe_name}'
    require(destination.resolve().is_relative_to(path.resolve()), 'evidence destination escapes experiment')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        require(destination.read_bytes() == raw, 'existing evidence hash mismatch')
    else:
        destination.write_bytes(raw)
    return {'file': destination.relative_to(root).as_posix(), 'sha256': sha}


def check_evidence(root: Path, path: Path, entries: list[dict]):
    for entry in entries:
        data = entry['data']
        evidence = data.get('scorecards', []) if entry['type'] == 'result' else ([data] if 'file' in data else [])
        require(isinstance(evidence, list), f'{path}: invalid scorecards')
        for item in evidence:
            require(isinstance(item, dict) and isinstance(item.get('file'), str), f'{path}: invalid evidence')
            file = root / item['file']
            require(not Path(item['file']).is_absolute() and file.resolve().is_relative_to(path.resolve()),
                    f'{path}: evidence must be repository-relative inside experiment')
            require(file.is_file() and digest(file.read_bytes()) == item.get('sha256'),
                    f'{item["file"]}: evidence hash mismatch')
            if entry['type'] == 'result':
                scorecard = read_json(file)
                require(isinstance(scorecard, dict), f'{file}: scorecard must be object')
                require(item.get('aggregate') == scorecard.get('aggregate'), f'{file}: evidence aggregate mismatch')
        if entry['type'] == 'review':
            require(isinstance(data.get('commit'), str), f'{path}: review requires commit')
            review_commit(root, data['commit'])


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
        replay(path, entries)
        if entries:
            effective_rule(root, path, entries)
            if entries[-1]['type'] != 'close':
                for name, file in (('rule', f"{entries[0]['data']['rule_snapshot']['id']}.json"), ('vetoes', 'VETOES.json')):
                    live = area / 'rules' / file
                    try:
                        matches = canonical(read_json(live)) == canonical(entries[0]['data'][f'{name}_snapshot'])
                    except (OSError, ValueError):
                        matches = False
                    if not matches:
                        print(f'warning: {path.name}: live {file} differs from registered snapshot', file=sys.stderr)
            check_evidence(root, path, entries)
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
        point = git(root, 'merge-base', 'HEAD', against).decode().strip()
        if point == git(root, 'rev-parse', 'HEAD').decode().strip() and point != git(root, 'rev-parse', against).decode().strip():
            return
        check_registration_history(root, point, paths)
        tracked = git(root, 'ls-tree', '-r', '--name-only', '-z', point, '--', 'experiments').decode().split('\0')
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
            old = git(root, 'show', f'{point}:{relative}')
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
    if args.cmd == 'rule':
        path = experiment(root, args.directory)
        verify(root)
        print(canonical(effective_rule(root, path, read_chain(path / 'ledger.jsonl'))).decode())
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
        require(args.issue > 0, 'issue must be positive')
        ident = f'EXP-{args.issue:03d}'
        require(not any(EXP.fullmatch(p.name)[1] == ident for p in experiment_dirs(root)), 'issue already has an experiment')
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
    require(kind == 'register' or bool(entries), f'{kind} requires register')
    data = {}
    if kind == 'register':
        require(not entries, 'register requires an empty chain')
        validate_hypothesis(path)
        validate_plan(path)
        choice = validate_rule(root, path)
        trunk = git(root, 'rev-parse', '--verify', f'{args.trunk}^{{commit}}').decode().strip()
        base = git(root, 'merge-base', 'HEAD', trunk).decode().strip()
        changes = set()
        for command in [('diff', '--no-renames', '--name-only', '-z', base, 'HEAD'),
                        ('diff', '--no-renames', '--name-only', '-z', '--cached', 'HEAD'),
                        ('diff', '--no-renames', '--name-only', '-z'), ('ls-files', '--others', '--exclude-standard', '-z')]:
            changes.update(p for p in git(root, *command).decode().split('\0') if p)
        outside = sorted(p for p in changes if not p.startswith('experiments/'))
        require(not outside, 'registration must precede code; paths outside experiments/: ' + ', '.join(outside))
        data = {'files': {name: digest((path / name).read_bytes()) for name in FILES},
                'trunk': args.trunk, 'base_commit': base,
                'head_commit': git(root, 'rev-parse', 'HEAD').decode().strip()}
        for name, filename in (('rule', f"{choice['rule']}.json"), ('vetoes', 'VETOES.json')):
            snapshot = read_json(area / 'rules' / filename)
            data[f'{name}_snapshot'] = snapshot
            data[f'{name}_sha256'] = digest(canonical(snapshot))
    elif kind == 'record':
        kind = 'result'
        cards = []
        for filename in args.scorecard:
            raw = Path(filename).read_bytes()
            scorecard = json.loads(raw)
            require(isinstance(scorecard, dict), f'{filename}: scorecard must be an object')
            card = copy_evidence(root, path, filename, f'results/{args.tier}')
            if 'aggregate' in scorecard:
                card['aggregate'] = scorecard['aggregate']
            cards.append(card)
        data = {'tier': args.tier, 'scorecards': cards, 'note': args.note}
        if args.cost is not None:
            data['cost'] = json.loads(args.cost)
    elif kind == 'review':
        commit = review_commit(root, args.commit)
        data = {'kind': args.kind, 'verdict': args.verdict, 'commit': commit,
                **copy_evidence(root, path, args.file, 'reviews')}
    elif kind == 'decide':
        kind = 'decision'
        if args.decision == 'keep':
            keep_ready(path, entries)
        data = {'decision': args.decision, 'note': args.note}
        if args.decision_file:
            data.update(copy_evidence(root, path, args.decision_file, 'decisions'))
    elif kind == 'event':
        kind = args.type
        data = json.loads(args.data)
        require(isinstance(data, dict), 'event --data must be a JSON object')
    elif kind == 'close':
        require(any(e['type'] == 'register' for e in entries), 'close requires register')
        decisions = [e['data']['decision'] for e in entries if e['type'] == 'decision']
        trunk_ref = git(root, 'rev-parse', '--symbolic-full-name', entries[0]['data']['trunk']).decode().strip()
        if trunk_ref.startswith('refs/remotes/'):
            trunk = trunk_ref.removeprefix('refs/remotes/').split('/', 1)[1]
        else:
            require(trunk_ref.startswith('refs/heads/'), 'close requires a named trunk branch')
            trunk = trunk_ref.removeprefix('refs/heads/')
        branch = git(root, 'branch', '--show-current').decode().strip()
        require(args.allow_branch or branch == trunk,
                f'close requires trunk local branch {trunk}; index is appended at merge time only')
        require(bool(decisions), 'close requires a decision')
        require(decisions[-1] != 'extend', 'cannot close extend; finish with keep, revert or abandon')
        if decisions[-1] == 'keep':
            keep_ready(path, entries)
        plan = validate_plan(path)
        rule = validate_rule(root, path, entries[0]['data']['rule_snapshot'])
        index = read_chain(area / 'INDEX.jsonl', index=True)
        require(not any(e['id'] == plan['id'] for e in index), 'experiment already indexed')
        require(args.merged_commit == 'none' or bool(re.fullmatch(r'[0-9a-fA-F]{7,64}', args.merged_commit)),
                'merged commit must be a hex SHA or none')
        decision = next(e for e in reversed(entries) if e['type'] == 'decision')
        data = {'merged_commit': args.merged_commit, 'issue': args.issue, 'decision_sha256': decision['sha256']}
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
    new.add_argument('--issue', type=int, required=True)
    new.add_argument('--kind', choices=sorted(KINDS), default='performance')
    for name in ('register', 'record', 'review', 'decide', 'event', 'close', 'rule'):
        sub = commands.add_parser(name)
        sub.add_argument('directory', metavar='DIR')
        if name == 'register':
            sub.add_argument('--trunk', default='origin/main')
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
        elif name == 'close':
            sub.add_argument('--allow-branch', action='store_true', help=argparse.SUPPRESS)
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
