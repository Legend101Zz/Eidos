"""Assignment privacy and pool verification without any external data."""

import json
from pathlib import Path

import pytest

from study.exp.cards.common import CARD_FILES, MODEL_FILES, sha, write_json
from study.exp.cards.pools import assign, build_pool, verify


def catalog():
    return {"cards": [dict(card_id=f"R{i+1:03d}", source="pmlb", key=f"source-name-{i}",
                           family=f"family-{i // 2}", task_type="classification" if (i // 2) % 2 else "regression",
                           n_rows=(2000, 8000, 30000)[(i // 2) % 3], n_features=5,
                           row_band=(i // 2) % 3, imbalanced=False) for i in range(60)]}


def test_assignment_public_determinism_family_atomicity_and_private_h(tmp_path):
    sizes = {"D": 8, "E1": 8, "E2": 8, "E3": 8, "H": 12}
    first = assign(catalog(), sizes, 71, tmp_path / "private1/seed")
    again = assign(catalog(), sizes, 71, tmp_path / "private1/seed")
    other_hidden = assign(catalog(), sizes, 71, tmp_path / "private2/seed")
    assert first == again == other_hidden
    private = json.loads((tmp_path / "private1/assignments.json").read_text())
    family_pool = {}
    for pool, cards in private["pools"].items():
        assert len(cards) == sizes[pool]
        for card in cards:
            assert family_pool.setdefault(card["family"], pool) == pool
    public_ids = {c["card_id"] for cards in first["pools"].values() for c in cards}
    assert not public_ids & {c["card_id"] for c in private["pools"]["H"]}
    encoded = json.dumps(first)
    assert all(c["card_id"] not in encoded for c in private["reserve"])
    assert "H" not in first["pools"]
    assert "key" in first["pools"]["D"][0]
    for pool in ("E1", "E2", "E3"):
        assert not any("key" in c or "source" in c or "family" in c for c in first["pools"][pool])
    assert Path(tmp_path / "private1/seed").stat().st_size == 32


def fake_card(root, card_id="R001", qualified=True):
    card = root / "seal" / card_id
    bundle = card / "bundle"
    controller = root / "controller" / card_id
    bundle.mkdir(parents=True)
    controller.mkdir(parents=True)
    for name in MODEL_FILES - {"task.json"}:
        (bundle / name).write_text("fabricated")
    write_json(bundle / "task.json", dict(task_id=card_id, project=card_id,
               inputs={"train": {"shape": [600, 5]}},
               hashes={p.name: sha(p.read_bytes()) for p in bundle.iterdir()}))
    for name in ("test_inputs.npz", "test_labels.npz", "baseline_test.npz"):
        (card / name).write_text("fabricated")
    write_json(card / "manifest.json", dict(id=card_id, task_type="classification", metric="accuracy",
               min_reference_gain=0.02, qualified=qualified, seeds=[11, 23, 37], splits={"train": {"n": 600}},
               files={name: sha((card / name).read_bytes()) for name in CARD_FILES}))
    write_json(controller / "qualification.json", {"qualified": qualified})
    write_json(controller / "reference.json", {"null_card": False})
    return card


def test_build_verify_tampering_and_unqualified_exclusion(tmp_path):
    card = fake_card(tmp_path / "staging")
    rejected = fake_card(tmp_path / "rejected", "R002", False)
    root = tmp_path / "pool"
    result = build_pool("EXAMPLE", [card, rejected], root)
    assert result["excluded_unqualified"] == ["R002"]
    assert result["card_ids"] == ["R001"]
    assert verify(root) == result["commitment"]
    with pytest.raises(ValueError, match="commitment"):
        verify(root, "0" * 64)
    extra = root / "seal/extra"
    extra.write_text("tampered")
    with pytest.raises(ValueError, match="inventory"):
        verify(root)
    extra.unlink()
    (root / "seal/R001/manifest.json").write_text("tampered")
    with pytest.raises(ValueError, match="digest"):
        verify(root)


def test_private_seed_length_and_symlink_refusals(tmp_path):
    seed = tmp_path / "private/seed"
    seed.parent.mkdir()
    seed.write_bytes(b"short")
    with pytest.raises(ValueError, match="32 bytes"):
        assign(catalog(), {"D": 8, "H": 8}, 77, seed)
    root = tmp_path / "pool"
    build_pool("EXAMPLE", [fake_card(tmp_path / "staging")], root)
    path = root / "seal/R001/bundle/brief.md"
    original = tmp_path / "original"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(ValueError, match="symlink"):
        verify(root)


def test_standalone_build_enforces_null_allowance(tmp_path):
    cards = [fake_card(tmp_path / "staging", f"R{i+1:03d}") for i in range(3)]
    for card in cards:
        write_json(card.parent.parent / "controller" / card.name / "reference.json", {"null_card": True})
    with pytest.raises(ValueError, match="null-card allowance"):
        build_pool("D", cards[:2], tmp_path / "D")
    with pytest.raises(ValueError, match="null-card allowance"):
        build_pool("H", cards, tmp_path / "H")
    assert build_pool("H", cards[:2], tmp_path / "allowed-H")["card_ids"] == ["R001", "R002"]
