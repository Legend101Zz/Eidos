"""Run an ordinary project command in a private working copy (stdlib only)."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_COPY_BYTES = 2 << 20
_SLOT = re.compile(r"[^a-z0-9_\-]+")


def _copy_project(src_root, work, skip):
    for p in Path(src_root).iterdir():
        if p.is_file() and not p.is_symlink() and p.name not in skip and p.stat().st_size <= MAX_COPY_BYTES \
                and not p.name.endswith((".npz", ".pt", ".pth")):
            shutil.copyfile(p, work / p.name)


def run(inputs, config, *, reader, writer, context):
    work = Path(context.scratch_dir) / "work"
    out = Path(context.scratch_dir) / "out"
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    files = dict(config.get("files") or {})
    source = "project-root"
    if "source" in inputs:
        bundle = reader.json("source")
        for rel, text in (bundle.get("files") or {}).items():
            target = (work / rel).resolve()
            if work.resolve() not in target.parents:
                raise ValueError("source bundle path escapes the working directory: %r" % rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        source = "candidate-bundle:" + inputs["source"].artifact_id
    else:
        _copy_project(os.getcwd(), work, set(files.values()))
    for port, name in files.items():
        if port in inputs:
            if "/" in name or name.startswith("."):
                raise ValueError("file name for %s must be a plain name" % port)
            shutil.copyfile(reader.path(port), work / name)
    seed = config.get("seed", context.seed if context.seed is not None else 0)
    subst = {"{work}": str(work), "{out}": str(out), "{seed}": str(seed)}
    argv = []
    for i, a in enumerate(config["argv"]):
        if i == 0 and a == "python":
            a = sys.executable
        for k, v in subst.items():
            a = a.replace(k, v)
        argv.append(a)
    started = time.time()
    proc = subprocess.run(argv, cwd=str(work), capture_output=True, text=True,
                          timeout=float(config.get("timeout_seconds", 600)))
    elapsed = time.time() - started
    (out / "command-stdout.txt").write_text(proc.stdout[-200000:])
    (out / "command-stderr.txt").write_text(proc.stderr[-200000:])
    if proc.returncode != 0:
        raise RuntimeError("project command failed (exit %d): %s" % (proc.returncode, proc.stderr[-2000:]))
    staged, metrics = [], {}
    for p in sorted(out.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        slot = _SLOT.sub("_", p.stem.lower())
        if p.name == "receipt.json":
            try:
                metrics = json.loads(p.read_text())
            except ValueError:
                metrics = {}
            continue
        if p.suffix == ".npz":
            writer.stage_file(slot, str(p), media_type="application/x-npz",
                              schema="prediction-array/1" if "prediction" in p.name else "task-array/1")
        elif p.suffix in (".pt", ".pth"):
            writer.stage_file(slot, str(p), media_type="application/octet-stream", schema="model-file/1",
                              role="model-export")
        elif p.suffix == ".txt":
            writer.stage_file(slot, str(p), media_type="text/plain", role="log")
        else:
            writer.stage_file(slot, str(p), media_type="application/octet-stream")
        staged.append(slot)
    if "source" not in inputs:
        bundle = {"files": {p.name: p.read_text() for p in work.iterdir()
                            if p.is_file() and p.suffix in (".py", ".json", ".md", ".lock", ".txt", ".cfg", ".toml")
                            and p.name not in set(files.values())}}
        writer.stage_json("source_snapshot", bundle, schema="source-bundle/1")
        staged.append("source_snapshot")
    receipt = {"argv": [os.path.basename(argv[0])] + argv[1:], "returncode": proc.returncode, "seconds": round(elapsed, 3),
               "outputs": staged, "source": source, "project_receipt": metrics}
    writer.stage_json("receipt", receipt, schema="project-receipt/1")
    numeric = {k: v for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {"metrics": numeric, "source": source, "outputs": staged, "seconds": round(elapsed, 3)}
