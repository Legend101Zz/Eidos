#!/usr/bin/env python3
"""Run an exported component through the real worker invocation protocol."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    sources = [root / "support"] + [
        root / "packages" / p / "src" for p in ("eidos-wire", "eidos-worker-sdk", "eidos-pytorch")
    ]
    # Earlier releases kept for pinned references (<id>@<release>/) share module names with the current release: the
    # worker puts the invoked component's own directory first, so only current releases go on the shared path.
    sources += sorted(p.parent for p in (root / "components").glob("*/eidos-component.json") if "@" not in p.parent.name)
    sys.path[:0] = list(map(str, sources))
    from eidos_wire import OutputManifest
    from eidos_worker.testing import make_invocation

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", nargs="?")
    parser.add_argument("--args", default="{}", help="JSON: config, inputs, seed, limits, ports")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    catalog = {p.parent.name: p for p in (root / "components").glob("*/eidos-component.json")}
    if args.list:
        print("\n".join(sorted(catalog)))
        return 0
    if args.component not in catalog:
        parser.error("unknown component; use --list")
    spec = json.loads(args.args)
    manifest = json.loads(catalog[args.component].read_text())
    inputs = {}
    for name, value in spec.get("inputs", {}).items():
        media = value.get("media_type") if isinstance(value, dict) else None
        if isinstance(value, dict) and "file" in value:
            item = Path(value["file"]).resolve()
        elif isinstance(value, dict) and "text" in value:
            item = value["text"].encode()
        elif isinstance(value, dict) and "json" in value:
            item = value["json"]
        else:
            item = value
        inputs[name] = (item, media) if media else item
    out = (args.out or Path("toolkit-runs") / f"{args.component}-{time.time_ns()}").resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("--out must be a fresh empty directory; prior invocation receipts are immutable")
    invocation = make_invocation(
        out,
        manifest["entry"],
        inputs=inputs,
        config=spec.get("config", {}),
        source_dir=catalog[args.component].parent,
        seed=spec.get("seed", 11),
        component_id=args.component,
        limits=spec.get("limits", {}),
        ports=spec.get("ports", {}),
        llm_grant=spec.get("llm_grant"),
    )
    invocation.write(out / "invocation.json")
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join(map(str, sources)), OMP_NUM_THREADS="1", MKL_NUM_THREADS="1"
    )
    completed = subprocess.run(
        [sys.executable, "-m", "eidos_worker.run", str(out / "invocation.json")], env=env
    )
    if completed.returncode:
        return completed.returncode
    result = OutputManifest.read(out / "staging")
    print((out / "staging/eidos-output-manifest.json").read_text())
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
