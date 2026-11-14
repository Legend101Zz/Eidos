"""Deterministic source table packaging into the PrivateSeal card contract."""

import argparse
import json
import re
import tempfile
from pathlib import Path

import numpy as np

from .common import CARD_FILES, SEEDS, bar_for, private_tmp, read_json, run_cli, score, sha, write_json
from .intake import load_table

REPOSITORY = Path(__file__).resolve().parents[3]


def _stratified_take(indices, y, size, rng, classification):
    if not classification:
        shuffled = rng.permutation(indices)
        return shuffled[:size], shuffled[size:]
    labels, counts = np.unique(y[indices], return_counts=True)
    exact = counts * size / len(indices)
    allocation = np.floor(exact).astype(int)
    for i in np.argsort(-(exact - allocation), kind="stable")[:size - allocation.sum()]:
        allocation[i] += 1
    take, remain = [], []
    for label, n in zip(labels, allocation, strict=True):
        group = rng.permutation(indices[y[indices] == label])
        take.extend(group[:n])
        remain.extend(group[n:])
    return rng.permutation(take).astype(int), rng.permutation(remain).astype(int)


def prepare_arrays(x, y, task_type, seed):
    x, y = np.asarray(x, dtype=np.float32), np.asarray(y)
    if task_type not in {"classification", "regression"} or x.ndim != 2 or y.shape != (len(x),):
        raise ValueError("invalid table shape/type")
    if not np.isfinite(x).all():
        raise ValueError("nonfinite features")
    original_x, original_y = x, y
    # Preserve source order: np.unique's lexicographic order would make test values alter train membership.
    original_n = len(y)
    seen, keep = set(), []
    for i, (row, label) in enumerate(zip(x, y, strict=True)):
        key = (row.tobytes(), str(label))
        if key not in seen:
            keep.append(i)
            seen.add(key)
    x, y = x[keep], y[keep]
    rng = np.random.default_rng(seed)
    classification = task_type == "classification"
    labels = np.unique(y)
    label_map = {}
    if classification:
        if not 2 <= len(labels) <= 10:
            raise ValueError("classification requires 2..10 classes")
        codes = rng.permutation(len(labels))
        label_map = {str(label): int(code) for label, code in zip(labels, codes, strict=True)}
        y = np.asarray([label_map[str(label)] for label in y], dtype=np.int64)
    else:
        y = y.astype(np.float64)
        if not np.isfinite(y).all():
            raise ValueError("nonfinite target")
    n = len(y)
    n_test = min(3000, max(500, round(0.25 * n)))
    n_validation = min(1500, max(250, round(0.25 * (n - n_test))))
    n_train = min(n - n_test - n_validation, 4000)
    if n_train < 600:
        raise ValueError("table cannot supply at least 600 train rows")
    test, rest = _stratified_take(np.arange(n), y, n_test, rng, classification)
    validation, rest = _stratified_take(rest, y, n_validation, rng, classification)
    train, unused = _stratified_take(rest, y, n_train, rng, classification)
    constant = np.flatnonzero(np.ptp(x[train].astype(float), axis=0) == 0)
    retained = np.setdiff1d(np.arange(x.shape[1]), constant)
    permutation = rng.permutation(retained)
    if not len(permutation):
        raise ValueError("all features constant on train")
    x = x[:, permutation]
    target_scaling = None
    if not classification:
        mean, scale = float(y[train].mean()), float(y[train].std())
        if scale <= 0:
            raise ValueError("constant train target")
        target_scaling = dict(mean=mean, std=scale)
        y = ((y - mean) / scale).astype(np.float32)
    if not classification:
        # Train-fitted scaling can merge nearby raw targets at float32 precision.
        # Remove those rows and refit/resplit until the stored representation is unique.
        seen, retained = set(), []
        for i, (row, label) in enumerate(zip(x, y, strict=True)):
            key = (row.tobytes(), str(label))
            if key not in seen:
                retained.append(keep[i])
                seen.add(key)
        if len(retained) < n:
            data, provenance = prepare_arrays(original_x[retained], original_y[retained], task_type, seed)
            provenance["retained_source_rows"] = [retained[i] for i in provenance["retained_source_rows"]]
            provenance["split_source_rows"] = {name: [retained[i] for i in rows]
                                                for name, rows in provenance["split_source_rows"].items()}
            provenance["duplicate_rows_dropped"] = original_n - len(provenance["retained_source_rows"])
            return data, provenance
    ids = rng.integers(1, np.iinfo(np.int64).max, size=n, dtype=np.int64)
    while len(np.unique(ids)) != n:
        ids = rng.integers(1, np.iinfo(np.int64).max, size=n, dtype=np.int64)
    data = {name: dict(x=x[index], y=y[index], ids=ids[index])
            for name, index in (("train", train), ("validation", validation), ("test", test))}
    provenance = dict(seed=seed, split_seeds={"split": seed, "baseline": 11},
                      column_permutation=permutation.tolist(), label_map=label_map,
                      target_scaling=target_scaling, dropped_columns=constant.tolist(),
                      duplicate_rows_dropped=original_n - n,
                      retained_source_rows=keep,
                      split_source_rows={name: [keep[i] for i in indices]
                                         for name, indices in (("train", train), ("validation", validation),
                                                               ("test", test), ("unused", unused))},
                      split_sizes={name: len(values["y"]) for name, values in data.items()})
    return data, provenance


def package_card(x, y, task_type, card_id, pool_root, *, seed=20261114, source_info=None,
                 source_digest=None, columns=None):
    if not re.fullmatch(r"R\d{3,4}", card_id):
        raise ValueError("card id must be R followed by three or four digits")
    pool_root = Path(pool_root)
    card = pool_root / "seal" / card_id
    controller = pool_root / "controller" / card_id
    if card.exists() or controller.exists():
        raise ValueError("card output already exists")
    data, provenance = prepare_arrays(x, y, task_type, seed)
    bundle = card / "bundle"
    bundle.mkdir(parents=True)
    controller.mkdir(parents=True)
    for split in ("train", "validation"):
        np.savez_compressed(bundle / f"{split}.npz", **data[split])
    test = data["test"]
    np.savez_compressed(card / "test_inputs.npz", x=test["x"], ids=test["ids"])
    np.savez_compressed(card / "test_labels.npz", y=test["y"], ids=test["ids"])
    (bundle / "project.py").write_bytes(Path(__file__).with_name("baseline_project.py").read_bytes())
    for source, destination in (("research/public/development/validate_answer.py", "answer_schema.py"),
                                ("study/packaging/bundles/N01/requirements.lock", "requirements.lock")):
        (bundle / destination).write_bytes((REPOSITORY / source).read_bytes())
    classification = task_type == "classification"
    template = (REPOSITORY / "study/packaging/bundles/N01/task.json").read_text()
    task = json.loads(template)
    task.update(task_id=card_id, project=card_id, task_type=task_type,
                metric="accuracy" if classification else "rmse",
                direction="maximize" if classification else "minimize",
                nclasses=len(np.unique(data["train"]["y"])) if classification else 1,
                inputs={split: dict(shape=list(data[split]["x"].shape), dtype="float32")
                        for split in ("train", "validation")},
                qualification="controller-side qualification.json")
    brief = (REPOSITORY / f"study/packaging/bundles/{'N01' if classification else 'N02'}/brief.md").read_text()
    (bundle / "brief.md").write_text(f"{card_id}: {brief}")
    task["hashes"] = {p.name: sha(p.read_bytes()) for p in sorted(bundle.iterdir())}
    write_json(bundle / "task.json", task)
    with tempfile.TemporaryDirectory(prefix="cards-baseline-", dir=private_tmp(pool_root)) as temporary:
        predictions, ids, elapsed, size = run_cli(bundle / "project.py", bundle, card / "test_inputs.npz",
                                                Path(temporary), 11)
    if not np.array_equal(ids, test["ids"]):
        raise ValueError("baseline ID mismatch")
    np.savez_compressed(card / "baseline_test.npz", ids=ids, predictions=predictions)
    measured = score(test["y"], predictions, task_type)
    manifest = dict(id=card_id, task_type=task_type, metric=task["metric"], nclasses=task["nclasses"], qualified=False,
                    min_reference_gain=bar_for(measured, task_type), seeds=list(SEEDS),
                    splits={s: {"n": len(d["y"])} for s, d in data.items()},
                    baseline=dict(seed=11, score=measured),
                    files={name: sha((card / name).read_bytes()) for name in sorted(CARD_FILES)})
    write_json(card / "manifest.json", manifest)
    provenance.update(source=(source_info or {}).get("source", "arrays"),
                      key=(source_info or {}).get("key"), source_json_sha256=source_digest,
                      source_info=source_info, original_columns=columns,
                      baseline_retrain_seconds=elapsed, baseline_checkpoint_bytes=size)
    write_json(controller / "provenance.json", provenance)
    return card


def package(source_dir, card_id, pool_root, task_type, seed=20261114):
    source_dir = Path(source_dir)
    table = load_table(source_dir)
    return package_card(table["x"], table["y"], task_type, card_id, pool_root, seed=seed,
                        source_info=read_json(source_dir / "SOURCE.json"),
                        source_digest=sha((source_dir / "SOURCE.json").read_bytes()), columns=table["columns"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--card-id", required=True)
    parser.add_argument("--pool-root", required=True)
    parser.add_argument("--task-type", required=True, choices=["classification", "regression"])
    parser.add_argument("--seed", type=int, default=20261114)
    args = parser.parse_args()
    print(package(args.source_dir, args.card_id, args.pool_root, args.task_type, args.seed))


if __name__ == "__main__":
    main()
