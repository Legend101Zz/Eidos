"""Score numeric predictions against visible labels (validation/development only).

Never used as the final grader: the final evaluator owns test labels independently."""
import numpy as np


def run(inputs, config, *, reader, writer, context):
    with np.load(reader.path("predictions"), allow_pickle=False) as p:
        pid, pred = p["ids"], p["predictions"]
    with np.load(reader.path("reference"), allow_pickle=False) as r:
        rid, y = r["ids"], r["y"]
    if pred.ndim != 1 or pid.ndim != 1 or len(pid) != len(pred):
        raise ValueError("predictions must be one scalar per id")
    if pid.dtype.kind not in "iu" or len(set(pid.tolist())) != len(pid):
        raise ValueError("ids must be unique integers")
    if set(pid.tolist()) != set(rid.tolist()):
        raise ValueError("prediction ids do not match the reference ids exactly")
    if pred.dtype.kind not in "iuf" or not np.all(np.isfinite(pred)):
        raise ValueError("predictions must be finite numbers")
    order = {int(i): k for k, i in enumerate(pid.tolist())}
    aligned = np.array([pred[order[int(i)]] for i in rid.tolist()], dtype=np.float64)
    err = aligned - y.astype(np.float64)
    with np.errstate(over="raise", invalid="raise"):
        sq, ab = err ** 2, np.abs(err)
        mse, mae = float(sq.mean()), float(ab.mean())
    if not (np.isfinite(mse) and np.isfinite(mae)):
        raise ValueError("nonfinite metric from finite input")
    n = int(len(y))
    report = {"n": n, "mse": mse, "rmse": mse ** 0.5, "mae": mae, "split": config.get("split", "validation")}
    writer.stage_json("report", report, schema="regression-report/1")
    writer.stage_rows("per_example", [{"id": int(i), "squared_error": float(s), "abs_error": float(a)}
                                      for i, s, a in zip(rid.tolist(), sq.tolist(), ab.tolist())],  # noqa: B905 (equal lengths by construction; python>=3.9)
                      schema="per-example-error/1")
    return {"metrics": {"rmse": report["rmse"], "mse": mse, "mae": mae, "n": n}}
