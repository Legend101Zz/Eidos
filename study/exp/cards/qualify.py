"""Fail-closed card qualification with fresh baseline/reference reproductions."""

import argparse
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from sklearn.metrics import roc_auc_score

from .common import CARD_FILES, MODEL_FILES, SEEDS, gain, read_json, run_cli, score, sha, write_json


def verify_card(card):
    card = Path(card)
    for path in (card, *[card / name for name in CARD_FILES], card / "manifest.json", card / "bundle"):
        if path.is_symlink():
            raise ValueError("symlink in card")
    manifest = read_json(card / "manifest.json")
    actual = {name: sha((card / name).read_bytes()) for name in sorted(CARD_FILES)}
    if manifest["files"] != actual:
        raise ValueError("card file digest mismatch")
    task = read_json(card / "bundle/task.json")
    expected = {name: actual[f"bundle/{name}"] for name in MODEL_FILES - {"task.json"}}
    if task["hashes"] != expected:
        raise ValueError("bundle hash inventory mismatch")
    if task["task_id"] != manifest["id"] or task["project"] != manifest["id"]:
        raise ValueError("bundle identity mismatch")
    return manifest


def leakage_screen(x, y, task_type):
    flags, maximum = [], 0.0
    for column in range(x.shape[1]):
        values = x[:, column]
        if np.std(values) == 0:
            continue
        if task_type == "classification":
            for label in np.unique(y):
                binary = y == label
                if binary.all() or not binary.any():
                    continue
                auc = float(roc_auc_score(binary, values))
                strength = max(auc, 1 - auc)
                maximum = max(maximum, strength)
                if strength > 0.98:
                    flags.append(dict(column=column, label=int(label), auc=strength))
        elif np.std(y) > 0:
            correlation = abs(float(np.corrcoef(values, y)[0, 1]))
            maximum = max(maximum, correlation)
            if correlation > 0.99:
                flags.append(dict(column=column, absolute_correlation=correlation))
    return dict(passed=not flags, maximum=maximum, flags=flags)


def split_check(arrays):
    pairs = (("train", "validation"), ("train", "test"), ("validation", "test"))
    sets = {name: {(row.tobytes(), str(label)) for row, label in zip(data[0], data[1], strict=True)}
            for name, data in arrays.items()}
    features = {name: {row.tobytes() for row in data[0]} for name, data in arrays.items()}
    duplicate_count = sum(len(sets[a] & sets[b]) for a, b in pairs)
    labels = {}
    for name, data in arrays.items():
        labels[name] = {}
        for row, label in zip(data[0], data[1], strict=True):
            labels[name].setdefault(row.tobytes(), set()).add(str(label))
    conflicts = sum(sum(any(left != right for left in labels[a][row] for right in labels[b][row])
                        for row in features[a] & features[b]) for a, b in pairs)
    id_sets = [set(data[2].tolist()) for data in arrays.values()]
    return dict(passed=duplicate_count == 0 and
                sum(len(data[2]) for data in arrays.values()) == len(set.union(*id_sets)),
                cross_split_duplicate_rows=duplicate_count, conflicting_feature_rows=conflicts)


def qualify(card_dir, controller_dir, *, allow_null=True, cheap_only=False):
    card, controller = Path(card_dir), Path(controller_dir)
    checks = {}
    manifest = read_json(card / "manifest.json")
    try:
        verify_card(card)
        opening = card.parent / "commitment-opening.json"
        if opening.exists():
            from .pools import verify
            verify(card.parent.parent)
        checks["hashes"] = dict(passed=True)
    except (ValueError, KeyError, OSError) as error:
        checks["hashes"] = dict(passed=False, error=str(error))
    kind, bar = manifest["task_type"], manifest["min_reference_gain"]
    # Do not execute code from a damaged bundle.
    if not checks["hashes"]["passed"]:
        manifest["qualified"] = False
        write_json(card / "manifest.json", manifest)
        result = dict(qualified=False, checks=checks)
        write_json(controller / "qualification.json", result)
        return result
    with np.load(card / "test_labels.npz", allow_pickle=False) as data:
        y, ids = data["y"], data["ids"]
    with np.load(card / "baseline_test.npz", allow_pickle=False) as data:
        baseline = data["predictions"]
        if not np.array_equal(ids, data["ids"]):
            raise ValueError("baseline test IDs disagree")
    base_score = score(y, baseline, kind)
    checks["baseline_score"] = dict(passed=abs(base_score - manifest["baseline"]["score"]) < 1e-12,
                                    score=base_score)
    arrays = {}
    for split in ("train", "validation"):
        with np.load(card / "bundle" / f"{split}.npz", allow_pickle=False) as data:
            arrays[split] = (data["x"], data["y"], data["ids"])
    with np.load(card / "test_inputs.npz", allow_pickle=False) as data:
        arrays["test"] = (data["x"], y, data["ids"])
    checks["split_disjointness"] = split_check(arrays)
    checks["leakage"] = leakage_screen(*arrays["validation"][:2], kind)
    try:
        if cheap_only:
            raise OSError("reference search deferred")
        reference = read_json(controller / "reference.json")
        checks["reference_digest"] = dict(passed=sha((controller / "reference_project.py").read_bytes()) ==
                                          reference["reference_py_sha256"])
    except (OSError, KeyError, ValueError) as error:
        checks["reference_digest"] = dict(passed=False, error=str(error))
    reference_scores = []
    for recipe, project in (("baseline", card / "bundle/project.py"),
                             ("reference", controller / "reference_project.py")):
        if cheap_only and recipe == "reference":
            continue
        for seed in SEEDS:
            name = f"{recipe}_{seed}"
            if recipe == "reference" and not checks["reference_digest"]["passed"]:
                checks[name] = dict(passed=False, error="reference source digest mismatch")
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="cards-qualify-") as temporary:
                    predictions, actual_ids, elapsed, size = run_cli(project, card / "bundle",
                                                                     card / "test_inputs.npz",
                                                                     Path(temporary), seed)
                measured = score(y, predictions, kind)
                change = gain(base_score, measured, kind)
                passed = elapsed <= 120 and size <= 64 * 1024 * 1024 and np.array_equal(ids, actual_ids)
                if recipe == "baseline":
                    passed = passed and (np.array_equal(predictions, baseline) if seed == 11 else change < bar)
                else:
                    reference_scores.append(measured)
                    passed = passed and (allow_null or change >= bar)
                checks[name] = dict(passed=bool(passed), score=measured, gain=change, clears_bar=change >= bar,
                                    retrain_seconds=elapsed, checkpoint_bytes=size)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError) as error:
                checks[name] = dict(passed=False, error=str(error))
    if cheap_only:
        checks.pop("reference_digest", None)
        result = dict(qualified=all(c["passed"] for c in checks.values()), checks=checks)
        write_json(controller / "qualification.json", result)
        return result
    null_card = len(reference_scores) != 3 or any(gain(base_score, s, kind) < bar for s in reference_scores)
    manifest["null_card"] = null_card
    manifest["qualified"] = all(check["passed"] for check in checks.values())
    write_json(card / "manifest.json", manifest)
    result = dict(qualified=manifest["qualified"], null_card=null_card, checks=checks,
                  conflicting_feature_rows=checks["split_disjointness"]["conflicting_feature_rows"])
    write_json(controller / "qualification.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-dir", required=True)
    parser.add_argument("--controller-dir", required=True)
    args = parser.parse_args()
    result = qualify(args.card_dir, args.controller_dir)
    print(f"qualified={result['qualified']} null_card={result.get('null_card')}")


if __name__ == "__main__":
    main()
