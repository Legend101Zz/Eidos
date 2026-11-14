"""Persistent, scrubbed one-thread recipe child with process-group timeout cleanup."""
import json
import os
import select as io_select
import signal
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np

from . import cv, export, models, registry, select
from .util import canonical


class RecipeWorker:
    def __init__(self, root, x, y, xv, yv, kind, *, groups=None, ids=None, folds=5, repeats=None, partition_seed=81723, seed=11, scheme="cv-train"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.data = self.root / "visible.npz"
        values = dict(x=x, y=y, xv=xv, yv=yv, ids=np.arange(len(x)) if ids is None else ids)
        if groups is not None:
            values["groups"] = groups
        np.savez(self.data, **values)
        self.settings = dict(kind=kind, folds=folds, repeats=repeats, partition_seed=partition_seed, seed=seed, scheme=scheme)
        self.process = None
        self.log = None

    def start(self):
        env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ", "SYSTEMROOT") if key in os.environ}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                   HOME=str(self.root), TMPDIR=str(self.root), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                   OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1")
        self.log = (self.root / "child.log").open("wb")
        self.process = subprocess.Popen([sys.executable, "-m", "eidos_recipes.worker", str(self.data), canonical(self.settings)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                        cwd=self.root, env=env, start_new_session=True)

    def close(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def __enter__(self):
        self.previous_sigterm = signal.getsignal(signal.SIGTERM)
        def terminated(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminated)
        return self

    def __exit__(self, *_):
        self.close()
        signal.signal(signal.SIGTERM, self.previous_sigterm)

    def request(self, request, timeout):
        started = time.monotonic()
        if self.process is None:
            self.start()
        try:
            self.process.stdin.write((canonical(request) + "\n").encode())
            self.process.stdin.flush()
            ready, _, _ = io_select.select([self.process.stdout], [], [], max(.000001, timeout - (time.monotonic() - started)))
            if not ready:
                self.close()
                return dict(status="timeout", error="per-recipe timeout", fit_seconds=time.monotonic() - started)
            line = self.process.stdout.readline()
            if not line:
                self.close()
                return dict(status="failed", error="recipe child exited", fit_seconds=time.monotonic() - started)
            return json.loads(line)
        except (BrokenPipeError, OSError, ValueError):
            self.close()
            return dict(status="failed", error="recipe child protocol failed", fit_seconds=time.monotonic() - started)

    def evaluate(self, spec, *, timeout=60.):
        return self.request(dict(operation="evaluate", spec=spec), timeout)

    def fit(self, spec, *, timeout=60.):
        return self.request(dict(operation="fit", spec=spec), timeout)


def child(data, settings):
    with np.load(data, allow_pickle=False) as v:
        x, y, xv, yv, ids = (v[k] for k in ("x", "y", "xv", "yv", "ids"))
        groups = v.get("groups", None)
    partitions = cv.partitions(x, y, settings["kind"], groups=groups, seed=settings["partition_seed"], folds=settings["folds"], repeats=settings["repeats"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for line in sys.stdin:
            started = time.perf_counter()
            try:
                request = json.loads(line)
                spec = request["spec"]
                if request["operation"] == "fit" or settings["scheme"] == "train-fit-validation-score":
                    model = models.build(spec, settings["seed"]).fit(x, y)
                    fit_seconds = time.perf_counter() - started
                    p = model.predict(xv)
                    if not np.isfinite(p).all():
                        raise ValueError("nonfinite predictions")
                    path = Path(data).with_name("checkpoint.pt")
                    try:
                        size = export.save(path, model, spec)
                        # Exercise the restricted loader before declaring export eligibility.
                        export.load(path)
                    finally:
                        path.unlink(missing_ok=True)
                    result = dict(status="ok", scores=select.row_scores(settings["kind"], yv, p).tolist(),
                                  mean=select.score(settings["kind"], yv, p), predictions=p.tolist(),
                                  fit_seconds=fit_seconds, checkpoint_bytes=size,
                                  validation_se=select.score_se(settings["kind"], yv, p),
                                  export_eligible=fit_seconds <= 60 and size <= export.MAX_CHECKPOINT_BYTES)
                else:
                    result = cv.evaluate(spec, x, y, partitions=partitions, seed=settings["seed"], ids=ids)
                    result["checkpoint_bytes_est"] = registry.estimated_bytes(spec, len(x), x.shape[1])
            except Exception as exc:
                # Scientific errors are bounded and do not print environment or request contents.
                result = dict(status="failed", error=(type(exc).__name__ + ": " + str(exc)).replace("\n", " ")[:180],
                              fit_seconds=time.perf_counter() - started)
            print(canonical(result), flush=True)


if __name__ == "__main__":
    child(sys.argv[1], json.loads(sys.argv[2]))
