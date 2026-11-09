"""Diagnostics for linux:bwrap-sealed on this host (run as root in the separate-identity CI job).
Prints the bwrap argv the launcher builds and the result of running a trivial interpreter command under it,
as the lab identity and as the candidate identity. Never fails the job by itself."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from eidos.runtime.launcher import LaunchSpec, bwrap_argv, candidate_uid
from eidos.runtime.runner import python_prefixes

CAND_PY = os.environ.get("EIDOS_CANDIDATE_PYTHON", "/opt/eidos/envs/sealed-py312/bin/python")
base = Path(tempfile.mkdtemp(prefix="eidos-diag-", dir="/tmp"))
os.chmod(base, 0o755)
for py in (CAND_PY, sys.executable):
    for identity in ("lab", "candidate"):
        spec = LaunchSpec(argv=[py, "-I", "-c", "import os,sys; print('ok', os.getuid(), sys.prefix)"], cwd=str(base),
                          attempt_dir=str(base / "a"), profile="linux:bwrap-sealed", python_prefixes=python_prefixes(py),
                          identity=identity)
        argv = bwrap_argv(spec)
        ids = candidate_uid(py) if identity == "candidate" else None

        def pre(ids=ids):
            if ids:
                os.setgroups([])
                os.setgid(ids[1])
                os.setuid(ids[0])
        r = subprocess.run(argv, capture_output=True, text=True, preexec_fn=pre, env={"PATH": "/usr/bin:/bin"})
        print(f"== {py} as {identity} (ids={ids}) rc={r.returncode}\n  stdout={r.stdout.strip()!r}\n"
              f"  stderr={r.stderr.strip()[-600:]!r}\n  prefixes={python_prefixes(py)}")
print("ls -la /opt/eidos/envs/sealed-py312/bin:", subprocess.run(["ls", "-la", "/opt/eidos/envs/sealed-py312/bin"],
                                                                  capture_output=True, text=True).stdout)
print("readlink -f:", os.path.realpath(CAND_PY))
