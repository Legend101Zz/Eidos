"""Small, dependency-light contracts shared by the card tools."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SEEDS = (11, 23, 37)
MODEL_FILES = frozenset({"brief.md", "project.py", "train.npz", "validation.npz",
                         "requirements.lock", "task.json", "answer_schema.py"})
CARD_FILES = frozenset(f"bundle/{name}" for name in MODEL_FILES) | {
    "test_inputs.npz", "test_labels.npz", "baseline_test.npz"
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def inventory(root, exclude=()):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in seal")
        if path.is_file() and path.relative_to(root).as_posix() not in exclude:
            result[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return result


def score(y, predictions, task_type):
    predictions = np.asarray(predictions)
    if task_type == "classification":
        if predictions.ndim == 2:
            predictions = predictions.argmax(axis=1)
        return float(np.mean(y == predictions))
    return float(np.sqrt(np.mean((np.asarray(y, dtype=float) - predictions) ** 2)))


def gain(base, candidate, task_type):
    return candidate - base if task_type == "classification" else base - candidate


def bar_for(base, task_type):
    return 0.02 if task_type == "classification" else 0.05 * base


def subprocess_env():
    env = dict(os.environ)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
        env[name] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def run_cli(project, bundle, inputs, out, seed, timeout=120):
    """Fresh train and inference processes, with a separate 120 s training deadline."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    python = os.environ.get("EIDOS_RESEARCH_PYTHON", sys.executable)
    started = time.monotonic()
    command = [python, str(Path(project).resolve()), "train", "--inputs", str(Path(bundle).resolve()),
               "--out", str(out.resolve()), "--seed", str(seed)]
    subprocess.run(command, check=True, capture_output=True, timeout=timeout, env=subprocess_env())
    elapsed = time.monotonic() - started
    subprocess.run([python, str(Path(project).resolve()), "infer", "--model", str(out / "model.pt"),
                    "--inputs", str(Path(inputs).resolve()), "--out", str(out / "predictions.npz")],
                   check=True, capture_output=True, timeout=timeout, env=subprocess_env())
    with np.load(out / "predictions.npz", allow_pickle=False) as data:
        predictions, ids = data["predictions"].copy(), data["ids"].copy()
    return predictions, ids, elapsed, (out / "model.pt").stat().st_size
