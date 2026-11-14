"""Require a Kernel-Change message line on a commit that changes the kernel."""

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


def check(base: str, head: str = 'HEAD', *, root: Path | None = None) -> bool:
    root = root or Path.cwd()
    paths = kernel_files(git(root, 'diff', '--no-renames', '--name-only', '-z', f'{base}...{head}'))
    if not paths:
        print(f'Kernel guard: no kernel paths changed in {base}...{head}')
        return True
    print('Kernel paths changed:\n' + '\n'.join(f'  {p}' for p in paths))
    commits = git(root, 'rev-list', '--no-merges', f'{base}..{head}').splitlines()
    for commit in commits:
        touched = kernel_files(git(root, 'diff-tree', '--root', '--no-commit-id', '--no-renames',
                                   '--name-only', '-z', '-r', commit))
        if touched:
            message = git(root, 'show', '-s', '--format=%B', commit)
            trailer = re.search(r'^Kernel-Change:[ \t]*\S[^\r\n]*', message, re.MULTILINE)
            if trailer:
                print(f'Kernel guard: accepted {commit}: {trailer[0]}')
                return True
    print('Kernel guard: missing nonempty Kernel-Change: line on a non-merge commit touching a kernel path')
    return False


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
