"""Regenerate fixture-only files for the PyTorch components (author tests, not qualification).

Run with the project/torch interpreter and the SDK on PYTHONPATH, from the repository root:

    PYTHONPATH=packages/eidos-wire/src:packages/eidos-worker-sdk/src:packages/eidos-pytorch/src \
        $TORCH_PY components/build_fixtures.py

It copies the switch example (model code, scorer, adapter) into each component's ``fixtures/selftest``
so a published bundle validates without the repository, trains two tiny checkpoints with the real
``eidos.pytorch.train`` component, and writes the JSON fixtures.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from eidos_wire import OutputManifest
from eidos_worker.testing import make_invocation

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "components"
EXAMPLE = ROOT / "examples" / "switch-order-project"
TORCH_COMPONENTS = [
    "eidos.pytorch.train",
    "eidos.pytorch.evaluate",
    "eidos.pytorch.weight-edit",
    "eidos.pytorch.behavior-check",
]
ADAPTER = "eidos_adapter:ADAPTER"


def run_component(name, entry, config, inputs=None):
    tmp = tempfile.mkdtemp(prefix="eidos-fx-")
    inv = make_invocation(tmp, entry, inputs=inputs, config=config, source_dir=COMP / name)
    path = Path(tmp) / "invocation.json"
    inv.write(path)
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(os.path.abspath(p) for p in os.environ["PYTHONPATH"].split(os.pathsep)),
    )
    subprocess.run(
        [sys.executable, "-m", "eidos_worker.run", str(path)],
        check=True,
        capture_output=True,
        env=env,
        cwd=EXAMPLE,
    )
    out = OutputManifest.read(inv.staging_dir)
    assert out.status == "succeeded", out.error
    return inv, out


def train_checkpoint(builder, steps, seed, dest):
    inv, out = run_component(
        "eidos.pytorch.train",
        "eidos_train.train:run",
        {"adapter": ADAPTER, "builder": builder, "steps": steps, "seed": seed},
    )
    entry = next(e for e in out.outputs if e.slot == "checkpoint")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(inv.staging_dir) / entry.filename, dest)
    return out.receipt["checkpoints"]["checkpoint"]["state_entries"]


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


def fixture(name, inputs, config, expect):
    return {"name": name, "author_test": True, "inputs": inputs, "config": config, "expect": expect}


def main():
    for name in TORCH_COMPONENTS:
        dest = COMP / name / "fixtures" / "selftest"
        dest.mkdir(parents=True, exist_ok=True)
        for f in ("switch_order.py", "pair_report.py", "eidos_adapter.py"):
            shutil.copyfile(EXAMPLE / f, dest / f)
    entries = train_checkpoint(
        "OrderedSwitchNet", 6, 5, COMP / "eidos.pytorch.train/fixtures/data/parent-step6.ckpt"
    )
    for name in ("eidos.pytorch.evaluate", "eidos.pytorch.weight-edit", "eidos.pytorch.behavior-check"):
        train_checkpoint("OrderedSwitchNet", 20, 5, COMP / name / "fixtures/data/parent-ordered-step20.ckpt")
    path = ["fixtures/selftest"]
    ckpt6 = {
        "parent_checkpoint": {
            "file": "fixtures/data/parent-step6.ckpt",
            "media_type": "application/octet-stream",
        }
    }
    base = {"adapter": ADAPTER, "python_path": path}
    t = COMP / "eidos.pytorch.train" / "fixtures"
    write(
        t / "train-from-scratch.json",
        fixture(
            "train-from-scratch",
            {},
            dict(base, builder="OrderedSwitchNet", steps=8, seed=3, checkpoint_every=4),
            {
                "status": "succeeded",
                "receipt": {
                    "end_step": 8,
                    "updates": 8,
                    "examples": 800,
                    "restore_class": "exact",
                    "resumed_from_step": None,
                },
            },
        ),
    )
    migration = {e: "copy" for e in entries}
    write(
        t / "branch-no-change-exact.json",
        fixture(
            "branch-no-change-exact",
            ckpt6,
            dict(
                base,
                builder="OrderedSwitchNet",
                steps=12,
                seed=5,
                verify_no_change=True,
                branch={
                    "migration": migration,
                    "stream_policy": "paired",
                    "seed": None,
                    "intervention": {"kind": "none"},
                    "restore_class": "exact",
                    "required_fixtures": ["no-change-restore"],
                },
            ),
            {
                "status": "succeeded",
                "receipt": {
                    "resumed_from_step": 6,
                    "end_step": 12,
                    "updates": 6,
                    "restore_class": "exact",
                    "no_change_restore_match": True,
                },
            },
        ),
    )
    undeclared = {k: v for k, v in migration.items() if k != "optimizer"}
    write(
        t / "branch-undeclared-state.json",
        fixture(
            "branch-undeclared-state",
            ckpt6,
            dict(
                base,
                builder="OrderedSwitchNet",
                steps=12,
                seed=5,
                branch={"migration": undeclared, "stream_policy": "paired"},
            ),
            {"status": "failed", "error_category": "user-code"},
        ),
    )
    write(
        t / "missing-adapter.json",
        fixture("missing-adapter", {}, {"steps": 3}, {"status": "failed", "error_category": "contract"}),
    )

    ck = {
        "checkpoint": {
            "file": "fixtures/data/parent-ordered-step20.ckpt",
            "media_type": "application/octet-stream",
        }
    }
    e = COMP / "eidos.pytorch.evaluate" / "fixtures"
    write(
        e / "evaluate-small.json",
        fixture(
            "evaluate-small",
            ck,
            dict(base, evaluation={"split": "test", "data_seed": 5, "pairs": 50}),
            {
                "status": "succeeded",
                "receipt": {"rows": 50, "split": "test", "cluster": "5", "input_checkpoint_step": 20},
            },
        ),
    )
    write(
        e / "evaluate-unknown-builder.json",
        fixture(
            "evaluate-unknown-builder",
            ck,
            dict(base, builder="NoSuchNet"),
            {"status": "failed", "error_category": "user-code"},
        ),
    )
    w = COMP / "eidos.pytorch.weight-edit" / "fixtures"
    write(
        w / "zero-recorder-weight.json",
        fixture(
            "zero-recorder-weight",
            ck,
            {"edits": [{"entry": "readout.weight", "index": [0, -1], "op": "set", "value": 0.0}]},
            {
                "status": "succeeded",
                "receipt": {
                    "parent_unchanged": True,
                    "storage_independent": True,
                    "restore_class": "warm-start",
                },
            },
        ),
    )
    write(
        w / "unknown-entry.json",
        fixture(
            "unknown-entry",
            ck,
            {"edits": [{"entry": "readout.nothing", "value": 0.0}]},
            {"status": "failed", "error_category": "contract"},
        ),
    )
    b = COMP / "eidos.pytorch.behavior-check" / "fixtures"
    write(
        b / "wrong-builder.json",
        fixture(
            "wrong-builder",
            ck,
            dict(base, builder="SwitchNet"),
            {
                "status": "succeeded",
                "receipt": {
                    "behavior_match": False,
                    "strict_load_ok": True,
                    "builder": "SwitchNet",
                    "reference_builder": "OrderedSwitchNet",
                },
            },
        ),
    )
    write(
        b / "right-builder.json",
        fixture(
            "right-builder",
            ck,
            dict(base, builder="OrderedSwitchNet"),
            {
                "status": "succeeded",
                "receipt": {"behavior_match": True, "strict_load_ok": True, "builder": "OrderedSwitchNet"},
            },
        ),
    )


if __name__ == "__main__":
    main()
