"""Ledger contracts exercised through the CLI in disposable Git repositories."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from study.exp import ledger

ROOT = Path(__file__).resolve().parents[2]
KNOWN_METRICS = {'vrg', 'mrg', 'verified_rate', 'error_ratio', 'uplift_autopilot',
                 'novelty_wins', 'false_claims', 'dead_ends', 'tokens', 'target'}


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(root, message='fixture'):
    git(root, 'add', '.')
    git(root, 'commit', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Fixture Actor')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    shutil.copytree(ROOT / 'experiments', root / 'experiments')
    commit(root, 'base templates')
    git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    git(root, 'checkout', '-b', 'experiment')
    return root


def cli(root, *args, error=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, '-m', 'study.exp.ledger', *map(str, args)],
                            cwd=root, env=env, capture_output=True, text=True)
    if error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0, result.stdout
        assert error in result.stderr, result.stderr
    return result.stdout


def new(root, slug='fixture', kind='infra'):
    directory = cli(root, 'new', '--title', 'A fixture experiment', '--slug', slug, '--kind', kind, '--issue', 10).strip()
    return root / directory


def fill(path):
    (path / 'HYPOTHESIS.md').write_text('\n\n'.join(f'## {section}\nConcrete evidence for {section}.'
                                                for section in ledger.SECTIONS) + '\n')


def registered(root, **kwargs):
    path = new(root, **kwargs)
    fill(path)
    cli(root, 'register', path)
    return path


def result(root, path):
    card = root / 'scorecard.json'
    card.write_text(json.dumps({'aggregate': {'vrg': .123, 'nested': [1, None, {'raw': 'unchanged'}]}}))
    cli(root, 'record', path, '--tier', 'T0', '--scorecard', card, '--scorecard', card,
        '--cost', '{"usd":0}', '--note', 'fixture evidence')
    return card


def review(root, path, kind='code', verdict='ACCEPT'):
    file = root / 'REVIEW.md'
    file.write_text('Independent fixture review')
    cli(root, 'review', path, '--kind', kind, '--verdict', verdict,
        '--commit', git(root, 'rev-parse', 'HEAD'), '--file', file)


def close(root, path):
    cli(root, 'decide', path, '--decision', 'revert', '--decision-file', path / 'DECISION.md', '--note', 'no gain')
    cli(root, 'close', path, '--merged-commit', 'none', '--issue', 10)


def test_empty_and_new_ids(repo):
    cli(repo, 'verify', '--against', 'origin/main')
    assert cli(repo, 'status') == ''
    first = new(repo)
    assert first.name == 'EXP-000-fixture'
    assert json.loads((first / 'plan.json').read_text())['id'] == 'EXP-000'
    assert (first / 'ledger.jsonl').read_bytes() == b''
    second = new(repo, slug='next')
    assert second.name == 'EXP-001-next'
    cli(repo, 'status')


def test_every_command_and_close_index(repo):
    path = registered(repo)
    card = result(repo, path)
    entry = ledger.read_chain(path / 'ledger.jsonl')[-1]
    assert len(entry['data']['scorecards']) == 2
    assert entry['data']['scorecards'][0]['aggregate'] == json.loads(card.read_text())['aggregate']
    assert entry['data']['scorecards'][0]['sha256'] == ledger.digest(card.read_bytes())
    review(repo, path)
    for kind in ('incident', 'exposure', 'budget', 'note'):
        cli(repo, 'event', path, '--type', kind, '--data', '{"detail":"fixture"}')
    cli(repo, 'decide', path, '--decision', 'keep')
    cli(repo, 'close', path, '--merged-commit', git(repo, 'rev-parse', 'HEAD'), '--issue', 10)
    chain = ledger.read_chain(path / 'ledger.jsonl')
    index = ledger.read_chain(repo / 'experiments/INDEX.jsonl', index=True)
    assert len(index) == 1
    assert index[0]['chain_head'] == chain[-1]['sha256']
    assert chain[0]['actor'] == 'Fixture Actor'
    assert chain[0]['utc'].endswith('Z')
    assert index[0]['decision'] == 'keep'
    cli(repo, 'verify', '--against', 'origin/main')
    assert 'close keep' in cli(repo, 'status')
    cli(repo, 'close', path, '--merged-commit', 'none', '--issue', 10, error='already closed')
    cli(repo, 'event', path, '--type', 'note', '--data', '{}', error='already closed')
    assert len(ledger.read_chain(repo / 'experiments/INDEX.jsonl', index=True)) == 1


def test_register_rejects_unfilled_and_empty_sections(repo):
    path = new(repo)
    cli(repo, 'register', path, error='fill')
    fill(path)
    text = (path / 'HYPOTHESIS.md').read_text().replace('Concrete evidence for Prediction.', '')
    (path / 'HYPOTHESIS.md').write_text(text)
    cli(repo, 'register', path, error="'Prediction' needs text")


@pytest.mark.parametrize('field,value,message', [
    ('rule', '../unknown', 'unknown rule'), ('version', 2, 'unknown version'),
    ('extension', 'false', 'extension must be boolean'), ('overrides', [], 'overrides must be an object'),
])
def test_invalid_rules(repo, field, value, message):
    path = new(repo)
    fill(path)
    choice = json.loads((path / 'rule.json').read_text())
    choice[field] = value
    (path / 'rule.json').write_text(json.dumps(choice))
    cli(repo, 'register', path, error=message)


@pytest.mark.parametrize('field,value,message', [
    ('kind', 'unknown', 'invalid kind'), ('id', 'EXP-999', 'id does not match'),
    ('tiers', [], 'tiers must be a nonempty list'), ('treatment', {}, 'treatment requires'),
    ('budget', {'live_runs': -1, 'usd': 0, 'quota_weekly_percent': 0}, 'budget.live_runs'),
])
def test_invalid_plan(repo, field, value, message):
    path = new(repo)
    fill(path)
    plan = json.loads((path / 'plan.json').read_text())
    plan[field] = value
    (path / 'plan.json').write_text(json.dumps(plan))
    cli(repo, 'register', path, error=message)


@pytest.mark.parametrize('state', ['untracked', 'staged', 'committed', 'unstaged'])
def test_registration_precedes_code(repo, state):
    if state == 'unstaged':
        (repo / 'code.py').write_text('base')
        commit(repo)
        git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    path = new(repo)
    fill(path)
    (repo / 'code.py').write_text('changed')
    if state in ('staged', 'committed'):
        git(repo, 'add', 'code.py')
    if state == 'committed':
        git(repo, 'commit', '-m', 'too early')
    cli(repo, 'register', path, error='outside experiments/: code.py')
    assert not (path / 'ledger.jsonl').read_bytes()


def test_register_reminder_and_duplicate(repo):
    path = new(repo)
    fill(path)
    output = cli(repo, 'register', path)
    assert 'git add -- experiments/EXP-000-fixture' in output
    assert "git commit -m 'Register EXP-000-fixture'" in output
    assert 'git push -u origin HEAD' in output
    cli(repo, 'register', path, error='empty chain')
    assert ledger.read_chain(path / 'ledger.jsonl')[0]['data']['base'] == git(repo, 'rev-parse', 'origin/main')


@pytest.mark.parametrize('kind', ['infra', 'performance'])
def test_keep_prerequisites_and_latest_review(repo, kind):
    path = new(repo, kind=kind)
    cli(repo, 'decide', path, '--decision', 'keep', error='requires register')
    fill(path)
    cli(repo, 'register', path)
    cli(repo, 'decide', path, '--decision', 'keep', error='requires at least one result')
    result(repo, path)
    cli(repo, 'decide', path, '--decision', 'keep', error='code review ACCEPT')
    review(repo, path, verdict='FIX-FIRST')
    cli(repo, 'decide', path, '--decision', 'keep', error='code review ACCEPT')
    review(repo, path)
    if kind != 'infra':
        cli(repo, 'decide', path, '--decision', 'keep', error='result review ACCEPT')
        review(repo, path, kind='result')
    cli(repo, 'decide', path, '--decision', 'keep')
    review(repo, path, verdict='REJECT')
    cli(repo, 'decide', path, '--decision', 'keep', error='code review ACCEPT')


@pytest.mark.parametrize('decision', ['revert', 'extend', 'abandon'])
def test_other_decisions(repo, decision):
    path = registered(repo)
    cli(repo, 'decide', path, '--decision', decision)
    assert ledger.read_chain(path / 'ledger.jsonl')[-1]['data']['decision'] == decision


@pytest.mark.parametrize('tamper', ['edit', 'delete', 'reorder'])
def test_chain_tampering(repo, tamper):
    path = registered(repo)
    for i in range(2):
        cli(repo, 'event', path, '--type', 'note', '--data', json.dumps({'i': i}))
    file = path / 'ledger.jsonl'
    lines = file.read_bytes().splitlines(keepends=True)
    if tamper == 'edit':
        lines[1] = lines[1].replace(b'"i":0', b'"i":9')
    elif tamper == 'delete':
        del lines[1]
    else:
        lines[1], lines[2] = lines[2], lines[1]
    file.write_bytes(b''.join(lines))
    cli(repo, 'verify', error='ledger.jsonl:2:')


def test_against_truncated_chain_and_append(repo):
    path = registered(repo)
    cli(repo, 'event', path, '--type', 'note', '--data', '{}')
    commit(repo)
    git(repo, 'tag', 'reference')
    original = (path / 'ledger.jsonl').read_bytes()
    cli(repo, 'event', path, '--type', 'note', '--data', '{"appended":true}')
    commit(repo)
    cli(repo, 'verify', '--against', 'reference')
    (path / 'ledger.jsonl').write_bytes(original.splitlines(keepends=True)[0])
    cli(repo, 'verify')  # A valid shorter hash chain still violates the reference prefix.
    cli(repo, 'verify', '--against', 'reference', error='append-only prefix violation')


def test_against_rewritten_index_even_when_rehashed(repo):
    path = registered(repo)
    close(repo, path)
    commit(repo)
    git(repo, 'tag', 'reference')
    index = repo / 'experiments/INDEX.jsonl'
    entries = ledger.read_chain(index, index=True)
    fields = {k: v for k, v in entries[0].items() if k not in {'seq', 'prev', 'sha256'}}
    fields['utc'] = '2000-01-01T00:00:00Z'
    index.write_bytes(b'')
    ledger.append(index, [], fields)
    cli(repo, 'verify')
    cli(repo, 'verify', '--against', 'reference', error='INDEX.jsonl: append-only prefix violation')


def test_registered_file_edit_and_reference_rehash(repo):
    path = registered(repo)
    commit(repo)
    git(repo, 'tag', 'reference')
    (path / 'HYPOTHESIS.md').write_text('rewritten evidence')
    cli(repo, 'verify', error='registered file hash mismatch')
    file = path / 'ledger.jsonl'
    entry = ledger.read_chain(file)[0]
    entry['data']['files']['HYPOTHESIS.md'] = ledger.digest((path / 'HYPOTHESIS.md').read_bytes())
    fields = {k: v for k, v in entry.items() if k not in {'seq', 'prev', 'sha256'}}
    file.write_bytes(b'')
    ledger.append(file, [], fields)
    cli(repo, 'verify')
    cli(repo, 'verify', '--against', 'reference', error='registered file violation')


def test_against_deleted_experiment(repo):
    path = registered(repo)
    commit(repo)
    git(repo, 'tag', 'reference')
    shutil.rmtree(path)
    cli(repo, 'verify', '--against', 'reference', error='experiment deleted')


def test_two_branches_merge_and_close_in_sequence(repo):
    # Reserve two ids before either branch starts; chains can then grow independently.
    first = new(repo, slug='first')
    second = new(repo, slug='second')
    for path in (first, second):
        fill(path)
    commit(repo, 'reserve ids')
    git(repo, 'branch', 'second')
    cli(repo, 'register', first)
    commit(repo, 'first registration')
    git(repo, 'checkout', 'second')
    cli(repo, 'register', second)
    commit(repo, 'second registration')
    git(repo, 'checkout', 'experiment')
    git(repo, 'merge', '--no-ff', 'second', '-m', 'merge parallel chains')
    close(repo, first)
    commit(repo, 'first index append')
    git(repo, 'tag', 'reference')
    close(repo, second)
    commit(repo, 'second index append')
    cli(repo, 'verify', '--against', 'reference')
    index = ledger.read_chain(repo / 'experiments/INDEX.jsonl', index=True)
    assert [entry['id'] for entry in index] == ['EXP-000', 'EXP-001']
    assert index[1]['prev'] == index[0]['sha256']


def test_clock_injection(repo):
    path = new(repo)
    args = ledger.parser().parse_args(['event', str(path), '--type', 'note', '--data', '{}'])
    ledger.run(args, repo, clock=lambda: datetime(2026, 1, 2, 3, 4, tzinfo=UTC), actor='Clock fixture')
    entry = ledger.read_chain(path / 'ledger.jsonl')[0]
    assert entry['utc'] == '2026-01-02T03:04:00Z'
    assert entry['actor'] == 'Clock fixture'


def test_golden_rules_and_empty_index():
    assert (ROOT / 'experiments/INDEX.jsonl').read_bytes() == b''
    files = sorted((ROOT / 'experiments/rules').glob('*.json'))
    assert len(files) == 6

    def inspect(value):
        if isinstance(value, dict):
            if 'metric' in value:
                assert value['metric'] in KNOWN_METRICS
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    for file in files:
        rule = json.loads(file.read_text())
        assert rule['schema'] == 'eidos-exp-rule/1'
        assert rule['version'] == 1
        assert rule['keep_when'] and rule['text']
        inspect(rule)


def test_registration_rejects_rename_into_experiments(repo):
    (repo / 'code.py').write_text('base code')
    commit(repo)
    git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    path = new(repo)
    fill(path)
    git(repo, 'mv', 'code.py', str(path / 'moved.py'))
    cli(repo, 'register', path, error='outside experiments/: code.py')


def test_result_without_aggregate_and_multiple_files(repo):
    path = registered(repo)
    first = repo / 'empty-scorecard.json'
    second = repo / 'second-scorecard.json'
    first.write_text('{"individual": []}')
    second.write_text('{"aggregate": {"vrg": 0}}')
    cli(repo, 'record', path, '--tier', 'T1', '--scorecard', first, second)
    cards = ledger.read_chain(path / 'ledger.jsonl')[-1]['data']['scorecards']
    assert 'aggregate' not in cards[0]
    assert cards[1]['aggregate'] == {'vrg': 0}


def test_close_requires_registration_and_decision(repo):
    path = new(repo)
    cli(repo, 'close', path, '--merged-commit', 'none', '--issue', 10, error='requires register')
    fill(path)
    cli(repo, 'register', path)
    cli(repo, 'close', path, '--merged-commit', 'none', '--issue', 10, error='requires a decision')
    cli(repo, 'decide', path, '--decision', 'abandon')
    cli(repo, 'close', path, '--merged-commit', 'not-a-sha', '--issue', 10, error='hex SHA or none')
    assert not (repo / 'experiments/INDEX.jsonl').read_bytes()


def test_index_hash_tamper(repo):
    path = registered(repo)
    close(repo, path)
    index = repo / 'experiments/INDEX.jsonl'
    index.write_bytes(index.read_bytes().replace(b'"issue":10', b'"issue":11'))
    cli(repo, 'verify', error='INDEX.jsonl:1: sha256 mismatch')


def test_against_checks_head_even_if_working_tree_restores_index(repo):
    path = registered(repo)
    close(repo, path)
    commit(repo)
    git(repo, 'tag', 'reference')
    index = repo / 'experiments/INDEX.jsonl'
    original = index.read_bytes()
    entry = ledger.read_chain(index, index=True)[0]
    fields = {k: v for k, v in entry.items() if k not in {'seq', 'prev', 'sha256'}}
    fields['utc'] = '2000-01-01T00:00:00Z'
    index.write_bytes(b'')
    ledger.append(index, [], fields)
    commit(repo, 'rewrite index')
    index.write_bytes(original)
    cli(repo, 'verify', '--against', 'reference', error='(HEAD)')
