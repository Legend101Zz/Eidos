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
_MODEL_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(?:pt|pth)")


def _copy_project(src_root, work, skip):
    for p in Path(src_root).iterdir():
        if p.is_file() and not p.is_symlink() and p.name not in skip and p.stat().st_size <= MAX_COPY_BYTES \
                and not p.name.endswith((".npz", ".pt", ".pth")):
            shutil.copyfile(p, work / p.name)


SUFFIX_BY_MEDIA = {"application/x-npz": ".npz", "application/json": ".json", "text/csv": ".csv", "text/plain": ".txt"}


def default_suffix(media_type, schema=None):
    if schema == "model-file/1":
        return ".pt"
    return SUFFIX_BY_MEDIA.get(media_type or "", "")


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
            if isinstance(text, dict) and "base64" in text:
                import base64
                target.write_bytes(base64.b64decode(text["base64"]))
            else:
                target.write_text(text)
        source = "candidate-bundle:" + inputs["source"].artifact_id
    elif config.get("copy") is not None:  # explicit allowlist of project files
        for name in config["copy"]:
            src = Path(os.getcwd()) / name
            if "/" in name or not src.is_file() or src.is_symlink():
                raise ValueError("copy entry must be a plain project file: %r" % name)
            shutil.copyfile(src, work / name)
    else:
        _copy_project(os.getcwd(), work, set(files.values()))
    # Declared inputs not named in config.files are staged by the documented default: <port><ext>, with the
    # extension from the input's media type (the same rule the lab-free driver uses).
    for port, binding in inputs.items():
        if port != "source" and port not in files:
            files[port] = port + default_suffix(getattr(binding, "media_type", None), getattr(binding, "schema", None))
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
    if proc.returncode != 0 and not config.get("allow_nonzero_exit"):
        raise RuntimeError("project command failed (exit %d); work dir had %s; stderr tail: %s"
                           % (proc.returncode, sorted(x.name for x in work.iterdir())[:40], proc.stderr[-1500:]))
    staged, metrics, model_exports = [], {}, []
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
            if not _MODEL_BASENAME.fullmatch(p.name) or ".." in p.name:
                raise ValueError("unsafe original model output basename")
            model = writer.stage_file(slot, str(p), media_type="application/octet-stream", schema="model-file/1",
                                      role="model-export")
            model_exports.append(dict(slot=slot, basename=p.name, digest=model.digest, size=model.size))
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
               "staged_inputs": {k: v for k, v in files.items() if k in inputs},
               "outputs": staged, "source": source, "project_receipt": metrics, "model_exports": model_exports}
    writer.stage_json("receipt", receipt, schema="project-receipt/1")
    numeric = {k: v for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {"metrics": numeric, "source": source, "outputs": staged, "seconds": round(elapsed, 3),
            "returncode": proc.returncode}
