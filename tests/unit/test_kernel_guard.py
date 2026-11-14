"""Kernel trailers must belong to a non-merge commit that touches the kernel."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/ci/kernel_guard.py'


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def change(root, path, message):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(message)
    git(root, 'add', '.')
    git(root, 'commit', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-b', 'main')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    change(tmp_path, 'README.md', 'base')
    git(tmp_path, 'tag', 'base')
    return tmp_path


def guard(root, passed, head='HEAD'):
    result = subprocess.run([sys.executable, str(SCRIPT), '--base', 'base', '--head', head],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == (0 if passed else 1), result.stdout + result.stderr
    return result.stdout


def test_no_kernel_change(repo):
    change(repo, 'src/ordinary.py', 'ordinary change')
    assert 'no kernel paths' in guard(repo, True)


@pytest.mark.parametrize('message,passed', [
    ('change without trailer', False), ('change\n\nKernel-Change: reason', True),
    ('change\nKernel-Change:   ', False), ('change\nKernel-Change:\nOther text', False),
    ('change\n Kernel-Change: indented', False),
])
def test_kernel_change(repo, message, passed):
    change(repo, 'src/eidos/domain/assessment.py', message)
    assert 'src/eidos/domain/assessment.py' in guard(repo, passed)


def test_unrelated_trailer_does_not_count(repo):
    change(repo, 'README.md', 'unrelated\n\nKernel-Change: not the kernel')
    change(repo, 'study/fresh_check.py', 'kernel with no trailer')
    guard(repo, False)


@pytest.mark.parametrize('path', ['study/sandbox/nested/probe.py',
                                 'components/eidos.assessors.paired-ci/nested/contract.py'])
def test_prefix_directories(repo, path):
    change(repo, path, 'directory kernel\n\nKernel-Change: explain')
    assert path in guard(repo, True)


@pytest.mark.parametrize('trailer_on_child', [True, False])
def test_merge_commit_in_range(repo, trailer_on_child):
    git(repo, 'checkout', '-b', 'feature')
    change(repo, 'study/grade_block.py', 'kernel' + ('\n\nKernel-Change: child reason' if trailer_on_child else ''))
    git(repo, 'checkout', 'main')
    change(repo, 'README.md', 'main change')
    git(repo, 'merge', '--no-ff', 'feature', '-m', 'merge\n\nKernel-Change: merge alone does not count')
    guard(repo, trailer_on_child)


def test_rename_out_of_kernel_still_counts(repo):
    change(repo, 'study/private_seal.py', 'initial')
    git(repo, 'tag', '-f', 'base')
    git(repo, 'mv', 'study/private_seal.py', 'ordinary.py')
    git(repo, 'commit', '-m', 'rename without explanation')
    guard(repo, False)


def test_explicit_head(repo):
    earlier = change(repo, 'README.md', 'ordinary')
    change(repo, 'src/eidos/domain/evidence.py', 'kernel')
    guard(repo, True, earlier)


def test_every_guarded_commit_needs_own_trailer(repo):
    change(repo, 'study/fresh_check.py', 'first\n\nKernel-Change: reason')
    change(repo, 'study/fresh_check.py', 'second without trailer')
    guard(repo, False)


def test_subject_is_not_body_trailer(repo):
    change(repo, 'study/fresh_check.py', 'Kernel-Change: subject only')
    guard(repo, False)


@pytest.mark.parametrize('path', [
    'experiments/rules/R-mech.json', 'study/exp/ledger.py', 'study/exp/compare.py',
    'study/exp/scorecard.py', 'study/exp/stats.py', 'study/exp/novelty.py', 'study/exp/traces.py',
    'study/exp/cards/reference.py', 'study/exp/cards/reference_runtime.py',
    'study/exp/cards/qualify.py', 'study/exp/cards/baseline_project.py',
    'scripts/ci/kernel_guard.py', 'scripts/ci/guard_grandfathered.txt', '.github/workflows/ci.yml',
])
def test_measurement_paths_require_trailer(repo, path):
    change(repo, path, 'unguarded measurement change')
    guard(repo, False)


def test_measurement_body_trailer(repo):
    change(repo, 'study/exp/ledger.py', 'change\n\nMeasurement-Change: freeze rules')
    guard(repo, True)


def test_grandfathered_commit(repo):
    sha = change(repo, 'study/exp/ledger.py', 'old change')
    file = repo / 'scripts/ci/guard_grandfathered.txt'
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(sha + '  # introduces guard\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'seed exemptions\n\nMeasurement-Change: seed exemptions')
    guard(repo, True)
