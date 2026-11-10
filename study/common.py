"""Shared paths, atomic records and the agent filesystem boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "study"
DEVELOPMENT = ROOT / "research/public/development"
MODEL = "MiniMax-M3.1-Flash"
ENVELOPE = {"wall_s": 1800, "tokens": 8_000_000, "threads": 1}
FAMILIES = {"D01": "iris", "D02": "digits", "D12": "duplicate-input-null"}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def research_python():
    path = os.environ.get("EIDOS_RESEARCH_PYTHON")
    if not path or not Path(path).is_file():
        raise RuntimeError("Source /Volumes/Mrigesh SSD/eidos-v1/env.sh; EIDOS_RESEARCH_PYTHON is required")
    return path


def agent_env(run_dir):
    """A clean environment: no inherited lab secrets, repo imports or user configuration."""
    root = Path(run_dir).resolve()
    for name in (".claude-cfg", ".home", ".tmp", ".cache"):
        (root / name).mkdir(exist_ok=True)
    node = Path(shutil.which("node") or "/usr/bin/node").resolve()
    env = {k: os.environ[k] for k in ("LANG", "LC_ALL", "TERM", "USER") if k in os.environ}
    env.update(
        {
            "HOME": str(root / ".home"),
            "TMPDIR": str(root / ".tmp"),
            "CLAUDE_CODE_TMPDIR": str(root / ".tmp"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "XDG_CONFIG_HOME": str(root / ".home"),
            "CLAUDE_CONFIG_DIR": str(root / ".claude-cfg"),
            "PATH": os.pathsep.join(
                [
                    str(Path(research_python()).parent),
                    str(node.parent),
                    "/opt/homebrew/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            ),
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return env


def sandbox_command(run_dir, command, *, network=True, readonly=()):
    """Public helper for arms 1–3. Parameters avoid profile string interpolation."""
    python = research_python()
    prefix = subprocess.check_output([python, "-c", "import sys; print(sys.prefix)"], text=True).strip()
    node = Path(shutil.which("node") or "/usr/bin/node").resolve().parent.parent
    extra = "" if network else "\n(deny network*)\n"
    readonly_params = []
    for index, path in enumerate(readonly):
        extra += f'\n(deny file-write* (subpath (param "READONLY{index}")))\n'
        readonly_params += ["-D", f"READONLY{index}=" + str(Path(path).resolve())]
    profile = (
        ["-p", (STUDY / "sandbox/agent.sb").read_text() + extra]
        if extra
        else ["-f", str(STUDY / "sandbox/agent.sb")]
    )
    return [
        "/usr/bin/sandbox-exec",
        *profile,
        "-D",
        "RUN_DIR=" + str(Path(run_dir).resolve()),
        "-D",
        "PYTHON_PREFIX=" + str(Path(prefix).resolve()),
        "-D",
        "NODE_PREFIX=" + str(node),
        *readonly_params,
        *map(str, command),
    ]


def render_prompt(arm):
    paragraph = (STUDY / f"prompts/tools_arm{arm}.md").read_text().strip()
    if "PLACEHOLDER" in paragraph:
        raise NotImplementedError(f"arm {arm} tools paragraph not implemented")
    return (STUDY / "prompts/task.md").read_text().replace("{{TOOLS_PARAGRAPH}}", paragraph)


def dev_command(script, *args, **kwargs):
    return subprocess.run(
        [research_python(), str(DEVELOPMENT / script), *map(str, args)],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
        **kwargs,
    )
