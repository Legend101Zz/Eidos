"""Train-only reference search and arithmetic on fabricated nonlinear/noise tasks."""

import ast
import tempfile
from pathlib import Path

import numpy as np
import pytest

from study.exp.cards.common import read_json, run_cli, score, write_json
from study.exp.cards.reference import (
    configurations,
    greedy_ensemble,
    paired_se,
    run_reference,
    select_reference,
)


@pytest.mark.parametrize("kind", ["classification", "regression"])
def test_nonlinear_reference_and_export_reproduction(tmp_path, kind):
    rng = np.random.default_rng(19)
    x = rng.uniform(-2, 2, (850, 4)).astype(np.float32)
    y = ((x[:, 0] * x[:, 1] > 0).astype(np.int64) if kind == "classification" else
         (x[:, 0] ** 2 + x[:, 1] ** 2).astype(np.float32))
    directory = tmp_path / "controller"
    result = run_reference(x[:650], y[:650], x[650:], y[650:], kind, budget_seconds=20,
                           max_configurations=3, output_dir=directory)
    assert result["ref_gain"] > result["bar"]
    assert not result["null_card"]
    assert len(result["ref_scores"]) == len(result["retrain_seconds"]) == 3
    spec = read_json(directory / "reference_spec.json")
    assert spec["ensemble"]["bags"] == 10
    assert abs(sum(m["weight"] for m in spec["members"]) - 1) < 1e-10
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    write_json(bundle / "task.json", {"task_type": kind, "nclasses": 2 if kind == "classification" else 1})
    for split in ("train", "validation"):
        np.savez(bundle / f"{split}.npz", x=x[:650], y=y[:650], ids=np.arange(650))
    inputs = tmp_path / "inputs.npz"
    np.savez(inputs, x=x[650:], ids=np.arange(200))
    with tempfile.TemporaryDirectory() as temporary:
        prediction, _, _, _ = run_cli(directory / "reference_project.py", bundle, inputs, Path(temporary), 11)
    with np.load(directory / "reference_test.npz", allow_pickle=False) as expected:
        np.testing.assert_array_equal(prediction, expected["predictions"])
    assert score(y[650:], prediction, kind) == result["ref_scores"][0]


def test_selection_does_not_read_evaluation_arrays():
    rng = np.random.default_rng(29)
    x = rng.normal(size=(120, 4)).astype(np.float32)
    y = (x[:, 0] > 0).astype(int)
    clean = select_reference(x, y, "classification", budget_seconds=10, max_configurations=1,
                             x_test=x, y_test=y, x_validation=x, y_validation=y)
    poisoned = select_reference(x, y, "classification", budget_seconds=10, max_configurations=1,
                                x_test=np.full_like(x, np.nan), y_test=np.full(len(y), np.nan),
                                x_validation=np.full_like(x, np.nan), y_validation=np.full(len(y), np.nan))
    assert [m["config"] for m in clean["members"]] == [m["config"] for m in poisoned["members"]]
    assert clean["ensemble"] == poisoned["ensemble"]
    assert clean["cv"] == {"folds": 5, "repeats": 2, "train_only": True}


def test_import_independence_and_recipe_families():
    root = Path(__file__).resolve().parents[1] / "exp/cards"
    for file in ("reference.py", "reference_runtime.py", "common.py"):
        tree = ast.parse((root / file).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert all(not name.startswith(("components", "eidos")) for name in names)
    configs = configurations("classification", 600)
    assert {c["family"] for c in configs} == {"linear", "knn", "rbf", "extra", "forest", "hist", "mlp"}
    assert sum(c["family"] == "hist" for c in configs) == 30
    assert not any(c["family"] == "rbf" for c in configurations("regression", 4000))


def test_ensemble_replacement_bookkeeping_and_paired_se():
    y = np.array([0, 1, 0, 1])
    base = np.array([0, 0, 1, 1])
    ref = np.array([0, 1, 0, 1])
    delta = np.array([0.0, 1.0, 1.0, 0.0])
    assert paired_se(y, base, ref, "classification") == pytest.approx(delta.std(ddof=1) / 2)
    assert paired_se(np.zeros(4), np.zeros(4), np.zeros(4), "regression") == 0
    predictions = [np.array([0.0, 1.0]) + i * 0.01 for i in range(10)]
    weights, evidence = greedy_ensemble(predictions, np.array([0.005, 1.005]), "regression", steps=20)
    assert weights.sum() == pytest.approx(1)
    assert (weights >= 0).all()
    assert evidence["ensemble_score"] <= evidence["best_single_score"] + 1e-12
    assert len(evidence["top_indices"]) == 2


def test_pure_noise_null_card(tmp_path):
    rng = np.random.default_rng(900)
    x = rng.normal(size=(1000, 3)).astype(np.float32)
    y = rng.integers(0, 2, 1000)
    # A noise baseline at chance; the large margin prevents treating random fluctuation as headroom.
    baseline = y[600:].copy()
    baseline[:190] = 1 - baseline[:190]
    result = run_reference(x[:600], y[:600], x[600:], y[600:], "classification", budget_seconds=10,
                           max_configurations=1, baseline_predictions=baseline, output_dir=tmp_path)
    assert result["null_card"]
    assert result["headroom"] >= 2 * result["se_paired"]
