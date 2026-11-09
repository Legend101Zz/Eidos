"""Card adapter (builder-side): apply the release's shared error-slice instrument to validation
predictions. Runs inside an eidos.project.command job from a source bundle that carries the release's
data.py/instrument.py unchanged. Uses public development validation labels (reference-assisted)."""
import argparse
import hashlib
import json
from pathlib import Path

from data import dataset
from grade import read_predictions
from instrument import error_slices

p = argparse.ArgumentParser()
p.add_argument("--project", required=True)
p.add_argument("--predictions", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
data, kind, nclass = dataset(a.project)
x, y, ids = data["validation"]
pred = read_predictions(a.predictions, ids, kind, nclass)
report = error_slices(x, y, pred, kind)
n = sum(b["n"] for b in report["bins"])
agg = sum(b["n"] * (b["mean_error"] or 0) for b in report["bins"]) / len(y)
assert n == len(y), "slice counts do not sum to the validation size"
assert abs(agg - report["mean_error"]) < 1e-10, "weighted aggregation mismatch"
out = Path(a.out)
(out / "slices.json").write_text(json.dumps({"project": a.project, **report}, indent=2))
(out / "receipt.json").write_text(json.dumps({
    "instrument_version": report["instrument_version"], "slice_counts_sum": n, "mean_error": report["mean_error"],
    "instrument_sha256": hashlib.sha256(Path("instrument.py").read_bytes()).hexdigest(), "aggregation_checked": 1}))
