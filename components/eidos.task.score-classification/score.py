"""Score classification predictions against visible labels (validation/development only).

Never used as the final grader: the final evaluator owns test labels independently."""
import math

import numpy as np


def _wilson(k, n, z=1.959964):
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def run(inputs, config, *, reader, writer, context):
    with np.load(reader.path("predictions"), allow_pickle=False) as p:
        pid, pred = p["ids"], p["predictions"]
    with np.load(reader.path("reference"), allow_pickle=False) as r:
        rid, y = r["ids"], r["y"]
    if pred.ndim != 1 or len(pid) != len(pred):
        raise ValueError("predictions must be one scalar per id")
    if len(set(pid.tolist())) != len(pid):
        raise ValueError("duplicate prediction ids")
    if set(pid.tolist()) != set(rid.tolist()):
        raise ValueError("prediction ids do not match the reference ids exactly")
    if not np.all(np.isfinite(pred)) or not np.issubdtype(pred.dtype, np.integer):
        raise ValueError("classification predictions must be finite integers")
    nclasses = int(config.get("nclasses", int(y.max()) + 1))
    if pred.min() < 0 or pred.max() >= nclasses:
        raise ValueError("class id out of range")
    order = {int(i): k for k, i in enumerate(pid.tolist())}
    aligned = np.array([pred[order[int(i)]] for i in rid.tolist()])
    correct = (aligned == y).astype(int)
    n, k = int(len(y)), int(correct.sum())
    acc = k / n
    report = {"n": n, "correct": k, "accuracy": acc, "wilson_95": _wilson(k, n), "split": config.get("split", "validation")}
    writer.stage_json("report", report, schema="classification-report/1")
    writer.stage_rows("per_example", [{"id": int(i), "correct": int(c)} for i, c in zip(rid.tolist(), correct.tolist())],
                      schema="per-example-correct/1")
    return {"metrics": {"accuracy": acc, "n": n}, "wilson_95": report["wilson_95"]}
