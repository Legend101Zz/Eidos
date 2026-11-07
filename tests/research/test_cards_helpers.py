"""Fast unit tests (no lab): card helpers and the regression scorer's strict contract."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")  # the lab venv has none; run with the research dev interpreter

from eidos.tasks.cards import card_charter, command_config, load_card  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CARD = {"schema_version": "eidos-task/0.2", "id": "X01", "project": "p", "permitted_operations": ["edit project code"],
        "limits": {"device": "cpu"}}


def test_card_charter_is_exploration_only(tmp_path):
    f = tmp_path / "X01.json"
    f.write_text(json.dumps(CARD))
    ch = card_charter(load_card(f), metric="rmse", direction="decrease")
    assert ch["exploration_only"] is True and ch["study_id"] == "x01-card" and "rmse" in ch["notes"]
    with pytest.raises(ValueError):
        card_charter(CARD, metric="m", direction="up")
    with pytest.raises(ValueError):
        load_card(_write(tmp_path, {**CARD, "schema_version": "other"}))


def _write(tmp_path, card):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(card))
    return f


def test_command_config():
    cfg = command_config(["python", "p.py", "--seed", "{seed}"], seed=3, files={"model": "m.pt"})
    assert cfg["seed"] == 3 and cfg["files"] == {"model": "m.pt"} and cfg["argv"][0] == "python"
    with pytest.raises(ValueError):
        command_config([])


class _Reader:
    def __init__(self, paths):
        self.paths = paths

    def path(self, port):
        return self.paths[port]


class _Writer:
    def __init__(self):
        self.staged = {}

    def stage_json(self, slot, obj, schema=None):
        self.staged[slot] = obj

    def stage_rows(self, slot, rows, schema=None):
        self.staged[slot] = rows


def _score():
    spec = importlib.util.spec_from_file_location("score_reg", REPO / "components/eidos.task.score-regression/score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run


def _run(tmp_path, ids, pred, rid=(1, 2, 3), y=(0.0, 1.0, 2.0)):
    np.savez(tmp_path / "p.npz", ids=np.asarray(ids), predictions=np.asarray(pred))
    np.savez(tmp_path / "r.npz", ids=np.asarray(rid), y=np.asarray(y))
    w = _Writer()
    out = _score()({}, {}, reader=_Reader({"predictions": tmp_path / "p.npz", "reference": tmp_path / "r.npz"}),
                   writer=w, context=SimpleNamespace())
    return out, w


def test_regression_scorer_reorders_and_scores(tmp_path):
    out, w = _run(tmp_path, [3, 1, 2], [3.0, 0.0, 1.0])
    assert out["metrics"]["mse"] == pytest.approx(1 / 3) and out["metrics"]["mae"] == pytest.approx(1 / 3)
    assert [r["id"] for r in w.staged["per_example"]] == [1, 2, 3]


@pytest.mark.parametrize("ids,pred", [([1, 2], [0.0, 1.0]), ([1, 1, 2], [0.0, 1.0, 2.0]), ([1, 2, 4], [0.0, 1.0, 2.0]),
                                      ([1, 2, 3], [0.0, np.nan, 2.0]), ([1, 2, 3], [0.0, np.inf, 2.0])])
def test_regression_scorer_rejects_malformed(tmp_path, ids, pred):
    with pytest.raises(ValueError):
        _run(tmp_path, ids, pred)
