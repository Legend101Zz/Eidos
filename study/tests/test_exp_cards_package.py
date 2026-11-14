"""Fabricated tables exercise the actual baseline subprocess and seal contracts."""

from pathlib import Path

import numpy as np
import pytest

from study.exp.cards.common import CARD_FILES, MODEL_FILES, bar_for, inventory, read_json, run_cli, write_json
from study.exp.cards.package import package_card, prepare_arrays
from study.exp.cards.pools import build_pool, verify
from study.exp.cards.qualify import leakage_screen, qualify, verify_card


@pytest.fixture(scope="module")
def packaged(tmp_path_factory):
    root = tmp_path_factory.mktemp("packaged")
    rng = np.random.default_rng(57)
    x = rng.normal(size=(1600, 5)).astype(np.float32)
    x[:, -1] = 42
    y = np.where(x[:, 0] * x[:, 1] > 0, "original-class-a", "original-class-b")
    cards = [package_card(x, y, "classification", "R001", root / name, seed=42,
                          source_info={"source": "pmlb", "key": "original-source-name"},
                          columns=[f"original-column-{i}" for i in range(5)]) for name in ("a", "b")]
    return root, cards, x, y


def mark_qualified(card):
    manifest = read_json(card / "manifest.json")
    manifest["qualified"] = True
    write_json(card / "manifest.json", manifest)
    controller = card.parent.parent / "controller" / manifest["id"]
    write_json(controller / "qualification.json", {"qualified": True})
    write_json(controller / "reference.json", {"null_card": False})


def test_deterministic_bytes_layout_and_neutrality(packaged):
    _, cards, _, _ = packaged
    assert inventory(cards[0]) == inventory(cards[1])
    card = cards[0]
    assert set(inventory(card)) == CARD_FILES | {"manifest.json"}
    assert set(p.name for p in (card / "bundle").iterdir()) == MODEL_FILES
    assert verify_card(card)["min_reference_gain"] == 0.02
    task = read_json(card / "bundle/task.json")
    assert task["project"] == task["task_id"] == "R001"
    assert task["schema_version"] == "eidos-task-bundle/0.1"
    repository = Path(__file__).resolve().parents[2]
    assert (card / "bundle/answer_schema.py").read_bytes() == (
        repository / "research/public/development/validate_answer.py").read_bytes()
    assert (card / "bundle/requirements.lock").read_bytes() == (
        repository / "study/packaging/bundles/N01/requirements.lock").read_bytes()
    for path in card.rglob("*"):
        if path.is_file():
            assert b"original-" not in path.read_bytes()
    ids = []
    for path in (card / "bundle/train.npz", card / "bundle/validation.npz", card / "test_inputs.npz"):
        with np.load(path, allow_pickle=False) as data:
            assert data["x"].dtype == np.float32
            assert data["ids"].dtype == np.int64
            ids.extend(data["ids"].tolist())
    assert len(ids) == len(set(ids))


def test_train_only_statistics_and_duplicate_rows():
    rng = np.random.default_rng(8)
    x = rng.normal(size=(1800, 5)).astype(np.float32)
    y = rng.normal(size=1800) * 70 + 300
    original, provenance = prepare_arrays(x, y, "regression", 71)
    test_rows = provenance["split_source_rows"]["test"]
    x[test_rows] += 10000
    y[test_rows] -= 9999
    changed, new_provenance = prepare_arrays(x, y, "regression", 71)
    for key in ("x", "y", "ids"):
        np.testing.assert_array_equal(original["train"][key], changed["train"][key])
    assert provenance["target_scaling"] == new_provenance["target_scaling"]
    assert abs(original["train"]["y"].mean()) < 1e-6
    assert abs(original["train"]["y"].std() - 1) < 1e-6
    _, p = prepare_arrays(np.concatenate([x, x[:5]]), np.concatenate([y, y[:5]]), "regression", 71)
    assert p["duplicate_rows_dropped"] == 5
    assert bar_for(2.5, "regression") == 0.125
    with pytest.raises(ValueError, match="600"):
        prepare_arrays(x[:100], y[:100], "regression", 71)


def test_baseline_cli_roundtrip(packaged, tmp_path):
    _, cards, _, _ = packaged
    card = cards[0]
    predictions, ids, seconds, size = run_cli(card / "bundle/project.py", card / "bundle",
                                             card / "test_inputs.npz", tmp_path, 11)
    with np.load(card / "baseline_test.npz", allow_pickle=False) as original:
        np.testing.assert_array_equal(predictions, original["predictions"])
        np.testing.assert_array_equal(ids, original["ids"])
    assert seconds < 120 and size < 64 * 1024 * 1024
    receipt = read_json(tmp_path / "receipt.json")
    assert receipt["steps"] == 80 and receipt["threads"] == 1


def test_regression_bar_and_baseline(tmp_path):
    rng = np.random.default_rng(88)
    x = rng.normal(size=(1600, 3)).astype(np.float32) * [1e5, 1, 0.0001]
    y = np.sin(x[:, 1]) + rng.normal(size=1600) * 0.1
    card = package_card(x, y, "regression", "R002", tmp_path)
    manifest = verify_card(card)
    assert manifest["min_reference_gain"] == 0.05 * manifest["baseline"]["score"]
    assert np.isfinite(manifest["baseline"]["score"])
    assert read_json(card / "bundle/task.json")["nclasses"] == 1


def test_commitment_matches_private_seal_and_detects_tampering(packaged, tmp_path, monkeypatch):
    import study.private_seal as seal_module

    _, cards, _, _ = packaged
    mark_qualified(cards[0])
    built = build_pool("TEST", [cards[0]], tmp_path / "pool")
    root = tmp_path / "pool"
    assert verify(root) == built["commitment"]
    monkeypatch.setattr(seal_module, "PRIVATE_CARD_BARS", {"R001": ("accuracy", 0.02)})
    seal = seal_module.PrivateSeal(root / "seal", built["commitment"])
    seal.stage_bundle("R001", tmp_path / "staged")
    changed = root / "seal/R001/bundle/brief.md"
    changed.write_text(changed.read_text() + "tamper")
    with pytest.raises(ValueError, match="digest"):
        verify(root)


def test_leakage_and_fail_closed_hashes(packaged, tmp_path):
    _, cards, _, _ = packaged
    x = np.arange(100)[:, None]
    assert not leakage_screen(x, (x[:, 0] > 40).astype(int), "classification")["passed"]
    assert not leakage_screen(x, x[:, 0] * 2, "regression")["passed"]
    import shutil

    card = tmp_path / "seal/R001"
    controller = tmp_path / "controller/R001"
    shutil.copytree(cards[1], card)
    controller.mkdir(parents=True)
    (card / "bundle/project.py").write_text("raise AssertionError('must never execute damaged code')")
    result = qualify(card, controller)
    assert not result["qualified"] and not result["checks"]["hashes"]["passed"]


def test_full_qualification_records_fresh_seed_checks(packaged):
    from study.exp.cards.reference import reference

    _, cards, _, _ = packaged
    card = cards[1]
    controller = card.parent.parent / "controller/R001"
    measured = reference(card, controller, budget_seconds=20, max_configurations=3)
    result = qualify(card, controller)
    assert result["qualified"] == all(check["passed"] for check in result["checks"].values())
    assert result["checks"]["baseline_11"]["passed"]
    for seed in (11, 23, 37):
        baseline_check = result["checks"][f"baseline_{seed}"]
        reference_check = result["checks"][f"reference_{seed}"]
        assert baseline_check["retrain_seconds"] < 120
        assert reference_check["passed"]
        assert reference_check["checkpoint_bytes"] < 64 * 1024 * 1024
    assert not measured["null_card"] and not result["null_card"]
