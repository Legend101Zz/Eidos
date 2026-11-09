"""Helpers for the built-in component tests (author tests, not independent qualification).

PyTorch components run in the *project* interpreter (``EIDOS_TORCH_PYTHON``, default the shared
Python 3.12 + torch env) through the real ``python -m eidos_worker.run`` protocol; lab components run in
the interpreter that runs pytest (the lab environment).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from eidos_wire import OutputManifest
from eidos_worker.testing import make_invocation

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "components"
SWITCH = ROOT / "examples" / "switch-order-project"
IMAGE = ROOT / "examples" / "image-project"
TORCH_PY = os.environ.get("EIDOS_TORCH_PYTHON", "/workspace/.eidos-dev/h1/envs/py312/bin/python")
SDK_PATH = os.pathsep.join(
    str(ROOT / "packages" / p / "src") for p in ("eidos-wire", "eidos-worker-sdk", "eidos-pytorch")
)
CKPT = "application/octet-stream"


@dataclass
class Result:
    manifest: OutputManifest
    invocation: Any
    stderr: str

    @property
    def ok(self) -> bool:
        return self.manifest.status == "succeeded"

    @property
    def receipt(self) -> dict[str, Any]:
        return self.manifest.receipt

    def path(self, slot: str) -> Path:
        entry = next(e for e in self.manifest.outputs if e.slot == slot)
        return Path(self.invocation.staging_dir) / entry.filename

    def json(self, slot: str) -> Any:
        return json.loads(self.path(slot).read_text())

    def text(self, slot: str) -> str:
        return self.path(slot).read_text()

    def slots(self):
        return [e.slot for e in self.manifest.outputs]


def run_component(
    tmp_path: Path,
    name: str,
    entry: str,
    *,
    inputs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    python: str | None = None,
    cwd: Path | None = None,
    ports: dict[str, Any] | None = None,
    label: str = "run",
) -> Result:
    root = tmp_path / label
    inv = make_invocation(
        root,
        entry,
        inputs=inputs,
        config=config,
        source_dir=COMPONENTS / name,
        ports=ports,
        component_id=name,
    )
    path = root / "invocation.json"
    inv.write(path)
    env = dict(os.environ)
    py = python or sys.executable
    if python is not None:
        env["PYTHONPATH"] = SDK_PATH
    proc = subprocess.run(
        [py, "-m", "eidos_worker.run", str(path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=240,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return Result(OutputManifest.read(inv.staging_dir), inv, proc.stderr)


@pytest.fixture(scope="session")
def torch_py() -> str:
    if not Path(TORCH_PY).exists():
        pytest.skip(f"no torch interpreter at {TORCH_PY} (set EIDOS_TORCH_PYTHON)")
    return TORCH_PY


@pytest.fixture()
def torch_run(torch_py, tmp_path):
    """Run a PyTorch component in the project interpreter with the switch example as project root."""
    counter = {"n": 0}

    def go(name: str, entry: str, *, project: Path = SWITCH, **kw) -> Result:
        counter["n"] += 1
        return run_component(
            tmp_path, name, entry, python=torch_py, cwd=project, label=f"r{counter['n']}", **kw
        )

    return go


@pytest.fixture()
def lab_run(tmp_path):
    counter = {"n": 0}

    def go(name: str, entry: str, **kw) -> Result:
        counter["n"] += 1
        return run_component(tmp_path, name, entry, label=f"l{counter['n']}", **kw)

    return go


ADAPTER = "eidos_adapter:ADAPTER"
TRAIN = ("eidos.pytorch.train", "eidos_train.train:run")
EVALUATE = ("eidos.pytorch.evaluate", "eidos_evaluate.evaluate:run")
WEIGHT_EDIT = ("eidos.pytorch.weight-edit", "eidos_weight_edit.edit:run")
BEHAVIOR = ("eidos.pytorch.behavior-check", "eidos_behavior.check:run")
SCORER = ("lab.pairs.by_group", "pair_tools.report:run")
EXTENSION_PATH = str(COMPONENTS / "lab.learning.group-balanced-ema")
GROUP_EMA = {"spec": "group_ema.rule:create", "name": "group-balanced-ema", "config": {"decay": 0.9}}


def train_config(builder="OrderedSwitchNet", steps=45, seed=41, **extra) -> dict[str, Any]:
    return {"adapter": ADAPTER, "builder": builder, "steps": steps, "seed": seed, **extra}


def ckpt_input(result: Result, slot: str = "checkpoint", name: str = "checkpoint"):
    return {name: (result.path(slot), CKPT)}
