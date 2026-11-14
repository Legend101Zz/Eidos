"""Workflow contracts; a minimal line parser is used when PyYAML is unavailable."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/ci.yml'


def jobs_and_gate():
    text = WORKFLOW.read_text()
    # This workflow uses two-space job keys and six-space block needs lists.
    # Parse only the jobs map, so top-level env/permissions are never job ids.
    body = text.split('\njobs:\n', 1)[1]
    ids = set(re.findall(r'^  ([a-z][a-z0-9-]*):$', body, re.MULTILINE))
    gate = body.split('  merge-gate:\n', 1)[1]
    try:
        import yaml
    except ImportError:
        needs_block = gate.split('    needs:\n', 1)[1].split('    steps:\n', 1)[0]
        needs = re.findall(r'^      - ([a-z][a-z0-9-]*)$', needs_block, re.MULTILINE)
    else:
        parsed = yaml.safe_load(text)['jobs']
        assert set(parsed) == ids
        needs = parsed['merge-gate']['needs']
    return ids, gate, needs


def test_gate_depends_on_every_other_job():
    ids, gate, needs = jobs_and_gate()
    assert set(needs) == ids - {'merge-gate'}
    assert len(needs) == len(set(needs))
    assert "    if: always() && github.event_name == 'push'" in gate
    assert '    runs-on: ubuntu-24.04' in gate
    assert 'checkout' not in gate
    assert gate.count('      - name:') == 1
    assert 'permissions:\n  contents: read\n' in WORKFLOW.read_text()


def test_no_workflow_secrets():
    for path in (ROOT / '.github/workflows').glob('*'):
        if path.suffix in {'.yaml', '.yml'}:
            assert 'secrets.' not in path.read_text(), path


@pytest.mark.parametrize('results,success', [
    ({'unit': 'success', 'integration': 'success'}, True),
    ({'unit': 'failure'}, False), ({'unit': 'cancelled'}, False),
    ({'unit': 'skipped'}, False), ({}, False),
])
def test_gate_executes_and_prints_results(results, success):
    _, gate, _ = jobs_and_gate()
    # Extract the actual Python heredoc from the workflow to verify its behavior.
    script = gate.split("python3 - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0]
    script = '\n'.join(line[10:] for line in script.splitlines())
    env = dict(os.environ, JOB_RESULTS=json.dumps({name: {'result': value} for name, value in results.items()}))
    process = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True)
    assert process.returncode == (0 if success else 1), process.stderr
    assert '| Job | Result |' in process.stdout
    for name, value in results.items():
        assert f'| {name} | {value} |' in process.stdout


def test_guards_and_full_history():
    lint = WORKFLOW.read_text().split('  lint-and-unit:\n', 1)[1].split('\n  integration:', 1)[0]
    assert 'fetch-depth: 0' in lint
    assert lint.index('name: Kernel guard') > lint.index('name: shellcheck')
    assert 'uv run python scripts/ci/kernel_guard.py --base "$base"' in lint
    assert 'EVENT_BEFORE: ${{ github.event.before }}' in lint
    assert 'base=HEAD~1' in lint and '^0+$' in lint
    assert 'uv run python -m study.exp.ledger verify --against origin/main' in lint
    assert 'uv run python -m study.exp.ledger verify\n' in lint
    assert 'if [ -f study/exp/flags.py ]; then' in lint
    assert 'uv run python -m study.exp.flags check' in lint


@pytest.mark.parametrize('ref,before,expected', [
    ('refs/heads/task/example', 'a' * 40, 'origin/main'),
    ('refs/heads/main', 'b' * 40, 'b' * 40),
    ('refs/heads/main', '0' * 40, 'HEAD~1'),
    ('refs/heads/main', '', 'HEAD~1'),
])
def test_kernel_guard_base_selection(ref, before, expected):
    block = WORKFLOW.read_text().split('      - name: Kernel guard\n', 1)[1]
    block = block.split('      - name: Experiment ledger', 1)[0]
    script = block.split('        run: |\n', 1)[1]
    script = '\n'.join(line[10:] for line in script.splitlines())
    env = dict(os.environ, GITHUB_REF=ref, EVENT_BEFORE=before)
    process = subprocess.run(['bash', '-c', 'uv() { printf "%s\\n" "$*"; }\n' + script],
                             env=env, text=True, capture_output=True)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == f'run python scripts/ci/kernel_guard.py --base {expected}'


def test_ledger_tests_are_in_merge_gate():
    lint = WORKFLOW.read_text().split('  lint-and-unit:\n', 1)[1].split('\n  integration:', 1)[0]
    assert 'uv run pytest study/tests/test_exp_ledger.py -q' in lint


def test_previous_push_is_verified_when_available():
    block = WORKFLOW.read_text().split('      - name: Experiment ledger\n', 1)[1].split('      - run:', 1)[0]
    assert 'EVENT_BEFORE: ${{ github.event.before }}' in block
    assert 'git cat-file -e "$EVENT_BEFORE^{commit}"' in block
    assert 'verify --against "$EVENT_BEFORE"' in block
    assert '${{' not in block.split('        run: |', 1)[1]


def test_gate_uses_python3():
    assert "python3 - <<'PYTHON'" in jobs_and_gate()[1]


@pytest.mark.parametrize('before,available,compared', [
    ('a' * 40, True, True), ('a' * 40, False, False), ('0' * 40, True, False), ('', True, False),
])
def test_previous_push_runtime(before, available, compared):
    block = WORKFLOW.read_text().split('      - name: Experiment ledger\n', 1)[1].split('      - name:', 1)[0]
    script = block.split('        run: |\n', 1)[1]
    script = '\n'.join(line[10:] for line in script.splitlines())
    env = dict(os.environ, GITHUB_REF='refs/heads/task/example', EVENT_BEFORE=before)
    stubs = 'uv() { printf "%s\\n" "$*"; }\ngit() { return ' + ('0' if available else '1') + '; }\n'
    process = subprocess.run(['bash', '-e', '-c', stubs + script], cwd=ROOT,
                             env=env, text=True, capture_output=True)
    assert process.returncode == 0, process.stderr
    assert (f'verify --against {before}\n' in process.stdout) == compared
