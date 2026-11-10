"""Paired classification report: align baseline and candidate predictions with labels by example id.

Both arms of an evidence plan run this component on the same three artifacts (the baseline's held-out
predictions, the candidate's held-out predictions and the labels). Each run checks the complete
three-way alignment and fails closed on any mismatch, then emits the paired-report rows of *its own*
arm (``config.role``) in the ``predictions`` slot, which the assessment workflow reads. The assessor
(``eidos.assessors.paired-ci`` >= 1.1.0, metric ``accuracy``) recomputes correctness from the label and
prediction in each row and pairs the two arms by ``(cluster, example_id)``.

Accepted formats for each input (id field ``id``; value field ``prediction`` or ``label``):
* JSON: a list of records ``[{"id": .., "prediction": ..}]``, or columns ``{"ids": [..], "predictions": [..]}``
  (labels: ``"labels"`` or ``"y"``), or ``{"rows": [records]}``;
* JSONL or CSV records (CSV values are strings);
* NPZ arrays ``ids`` and ``predictions`` (labels: ``y`` or ``labels``), the convention of
  ``eidos.task.score-classification`` and ``eidos.pytorch.evaluate``.

Class labels and ids are compared in a canonical string form: integers, integral floats and strings that
spell an integer (``"7"``, ``"3.0"``) as decimal integers; other strings as given (trimmed). Booleans,
non-integral or non-finite numbers, empty strings and nulls are refused.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any

from eidos_worker.errors import ContractError

ROLES = ("baseline", "candidate")
VALUE_FIELDS = {"prediction": ("prediction", "predictions"), "label": ("label", "labels", "y")}
MAX_EXAMPLES = 5_000_000
# A string that spells an integer ("7", "-2", "3.0") is that integer, so CSV "1.0", JSON 1 and NPZ 1 agree.
INTEGER_TEXT = re.compile(r"[+-]?[0-9]+(\.0*)?")


def canonical(value: Any, what: str) -> str:
    """Canonical string form of an id or a class label; refuses anything ambiguous or non-finite."""
    if isinstance(value, bool) or value is None:
        raise ContractError(f"{what}: {value!r} is not a valid id or class label")
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()  # numpy scalar
        if isinstance(value, bool):
            raise ContractError(f"{what}: {value!r} is not a valid id or class label")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{what}: non-finite value {value!r}")
        if not value.is_integer():
            raise ContractError(f"{what}: {value!r} is not an integer class label or id")
        return str(int(value))
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ContractError(f"{what}: empty value")
        if text.lower() in ("nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            raise ContractError(f"{what}: non-finite value {value!r}")
        if INTEGER_TEXT.fullmatch(text):
            return str(int(text.split(".", 1)[0]))
        return text
    raise ContractError(f"{what}: unsupported value type {type(value).__name__}")


def _records_from_columns(data: dict[str, Any], port: str, kind: str) -> list[tuple[Any, Any]]:
    ids = data.get("ids")
    field = next((f for f in VALUE_FIELDS[kind] if f in data), None)
    if not isinstance(ids, list) or field is None or not isinstance(data[field], list):
        raise ContractError(f"input {port!r}: expected columns 'ids' and one of {list(VALUE_FIELDS[kind])}")
    if len(ids) != len(data[field]):
        raise ContractError(f"input {port!r}: {len(ids)} ids but {len(data[field])} {field}")
    return list(zip(ids, data[field], strict=True))


def _records_from_rows(rows: list[Any], port: str, kind: str, id_field: str) -> list[tuple[Any, Any]]:
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractError(f"input {port!r}: record {i} is not an object")
        field = next((f for f in VALUE_FIELDS[kind] if f in row), None)
        if id_field not in row or field is None:
            raise ContractError(f"input {port!r}: record {i} needs {id_field!r} and one of {list(VALUE_FIELDS[kind])}")
        out.append((row[id_field], row[field]))
    return out


def load_pairs(reader, port: str, kind: str, id_field: str = "id") -> list[tuple[Any, Any]]:
    """Read (id, value) pairs from an input in any supported format."""
    binding = reader.binding(port)
    path = reader.path(binding)
    media = (binding.media_type or "").lower()
    suffix = path.suffix.lower().lstrip(".")
    if "npz" in media or suffix == "npz":
        try:
            import numpy as np  # only for NPZ inputs
        except ImportError as exc:
            raise ContractError(
                f"input {port!r} is NPZ, which needs numpy in this component's environment; install the "
                "component in a project environment with numpy, or supply JSON, JSONL or CSV"
            ) from exc

        with np.load(path, allow_pickle=False) as arr:
            field = next((f for f in VALUE_FIELDS[kind] if f in arr.files), None)
            if "ids" not in arr.files or field is None:
                raise ContractError(f"input {port!r}: NPZ needs arrays 'ids' and one of {list(VALUE_FIELDS[kind])}")
            ids, vals = arr["ids"], arr[field]
            if ids.ndim != 1 or vals.ndim != 1 or len(ids) != len(vals):
                raise ContractError(f"input {port!r}: 'ids' and {field!r} must be 1-D arrays of equal length")
            return list(zip(ids.tolist(), vals.tolist(), strict=True))
    text = path.read_text(encoding="utf-8")
    if "csv" in media or suffix == "csv":
        return _records_from_rows(list(csv.DictReader(io.StringIO(text))), port, kind, id_field)
    if "jsonl" in media or "ndjson" in media or suffix in ("jsonl", "ndjson"):
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError as exc:
            raise ContractError(f"input {port!r}: invalid JSON lines ({exc})") from exc
        return _records_from_rows(rows, port, kind, id_field)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ContractError(f"input {port!r}: not JSON, JSONL, CSV or NPZ ({exc})") from exc
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        data = data["rows"]
    if isinstance(data, list):
        return _records_from_rows(data, port, kind, id_field)
    if isinstance(data, dict):
        return _records_from_columns(data, port, kind)
    raise ContractError(f"input {port!r}: unsupported JSON shape")


def index(pairs: list[tuple[Any, Any]], port: str) -> dict[str, str]:
    """Map canonical id -> canonical value; refuses duplicates and invalid values."""
    if not pairs:
        raise ContractError(f"input {port!r} has no examples")
    if len(pairs) > MAX_EXAMPLES:
        raise ContractError(f"input {port!r}: more than {MAX_EXAMPLES} examples")
    out: dict[str, str] = {}
    for raw_id, raw_value in pairs:
        key = canonical(raw_id, f"input {port!r} id")
        if key in out:
            raise ContractError(f"input {port!r}: duplicate id {key!r}")
        out[key] = canonical(raw_value, f"input {port!r} id {key!r} value")
    return out


def _sample(ids: set[str]) -> list[str]:
    return sorted(ids)[:5]


def check_alignment(baseline: dict[str, str], candidate: dict[str, str], labels: dict[str, str]) -> None:
    """The three id sets must be identical: no example may be dropped or added on either side."""
    problems = []
    for name, ids in (("baseline", baseline), ("candidate", candidate)):
        missing, extra = set(labels) - set(ids), set(ids) - set(labels)
        if missing:
            problems.append(f"{name} lacks {len(missing)} labelled ids (e.g. {_sample(missing)})")
        if extra:
            problems.append(f"{name} has {len(extra)} ids without a label (e.g. {_sample(extra)})")
    if problems:
        raise ContractError("example ids do not align: " + "; ".join(problems))


def run(inputs, config, *, reader, writer, context):
    role = config.get("role")
    if role not in ROLES:
        raise ContractError(f"config.role must be one of {ROLES} (the arm whose rows this run emits)")
    cluster = config.get("cluster")
    if cluster is None:
        raise ContractError("config.cluster (the training seed of both predictions, e.g. \"11\") is required")
    cluster = canonical(cluster, "config.cluster")
    id_field = str(config.get("id_field", "id"))
    preds = {side: index(load_pairs(reader, side, "prediction", id_field), side) for side in ROLES}
    labels = index(load_pairs(reader, "labels", "label", id_field), "labels")
    check_alignment(preds["baseline"], preds["candidate"], labels)
    allowed = config.get("classes")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed:
            raise ContractError("config.classes must be a non-empty list of class labels")
        classes = {canonical(c, "config.classes") for c in allowed}
        for name, values in (("labels", labels), *preds.items()):
            bad = sorted({v for v in values.values() if v not in classes})
            if bad:
                raise ContractError(f"{name}: values outside config.classes: {bad[:5]}")
    ordered = sorted(labels, key=lambda k: (len(k), k))
    mine = preds[role]
    rows = [{"example_id": k, "cluster": cluster, "role": role, "label": labels[k], "prediction": mine[k]}
            for k in ordered]
    writer.stage_rows("predictions", rows, schema="classification-pair-rows/1")
    return {
        "role": role,
        "cluster": cluster,
        "examples": len(rows),
        "classes": len(set(labels.values())),
        "aligned": True,
        "metrics": {"examples": len(rows)},
    }
