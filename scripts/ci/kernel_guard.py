"""Require a matching body trailer on every commit touching a guarded class."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

KERNEL_PATHS = (
    'src/eidos/domain/assessment.py',
    'src/eidos/domain/evidence.py',
    'src/eidos/domain/charter.py',
    'src/eidos/domain/qualification.py',
    'src/eidos/domain/release_guards.py',
    'src/eidos/application/handlers/finalization_evidence.py',
    'src/eidos/application/handlers/assessment.py',
    'src/eidos/application/handlers/evidence.py',
    'components/eidos.assessors.paired-ci/',
    'study/grade_block.py',
    'study/fresh_check.py',
    'study/private_seal.py',
    'study/sandbox/',
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout


def kernel_files(output: str) -> list[str]:
    return sorted(p for p in output.split('\0') if p and p.startswith(KERNEL_PATHS))


MEASUREMENT_PATHS = (
    'experiments/rules/', 'study/exp/ledger.py', 'study/exp/compare.py',
    'study/exp/scorecard.py', 'study/exp/stats.py', 'study/exp/novelty.py', 'study/exp/traces.py',
    'study/exp/cards/reference.py', 'study/exp/cards/reference_runtime.py',
    'study/exp/cards/qualify.py', 'study/exp/cards/baseline_project.py',
    'scripts/ci/kernel_guard.py', 'scripts/ci/guard_grandfathered.txt', '.github/workflows/',
)
GUARD_CLASSES = {'Kernel-Change': KERNEL_PATHS, 'Measurement-Change': MEASUREMENT_PATHS}


def grandfathered(root: Path, base: str) -> set[str]:
    result = subprocess.run(['git', '-C', str(root), 'show', f'{base}:scripts/ci/guard_grandfathered.txt'],
                            capture_output=True, text=True)
    exempt = set()
    if result.returncode:
        return exempt
    for line in result.stdout.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([0-9a-f]{40})[ \t]+#[ \t]*\S.*', line)
        if not match:
            raise ValueError('guard_grandfathered.txt: expected full commit SHA and # reason')
        exempt.add(match[1])
    return exempt


def check(base: str, head: str = 'HEAD', *, root: Path | None = None) -> bool:
    root = root or Path.cwd()
    exempt = grandfathered(root, head)
    commits = git(root, 'rev-list', '--no-merges', f'{base}..{head}').splitlines()
    changed = git(root, 'diff', '--no-renames', '--name-only', '-z', f'{base}...{head}').split('\0')
    covered = {name: set() for name in GUARD_CLASSES}
    passed = True
    any_guarded = False
    for commit in commits:
        touched = git(root, 'diff-tree', '--root', '--no-commit-id', '--no-renames',
                      '--name-only', '-z', '-r', commit).split('\0')
        body = git(root, 'show', '-s', '--format=%b', commit)
        for trailer, prefixes in GUARD_CLASSES.items():
            paths = [p for p in touched if p and p.startswith(prefixes)]
            if not paths:
                continue
            any_guarded = True
            covered[trailer].update(paths)
            print(f'{trailer} paths in {commit}:\n' + '\n'.join(f'  {p}' for p in paths))
            if commit in exempt:
                print(f'Guard: grandfathered {commit}')
            elif re.search(rf'^{trailer}:[ \t]*[^\s][^\r\n]*', body, re.MULTILINE):
                print(f'Guard: accepted {commit}: {trailer}')
            else:
                print(f'Guard: missing nonempty {trailer}: line in commit BODY: {commit}')
                passed = False
    for trailer, prefixes in GUARD_CLASSES.items():
        uncovered = [p for p in changed if p and p.startswith(prefixes) and p not in covered[trailer]]
        if uncovered:
            print(f'Guard: {trailer} paths changed only by a merge; no non-merge trailer counts: ' + ', '.join(uncovered))
            passed = False
    if not any_guarded:
        print(f'Kernel guard: no kernel paths or measurement paths changed in {base}...{head}')
    return passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', default='HEAD')
    args = parser.parse_args(argv)
    try:
        return 0 if check(args.base, args.head) else 1
    except ValueError as exc:
        print(f'Kernel guard: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
