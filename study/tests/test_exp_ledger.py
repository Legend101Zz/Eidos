"""Ledger contracts exercised through the CLI in disposable Git repositories."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import pytest
except ImportError:
    # unittest discovery imports this pytest-only module in minimal environments.
    # Identity decorators keep import safe; unittest collects zero free functions.
    from types import SimpleNamespace
    pytest = SimpleNamespace(fixture=lambda function: function,
                             mark=SimpleNamespace(parametrize=lambda *a, **k: lambda function: function))

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


def new(root, slug='fixture', kind='infra', issue=10):
    directory = cli(root, 'new', '--title', 'A fixture experiment', '--slug', slug, '--kind', kind, '--issue', issue).strip()
    path = root / directory
    if kind in {'infra', 'measurement'}:
        choice = json.loads((path / 'rule.json').read_text())
        choice['rule'] = 'R-infra'
        (path / 'rule.json').write_text(json.dumps(choice))
    return path


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
    cli(root, 'close', path, '--allow-branch', '--merged-commit', 'none', '--issue',
        json.loads((path / 'plan.json').read_text())['issue'])


def test_empty_and_new_ids(repo):
    cli(repo, 'verify', '--against', 'origin/main')
    assert cli(repo, 'status') == ''
    first = new(repo)
    assert first.name == 'EXP-010-fixture'
    assert json.loads((first / 'plan.json').read_text())['id'] == 'EXP-010'
    assert (first / 'ledger.jsonl').read_bytes() == b''
    second = new(repo, slug='next', issue=11)
    assert second.name == 'EXP-011-next'
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
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', git(repo, 'rev-parse', 'HEAD'), '--issue', 10)
    chain = ledger.read_chain(path / 'ledger.jsonl')
    index = ledger.read_chain(repo / 'experiments/INDEX.jsonl', index=True)
    assert len(index) == 1
    assert index[0]['chain_head'] == chain[-1]['sha256']
    assert chain[0]['actor'] == 'Fixture Actor'
    assert chain[0]['utc'].endswith('Z')
    assert index[0]['decision'] == 'keep'
    cli(repo, 'verify', '--against', 'origin/main')
    assert 'close keep' in cli(repo, 'status')
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', 'none', '--issue', 10, error='already closed')
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
    assert 'git add -- experiments/EXP-010-fixture' in output
    assert "git commit -m 'Register EXP-010-fixture'" in output
    assert 'git push -u origin HEAD' in output
    cli(repo, 'register', path, error='empty chain')
    assert ledger.read_chain(path / 'ledger.jsonl')[0]['data']['base_commit'] == git(repo, 'rev-parse', 'origin/main')


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
    commit(repo, 'registration before later push')
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
    cli(repo, 'verify', error='registration ledger prefix')
    cli(repo, 'verify', '--against', 'reference', error='registration ledger prefix')


def test_against_deleted_experiment(repo):
    path = registered(repo)
    commit(repo)
    git(repo, 'tag', 'reference')
    shutil.rmtree(path)
    cli(repo, 'verify', '--against', 'reference', error='experiment deleted')


def test_two_branches_merge_and_close_in_sequence(repo):
    git(repo, 'branch', 'second')
    first = registered(repo, slug='first', issue=10)
    commit(repo, 'first registration')
    git(repo, 'checkout', 'second')
    second = registered(repo, slug='second', issue=11)
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
    assert [entry['id'] for entry in index] == ['EXP-010', 'EXP-011']
    assert index[1]['prev'] == index[0]['sha256']


def test_clock_injection(repo):
    path = new(repo)
    fill(path)
    register_args = ledger.parser().parse_args(['register', str(path)])
    ledger.run(register_args, repo, clock=lambda: datetime(2026, 1, 2, 3, 3, tzinfo=UTC), actor='Clock fixture')
    args = ledger.parser().parse_args(['event', str(path), '--type', 'note', '--data', '{}'])
    ledger.run(args, repo, clock=lambda: datetime(2026, 1, 2, 3, 4, tzinfo=UTC), actor='Clock fixture')
    entry = ledger.read_chain(path / 'ledger.jsonl')[-1]
    assert entry['utc'] == '2026-01-02T03:04:00Z'
    assert entry['actor'] == 'Clock fixture'


def test_golden_rules_and_index_verifies():
    ledger.verify(ROOT)
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
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', 'none', '--issue', 10, error='requires register')
    fill(path)
    cli(repo, 'register', path)
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', 'none', '--issue', 10, error='requires a decision')
    cli(repo, 'decide', path, '--decision', 'abandon')
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', 'not-a-sha', '--issue', 10, error='requires merged commit none')
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


# Review regressions: these attacks must fail even with a freshly rehashed chain.
def rewrite_chain(path, entries):
    # Attack construction deliberately bypasses the production append checks.
    written = []
    for entry in entries:
        value = {k: v for k, v in entry.items() if k not in {'seq', 'prev', 'sha256'}}
        value.update(seq=len(written) + 1, prev=written[-1]['sha256'] if written else None)
        value['sha256'] = ledger.digest(ledger.canonical(value))
        written.append(value)
    (path / 'ledger.jsonl').write_bytes(b''.join(ledger.canonical(e) + b'\n' for e in written))


def test_late_registration_base_head_bypass(repo):
    path = new(repo)
    fill(path)
    (repo / 'code.py').write_text('too early')
    commit(repo)
    cli(repo, 'register', path, '--base', 'HEAD', error='unrecognized arguments')


def test_registration_commit_cannot_include_code(repo):
    registered(repo)
    (repo / 'code.py').write_text('sneaked into registration commit')
    commit(repo)
    cli(repo, 'verify', '--against', 'origin/main', error='code.py')


def test_rule_snapshot_is_authoritative(repo):
    path = registered(repo, kind='performance')
    definition = repo / 'experiments/rules/R-mech.json'
    before = json.loads(definition.read_text())
    definition.write_text(json.dumps(dict(before, keep_when=[])))
    output = cli(repo, 'rule', path)
    assert json.loads(output)['keep_when'] == before['keep_when']
    process = subprocess.run([sys.executable, '-m', 'study.exp.ledger', 'verify'], cwd=repo,
                             env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True)
    assert process.returncode == 0 and 'warning' in process.stderr.lower()
    entry = ledger.read_chain(path / 'ledger.jsonl')[0]['data']
    assert entry['rule_snapshot'] == before
    assert entry['rule_sha256'] == ledger.digest(ledger.canonical(before))
    assert entry['vetoes_sha256'] == ledger.digest(ledger.canonical(entry['vetoes_snapshot']))


@pytest.mark.parametrize('attack,message', [
    ('no_register', 'register must be first'),
    ('keep_without_evidence', 'requires at least one result'),
    ('close_without_decision', 'close requires a decision'),
    ('close_extend', 'extend'),
    ('after_close', 'entry after close'),
    ('duplicate_register', 'multiple register'),
    ('noncanonical', 'canonical'),
    ('duplicate_key', 'duplicate'),
    ('bad_utc', 'utc'),
    ('naive_utc', 'utc'),
    ('backwards_utc', 'utc'),
    ('absolute_path', 'absolute'),
])
def test_semantic_forgery(repo, attack, message):
    path = registered(repo)
    entries = ledger.read_chain(path / 'ledger.jsonl')
    sample = dict(entries[0], type='note', data={})
    if attack == 'no_register':
        entries = [sample]
    elif attack == 'keep_without_evidence':
        entries.append(dict(sample, type='decision', data={'decision': 'keep'}))
    elif attack == 'close_without_decision':
        entries.append(dict(sample, type='close'))
    elif attack in {'close_extend', 'after_close'}:
        entries += [dict(sample, type='decision', data={'decision': 'extend' if attack == 'close_extend' else 'revert'}),
                    dict(sample, type='close')]
        if attack == 'after_close':
            entries.append(sample)
    elif attack == 'duplicate_register':
        entries.append(entries[0])
    elif attack in {'bad_utc', 'naive_utc'}:
        entries[0]['utc'] = 'yesterday' if attack == 'bad_utc' else '2026-01-01T00:00:00'
    elif attack == 'backwards_utc':
        entries.append(dict(sample, utc='2000-01-01T00:00:00Z'))
    elif attack == 'absolute_path':
        entries.append(dict(sample, data={'nested': ['/private/local/path']}))
    rewrite_chain(path, entries)
    file = path / 'ledger.jsonl'
    if attack == 'noncanonical':
        file.write_bytes(b' ' + file.read_bytes())
    if attack == 'duplicate_key':
        file.write_bytes(file.read_bytes().replace(b'{"actor":', b'{"actor":"shadow","actor":', 1))
    cli(repo, 'verify', error=message)


def test_new_requires_issue(repo):
    cli(repo, 'new', '--title', 'title', '--slug', 'slug', error='--issue')


def test_verify_branch_behind_trunk(repo):
    branch = git(repo, 'rev-parse', 'HEAD')
    path = registered(repo)
    commit(repo)
    git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    git(repo, 'checkout', '--detach', branch)
    cli(repo, 'verify', '--against', 'origin/main')
    assert not path.exists()


def test_close_only_on_main_and_not_extend(repo):
    path = registered(repo)
    cli(repo, 'decide', path, '--decision', 'extend')
    cli(repo, 'close', path, '--merged-commit', 'none', '--issue', 10, error='merge time')
    git(repo, 'branch', '-m', 'main-fixture')
    git(repo, 'branch', '-D', 'main')
    git(repo, 'branch', '-m', 'main')
    cli(repo, 'close', path, '--merged-commit', 'none', '--issue', 10, error='extend')


@pytest.mark.parametrize('rule,kind,overrides,target,message', [
    ('R-infra', 'performance', {}, None, 'incompatible'),
    ('R-mech', 'infra', {}, None, 'incompatible'),
    ('R-behav', 'performance', {'delt': 1}, None, 'override'),
    ('R-behav', 'performance', {'delta': -5}, None, 'override'),
    ('R-behav', 'performance', {'delta': .51}, None, 'override'),
    ('R-disc', 'performance', {'delta': .04}, None, 'override'),
    ('R-enable', 'enabling', {'ni_margin': .16}, None, 'override'),
    ('R-enable', 'enabling', {}, None, 'target'),
    ('R-enable', 'enabling', {}, {'metric': 'tokens', 'direction': 'down', 'amount': 1}, 'target'),
])
def test_registration_rule_validation(repo, rule, kind, overrides, target, message):
    path = new(repo, kind=kind)
    fill(path)
    choice = json.loads((path / 'rule.json').read_text())
    choice.update(rule=rule, overrides=overrides)
    (path / 'rule.json').write_text(json.dumps(choice))
    if target:
        plan = json.loads((path / 'plan.json').read_text())
        plan['target'] = target
        (path / 'plan.json').write_text(json.dumps(plan))
    cli(repo, 'register', path, error=message)


def test_enable_vrg_clause_without_flag():
    rule = json.loads((ROOT / 'experiments/rules/R-enable.json').read_text())
    assert any(c.get('metric') == 'vrg' and 'when' not in c for c in rule['keep_when'])


def test_evidence_is_copied_and_verified(repo):
    path = registered(repo)
    source = result(repo, path)
    review(repo, path)
    entries = ledger.read_chain(path / 'ledger.jsonl')
    evidence = entries[1]['data']['scorecards'][0]
    assert evidence['file'].startswith(str(path.relative_to(repo)) + '/results/T0/')
    review_data = entries[-1]['data']
    assert review_data['file'].startswith(str(path.relative_to(repo)) + '/reviews/')
    source.unlink()
    cli(repo, 'verify')
    (repo / evidence['file']).write_text('tampered')
    cli(repo, 'verify', error='evidence hash mismatch')


@pytest.mark.parametrize('commit_ref', ['deadbeef', 'unrelated'])
def test_review_commit_must_exist_and_be_ancestor(repo, commit_ref):
    path = registered(repo)
    file = repo / 'REVIEW.md'
    file.write_text('review')
    if commit_ref == 'unrelated':
        commit(repo, 'registered evidence')
        head = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'checkout', '--orphan', 'unrelated')
        commit_ref = commit(repo)
        git(repo, 'checkout', '-B', 'experiment', head)
    cli(repo, 'review', path, '--kind', 'code', '--verdict', 'ACCEPT',
        '--commit', commit_ref, '--file', file, error='commit')


def test_close_rechecks_latest_review(repo):
    path = registered(repo)
    result(repo, path)
    review(repo, path)
    cli(repo, 'decide', path, '--decision', 'keep')
    review(repo, path, verdict='REJECT')
    git(repo, 'branch', '-m', 'main-fixture')
    git(repo, 'branch', '-D', 'main')
    git(repo, 'branch', '-m', 'main')
    cli(repo, 'close', path, '--merged-commit', git(repo, 'rev-parse', 'HEAD'), '--issue', 10,
        error='code review ACCEPT')


def test_crlf_chain_is_not_canonical(repo):
    path = registered(repo)
    file = path / 'ledger.jsonl'
    file.write_bytes(file.read_bytes().replace(b'\n', b'\r\n'))
    cli(repo, 'verify', error='canonical')


def test_rule_snapshot_identity_is_validated(repo):
    path = registered(repo)
    entries = ledger.read_chain(path / 'ledger.jsonl')
    data = entries[0]['data']
    data['rule_snapshot']['id'] = 'R-behav'
    data['rule_sha256'] = ledger.digest(ledger.canonical(data['rule_snapshot']))
    rewrite_chain(path, entries)
    cli(repo, 'verify', error='rule definition')


def test_code_commit_before_forged_registration(repo):
    path = registered(repo)
    registration = (path / 'ledger.jsonl').read_bytes()
    (path / 'ledger.jsonl').write_bytes(b'')
    (repo / 'feature.py').write_text('built before registration')
    commit(repo, 'build first')
    (path / 'ledger.jsonl').write_bytes(registration)
    commit(repo, 'register too late')
    cli(repo, 'verify', '--against', 'origin/main', error='feature.py')


def test_registered_history_allows_later_code(repo):
    path = registered(repo)
    commit(repo, 'register before code')
    (repo / 'feature.py').write_text('built after registration')
    commit(repo, 'build later')
    cli(repo, 'verify', '--against', 'origin/main')
    data = ledger.read_chain(path / 'ledger.jsonl')[0]['data']
    assert data['trunk'] == 'origin/main'
    assert data['base_commit'] == git(repo, 'rev-parse', 'origin/main')
    assert data['head_commit'] == data['base_commit']


def test_register_missing_trunk(repo):
    path = new(repo)
    fill(path)
    cli(repo, 'register', path, '--trunk', 'missing-trunk', error='missing-trunk')
    assert not (path / 'ledger.jsonl').read_bytes()


@pytest.mark.parametrize('rule,key,value,kind', [
    ('R-behav', 'delta', .10, 'performance'), ('R-behav', 'delta', .50, 'performance'),
    ('R-disc', 'delta', .05, 'performance'), ('R-disc', 'delta', .30, 'performance'),
    ('R-enable', 'ni_margin', .02, 'enabling'), ('R-enable', 'ni_margin', .15, 'enabling'),
])
def test_override_boundaries_and_effective_rule(repo, rule, key, value, kind):
    path = new(repo, kind=kind)
    fill(path)
    choice = json.loads((path / 'rule.json').read_text())
    choice.update(rule=rule, overrides={key: value})
    (path / 'rule.json').write_text(json.dumps(choice))
    if kind == 'enabling':
        plan = json.loads((path / 'plan.json').read_text())
        plan['target'] = dict(metric='tokens', direction='down', amount=1, tier='T1')
        plan.pop('model_free_suffices')  # Missing flag still requires VRG.
        (path / 'plan.json').write_text(json.dumps(plan))
    cli(repo, 'register', path)
    output = json.loads(cli(repo, 'rule', path))
    threshold = next(c for c in output['keep_when'] if c.get('override') == key)
    assert threshold['value'] == (-value if key == 'ni_margin' else value)
    if key == 'ni_margin':
        assert 'when' not in threshold
    assert output['vetoes']['id'] == 'VETOES'


def test_latest_decision_is_bound_at_close(repo):
    path = registered(repo)
    cli(repo, 'decide', path, '--decision', 'revert')
    older = ledger.read_chain(path / 'ledger.jsonl')[-1]['sha256']
    cli(repo, 'decide', path, '--decision', 'abandon')
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', 'none', '--issue', 10)
    entries = ledger.read_chain(path / 'ledger.jsonl')
    assert entries[-1]['data']['decision_sha256'] == entries[-2]['sha256']
    assert ledger.read_chain(repo / 'experiments/INDEX.jsonl', index=True)[0]['decision'] == 'abandon'
    entries[-1]['data']['decision_sha256'] = older
    rewrite_chain(path, entries)
    cli(repo, 'verify', error='latest decision')


def test_snapshot_digest_tampering(repo):
    path = registered(repo)
    entries = ledger.read_chain(path / 'ledger.jsonl')
    entries[0]['data']['vetoes_snapshot']['keep_when'] = []
    rewrite_chain(path, entries)
    cli(repo, 'verify', error='snapshot hash mismatch')


def test_rule_grammar_golden():
    checks = {'tests_green', 'ci_green', 'default_treatment_golden_unchanged', 'code_review',
              'practice_generators_no_harm', 'guardrails_clean', 'heldout_identifier_matches',
              'new_failure_class', 'cost_ratio', 'within_plan_cost_and_time'}
    stats = {'mean_delta', 'signflip_p', 'min_card_delta_reference_se', 'success_count_delta', 'count', 'improvement'}
    for file in (ROOT / 'experiments/rules').glob('*.json'):
        rule = json.loads(file.read_text())
        assert rule['id'] == file.stem
        assert isinstance(rule['overridable'], dict)
        if file.stem != 'VETOES':
            assert rule['inherits'] == ['VETOES']
        for c in rule['keep_when']:
            assert c.get('check') in checks | {None}
            assert c.get('stat') in stats | {None}
            assert c.get('op') in {'>=', '<=', '==', None}
            assert c.get('method') in {'exact_paired', None}
            tier = c.get('tier')
            assert tier == {'plan': 'target.tier'} or tier in {'T0', 'T1', 'T2', 'T3', 'all', 'planned', None}


def test_unittest_import_without_pytest():
    process = subprocess.run([sys.executable, '-S', '-m', 'unittest', 'discover', '-s', 'study/tests',
                              '-p', 'test_exp_ledger.py'], cwd=ROOT, text=True, capture_output=True)
    # Newer unittest versions use exit 5 for an empty (successfully imported) suite.
    assert process.returncode in {0, 5}, process.stderr
    assert 'Ran 0 tests' in process.stderr
    assert 'ImportError' not in process.stderr


def test_staged_and_unstaged_code_cannot_cancel_at_register(repo):
    (repo / 'code.py').write_text('base')
    commit(repo)
    git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    path = new(repo)
    fill(path)
    (repo / 'code.py').write_text('staged change')
    git(repo, 'add', 'code.py')
    (repo / 'code.py').write_text('base')
    cli(repo, 'register', path, error='outside experiments/: code.py')


def test_close_uses_local_branch_of_named_trunk(repo):
    git(repo, 'update-ref', 'refs/remotes/upstream/main', 'HEAD')
    path = new(repo)
    fill(path)
    cli(repo, 'register', path, '--trunk', 'upstream/main')
    cli(repo, 'decide', path, '--decision', 'abandon')
    git(repo, 'checkout', 'main')
    cli(repo, 'close', path, '--merged-commit', 'none', '--issue', 10)
    cli(repo, 'verify')


# Second-review regressions: each was run against 84255b0 before its fix.
def test_n1_review_survives_code_history_rewrite(repo):
    path = registered(repo)
    registration = commit(repo, 'registration')
    (repo / 'code.py').write_text('built')
    reviewed = commit(repo, 'original code')
    review(repo, path)
    # Preserve the public evidence while replacing the reviewed code commit.
    commit(repo, 'review evidence')
    git(repo, 'reset', '--soft', registration)
    commit(repo, 'equivalent rewritten code and review')
    assert git(repo, 'cat-file', '-t', reviewed) == 'commit'
    cli(repo, 'verify', '--against', 'origin/main')
    cli(repo, 'event', path, '--type', 'note', '--data', '{}')


@pytest.mark.parametrize('kind', ['incident', 'exposure', 'budget', 'note'])
def test_n2_event_file_is_metadata(repo, kind):
    path = registered(repo)
    cli(repo, 'event', path, '--type', kind, '--data', '{"file":"src/code.py"}')
    cli(repo, 'verify')
    cli(repo, 'status')


def test_n2_candidate_is_fully_verified_before_append(repo):
    path = registered(repo)
    card = repo / 'card.json'
    card.write_text('{"aggregate":{"duplicate":1,"duplicate":2}}')
    before = (path / 'ledger.jsonl').read_bytes()
    cli(repo, 'record', path, '--tier', 'T0', '--scorecard', card, error='duplicate JSON key')
    assert (path / 'ledger.jsonl').read_bytes() == before
    cli(repo, 'verify')


@pytest.mark.parametrize('name', ['rule', 'vetoes'])
def test_n4_snapshot_bound_to_registration_rules(repo, name):
    path = registered(repo)
    entries = ledger.read_chain(path / 'ledger.jsonl')
    data = entries[0]['data']
    data[f'{name}_snapshot']['keep_when'][0]['value'] = -5
    data[f'{name}_sha256'] = ledger.digest(ledger.canonical(data[f'{name}_snapshot']))
    rewrite_chain(path, entries)
    commit(repo, 'forged snapshot with unchanged rule file')
    cli(repo, 'verify', error=f'{name} snapshot differs from registration commit')


def test_n4_registration_ledger_prefix_is_frozen(repo):
    path = registered(repo)
    commit(repo, 'registration')
    entries = ledger.read_chain(path / 'ledger.jsonl')
    entries[0]['actor'] = 'Rewritten actor'
    rewrite_chain(path, entries)
    commit(repo, 'rewrite registration chain')
    cli(repo, 'verify', error='registration ledger prefix')


@pytest.mark.parametrize('forged', [False, True])
def test_n5_add_remove_before_registration(repo, forged):
    path = new(repo)
    fill(path)
    if forged:
        cli(repo, 'register', path)
        raw = (path / 'ledger.jsonl').read_bytes()
        (path / 'ledger.jsonl').write_bytes(b'')
    (repo / 'code.py').write_text('built before registration')
    commit(repo, 'build too early')
    git(repo, 'rm', 'code.py')
    commit(repo, 'remove early code')
    if forged:
        (path / 'ledger.jsonl').write_bytes(raw)
        commit(repo, 'forged late registration')
        cli(repo, 'verify', '--against', 'origin/main', error='code.py')
    else:
        cli(repo, 'register', path, error='code.py')
        assert not (path / 'ledger.jsonl').read_bytes()


@pytest.mark.parametrize('command', ['record', 'review', 'decide'])
@pytest.mark.parametrize('text', ['/Users/person/x', '/Volumes/disk/x', '/home/person/x',
                                  '/private/tmp/x', r'C:\\private', 'heldout/private',
                                  'eidos-grading', 'trusted-plane', 'prospective/seals', 'commitment-opening'])
def test_n6_evidence_hygiene(repo, command, text):
    path = registered(repo)
    file = repo / 'evidence.txt'
    file.write_text(json.dumps({'detail': 'See ' + text}, indent=2))
    args = {'record': ['--tier', 'T0', '--scorecard', file],
            'review': ['--kind', 'code', '--verdict', 'ACCEPT', '--commit', 'HEAD', '--file', file],
            'decide': ['--decision', 'abandon', '--decision-file', file]}[command]
    before = (path / 'ledger.jsonl').read_bytes()
    cli(repo, command, path, *args, error='line 2')
    assert (path / 'ledger.jsonl').read_bytes() == before
    assert not any((path / folder).exists() for folder in ('results', 'reviews', 'decisions'))
    cli(repo, command, path, *args, '--allow-paths')
    entry = ledger.read_chain(path / 'ledger.jsonl')[-1]
    assert entry['data']['allow_paths'] is True
    copied = entry['data']['scorecards'][0] if command == 'record' else entry['data']
    assert (repo / copied['file']).read_bytes() == file.read_bytes()
    cli(repo, 'verify')


@pytest.mark.parametrize('trunk', ['HEAD', 'experiment', 'own-alias'])
def test_n7_register_rejects_own_tip_as_trunk(repo, trunk):
    path = new(repo)
    fill(path)
    commit(repo, 'unregistered plan')
    git(repo, 'branch', 'own-alias')
    cli(repo, 'register', path, '--trunk', trunk, error='trunk')
    assert not (path / 'ledger.jsonl').read_bytes()


@pytest.mark.parametrize('decision,merged,issue,message', [
    ('keep', 'none', 10, 'keep requires an existing merged commit'),
    ('keep', 'deadbeef', 10, 'commit'),
    ('revert', 'HEAD', 10, 'requires merged commit none'),
    ('abandon', 'HEAD', 10, 'requires merged commit none'),
    ('abandon', 'none', 999, 'issue must match plan'),
])
def test_n10_close_metadata(repo, decision, merged, issue, message):
    path = registered(repo)
    if decision == 'keep':
        result(repo, path)
        review(repo, path)
    cli(repo, 'decide', path, '--decision', decision)
    before = (path / 'ledger.jsonl').read_bytes()
    cli(repo, 'close', path, '--allow-branch', '--merged-commit', merged, '--issue', issue, error=message)
    assert (path / 'ledger.jsonl').read_bytes() == before
    assert not (repo / 'experiments/INDEX.jsonl').read_bytes()
    cli(repo, 'verify')


@pytest.mark.parametrize('before_kind', ['missing', 'diverged', 'ancestor', 'zero', 'empty'])
@pytest.mark.parametrize('unmerged', [False, True])
def test_n3_previous_push_history(repo, before_kind, unmerged):
    path = registered(repo)
    registration = commit(repo, 'registration')
    if not unmerged:
        git(repo, 'update-ref', 'refs/remotes/origin/main', registration)
    before = {'missing': 'f' * 40, 'ancestor': registration, 'zero': '0' * 40, 'empty': ''}.get(before_kind)
    if before_kind == 'diverged':
        git(repo, 'checkout', '-b', 'old-tip')
        (path / 'old-note').write_text('previous branch tip')
        before = commit(repo, 'old branch tip')
        git(repo, 'checkout', 'experiment')
    error = ('history of a registered, unmerged experiment was rewritten'
             if unmerged and before_kind in {'missing', 'diverged'} else None)
    cli(repo, 'previous-push', '--before', before, error=error)


def test_n3_previous_push_still_checks_prefix(repo):
    path = registered(repo)
    cli(repo, 'event', path, '--type', 'note', '--data', '{}')
    previous = commit(repo, 'previous push with note')
    entries = ledger.read_chain(path / 'ledger.jsonl')
    rewrite_chain(path, entries[:1])
    commit(repo, 'truncate pushed note')
    cli(repo, 'previous-push', '--before', previous, error='append-only prefix')


def test_n8_history_constraints_documented():
    text = (ROOT / 'experiments/README.md').read_text().lower()
    for phrase in ('no squash', 'no rebase', 'no force-push', 'nothing outside `experiments/`',
                   'external precondition', 'merge-gate'):
        assert phrase in text


@pytest.mark.parametrize('trunk_merge', [False, True])
def test_n5_pre_registration_merge_policy(repo, trunk_merge):
    path = new(repo)
    fill(path)
    commit(repo, 'plan draft')
    git(repo, 'checkout', 'main' if trunk_merge else '-b', *([] if trunk_merge else ['side']))
    (repo / 'experiments/side-note').write_text('side branch record')
    commit(repo, 'side record')
    if trunk_merge:
        git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    git(repo, 'checkout', 'experiment')
    git(repo, 'merge', '--no-ff', 'main' if trunk_merge else 'side', '-m', 'merge before registration')
    cli(repo, 'register', path, error=None if trunk_merge else 'only trunk merges')
    if trunk_merge:
        commit(repo, 'registration after trunk merge')
        cli(repo, 'verify', '--against', 'origin/main')


def test_n6_allow_paths_preserves_aggregate_paths(repo):
    path = registered(repo)
    card = repo / 'card.json'
    card.write_text(json.dumps({'aggregate': {'source': '/home/person/data'}}))
    cli(repo, 'record', path, '--tier', 'T0', '--scorecard', card, '--allow-paths')
    cli(repo, 'verify')
    assert ledger.read_chain(path / 'ledger.jsonl')[-1]['data']['scorecards'][0]['aggregate'] == {
        'source': '/home/person/data'}


def test_n6_embedded_path_in_note_is_refused_before_append(repo):
    path = registered(repo)
    before = (path / 'ledger.jsonl').read_bytes()
    cli(repo, 'event', path, '--type', 'note', '--data', '{"note":"see /Volumes/disk/local"}', error='absolute')
    assert (path / 'ledger.jsonl').read_bytes() == before
