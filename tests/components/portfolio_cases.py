"""Independent arithmetic, boundary, fit-leak and executor parity author tests."""

import copy
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "components/eidos.search.recipe-portfolio"
sys.path.insert(0, str(COMPONENT))
from eidos_recipes import cv, export, models, ops, registry, select, shift, spec  # noqa: E402


def recipe(kind="classification", family="logistic", steps=None, **params):
    return dict(schema="recipe-spec/1", task_type=kind, steps=steps or [{"op": "standardize"}],
                model=dict(family=family, params=params))


def data(root, kind="classification"):
    rng = np.random.default_rng(821)
    x = rng.normal(size=(180, 12))
    y = (x[:, 0] + .5 * x[:, 1] > 0).astype(int) if kind == "classification" else x[:, 0] * 2 + rng.normal(size=180) * .1
    for name, sl in (("train", slice(0, 120)), ("validation", slice(120, None))):
        np.savez(root / f"{name}.npz", x=x[sl], y=y[sl], ids=np.arange(180)[sl])
    (root / "task.json").write_text(json.dumps(dict(task_type=kind)))
    return x[:120], y[:120], x[120:], y[120:]


def raises(message, action):
    try:
        action()
    except (ValueError, pickle.UnpicklingError) as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"expected refusal: {message}")


def portfolio(root, config=None, kind="classification"):
    import portfolio as component
    data(root, kind)
    ports = {key: root / f"{key}.npz" for key in ("train", "validation")}
    return component.search(ports, (dict(task_type=kind, include=["linear", "neighbors"],
                                        scheme="cv-train", cv_folds=3, cv_repeats=1,
                                        budget=dict(max_seconds=25)) | (config or {})),
                            root / "scratch", seed=11)


def spec_grid(root):
    grid = recipe(C=[.1, 1, 10])
    grid["steps"][0]["with_mean"] = [True, False]
    values = spec.expand([grid])
    assert len(values) == 6 and len({spec.recipe_id(s) for s in values}) == 6
    grid["model"]["params"]["C"] = list(range(1, 402))
    raises("400", lambda: spec.expand([grid]))
    grid["model"]["params"]["C"] = []
    raises("empty", lambda: spec.expand([grid]))


def spec_errors(root):
    for value, message in [(recipe(steps=[{"op": "standardise"}]), "unknown operator"),
                            (recipe(typo=3), "unknown parameter"),
                            (recipe("regression"), "task_type"),
                            (recipe(C=-1), "C"), (recipe(C=True), "C")]:
        raises(message, lambda v=value: spec.validate(v))
    assert spec.validate(recipe()) == recipe()
    assert len(str(spec.validation_error(recipe(C=-1)))) < 160


def registry_case(root):
    for entry in registry.REGISTRY.values():
        assert entry["schema"] is not None and entry["task_types"] and entry["determinism"]
        assert entry["cost"](100, 10) > 0 and entry["checkpoint_bytes"](100, 10) > 0
        assert isinstance(entry["complexity"], int)


def folds(root):
    x, y, _, _ = data(root)
    a = cv.partitions(x, y, "classification", seed=123, folds=5, repeats=2)
    b = cv.partitions(x, y, "classification", seed=123, folds=5, repeats=2)
    assert cv.folds_digest(a) == cv.folds_digest(b)
    assert cv.folds_digest(a) != cv.folds_digest(cv.partitions(x, y, "classification", seed=124, folds=5, repeats=2))
    groups = np.repeat(np.arange(30), 4)
    for tr, va in cv.partitions(x, y, "classification", groups=groups, folds=3):
        assert not set(groups[tr]) & set(groups[va])


def in_fold(root):
    # 3,000 pure noise features, 96 rows: global supervised screening creates a large leak.
    rng = np.random.default_rng(989)
    x, y = rng.normal(size=(96, 3000)), np.tile([0, 1], 48)
    s = recipe(steps=[{"op": "importance_topk", "k": 1}, {"op": "standardize"}])
    splits = cv.partitions(x, y, "classification", folds=4, repeats=1)
    clean = cv.evaluate(s, x, y, partitions=splits, seed=11)
    scores = []
    for tr, va in splits:
        m = models.build(s, 11).fit(x[tr], y[tr])
        poison_x, poison_y = x.copy(), y.copy()
        poison_x[va] = rng.normal(100, 20, poison_x[va].shape)
        poison_y[va] = 1 - poison_y[va]
        other = models.build(s, 11).fit(poison_x[tr], poison_y[tr])
        assert pickle.dumps(m, protocol=5) == pickle.dumps(other, protocol=5)
    leaked = ops.ImportanceTopK(k=1).fit_transform(x, y)
    for tr, va in splits:
        m = models.build(recipe(), 11).fit(leaked[tr], y[tr])
        scores.append(np.mean(m.predict(leaked[va]) == y[va]))
    assert np.mean(scores) > clean["mean"] + .1, (scores, clean["mean"])


def paired_band(root):
    rows = [dict(id="best", scores=[.9, .7, .9, .7], complexity=9, export_eligible=True),
            dict(id="simple", scores=[.8, .8, .8, .8], complexity=1, export_eligible=True),
            dict(id="bad", scores=[.7] * 4, complexity=0, export_eligible=True)]
    result = select.choose(rows)
    assert result["chosen_id"] == "simple" and result["band_ids"] == ["best", "simple"]
    assert select.choose(rows, complexity_order={"best": 0, "simple": 10})["chosen_id"] == "best"
    rows[1]["export_eligible"] = False
    assert select.choose(rows)["chosen_id"] == "best"
    rows[0]["export_eligible"] = False
    raises("eligible", lambda: select.choose(rows))


def row_pairing(root):
    truth = np.array([0, 0, 1, 1])
    a, b = np.array([0, 1, 1, 1]), np.array([0, 0, 0, 1])
    comparison = select.paired_interval("classification", truth, a, b)
    assert comparison["effect"] == 0
    assert np.isclose(comparison["se"], np.sqrt(2 / 3) / 2)
    truth = np.arange(10.)
    comparison = select.paired_interval("regression", truth, truth + 1, truth + 2)
    assert comparison["effect"] == 1 and comparison["se"] == 0
    table, _, _ = portfolio(root, dict(scheme="train-fit-validation-score"))
    assert all(len(r["scores"]) == 60 for r in table["recipes"] if r["status"] == "ok")
    assert table["validation_check"]["untouched_by_selection"] is False


def oof(root):
    table, predictions, _ = portfolio(root)
    assert predictions["schema"] == "recipe-oof/1"
    for item in predictions["recipes"]:
        assert len(item["predictions"]) == len(item["row_ids"]) == len(item["fold_ids"]) == 120
        assert len(set(item["row_ids"])) == 120
    assert len(predictions["recipes"]) == sum(r["status"] == "ok" for r in table["recipes"])


def timeout(root):
    from eidos_recipes.worker import RecipeWorker
    x, y, xv, yv = data(root)
    with RecipeWorker(root / "worker", x, y, xv, yv, "classification", folds=3, repeats=1) as worker:
        assert worker.evaluate(recipe(), timeout=.00001)["status"] == "timeout"
        # A timed-out child is replaced; later work can complete.
        assert worker.evaluate(recipe(), timeout=15)["status"] == "ok"


def stable_digest(root):
    from eidos_recipes.util import evidence_digest
    a = dict(recipes=[dict(id="a", fit_seconds=2., scores=[.8, .9])])
    b = copy.deepcopy(a)
    b["recipes"][0]["fit_seconds"] = 123.
    assert evidence_digest(a) == evidence_digest(b)
    b["recipes"][0]["scores"][0] = .7
    assert evidence_digest(a) != evidence_digest(b)
    table, _, _ = portfolio(root)
    other = root / "repeat"
    other.mkdir()
    repeated, _, _ = portfolio(other)
    assert table["evidence_digest"] == repeated["evidence_digest"]


def heldout(root):
    import portfolio as component
    class Reader:
        def path(self, port):
            raise AssertionError("opened a port before checking names")
    raises("ports", lambda: component.run({"train": {}, "test": {}}, {}, reader=Reader(), writer=None, context=None))
    raises("held-out", lambda: component.search({"train": root / "testfeatures.npz", "validation": root / "v.npz"}, {}, root))


def command(root, source, *args):
    return subprocess.run([sys.executable, str(source / "project.py"), *map(str, args)],
                          cwd=source, capture_output=True, text=True, timeout=60, check=True)


def parity(root):
    table, _, source = portfolio(root)
    assert source["library_digest"] == export.library_digest()
    destination = root / "candidate"
    destination.mkdir()
    for name, text in source["files"].items():
        path = destination / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(text)
    for path in (COMPONENT / "eidos_recipes").glob("*.py"):
        assert path.read_bytes() == (destination / "eidos_recipes" / path.name).read_bytes()
    command(root, destination, "train", "--inputs", root, "--out", root / "trained", "--seed", 11)
    with np.load(root / "trained/validation_predictions.npz") as values:
        np.testing.assert_array_equal(values["predictions"], table["validation_check"]["predictions"])
    command(root, destination, "infer", "--model", root / "trained/model.pt", "--inputs", root / "validation.npz", "--out", root / "reloaded.npz")
    with np.load(root / "reloaded.npz") as values:
        np.testing.assert_array_equal(values["predictions"], table["validation_check"]["predictions"])


def check_compare(root):
    parity(root)
    source = root / "candidate"
    command(root, source, "check", "--model", root / "trained/model.pt", "--inputs", root / "validation.npz", "--expected", root / "trained/validation_predictions.npz", "--out", root / "check")
    assert json.loads((root / "check/receipt.json").read_text())["readiness_ok"] is True
    command(root, source, "compare", "--inputs", root / "validation.npz", "--candidate", root / "trained/validation_predictions.npz", "--baseline", root / "trained/validation_predictions.npz", "--out", root / "compare")
    assert json.loads((root / "compare/receipt.json").read_text())["paired_baseline"]["effect"] == 0
    with np.load(root / "trained/validation_predictions.npz") as values:
        np.savez(root / "wrong.npz", ids=values["ids"] + 1, predictions=values["predictions"])
    result = subprocess.run([sys.executable, str(source / "project.py"), "check", "--model", str(root / "trained/model.pt"), "--inputs", str(root / "validation.npz"), "--expected", str(root / "wrong.npz"), "--out", str(root / "badcheck")], capture_output=True)
    assert result.returncode != 0


def unpickler(root):
    class Exploit:
        def __reduce__(self):
            return os.system, ("false",)
    payload = pickle.dumps(Exploit())
    raises("not allowed", lambda: export.loads(payload))
    # A callable from an otherwise allowed package is refused too.
    raises("not allowed", lambda: export.loads(pickle.dumps(np.load)))


def checkpoint_guard(root):
    x, y, _, _ = data(root)
    model = models.build(recipe(), 11).fit(x, y)
    raises("checkpoint", lambda: export.save(root / "model.pt", model, recipe(), max_bytes=1))
    assert not (root / "model.pt").exists()


def rescore(root):
    import portfolio as component
    raises("reproduction", lambda: component.validate_config(dict(mode="rescore", purpose="confirmation", add=[recipe()])))
    table, _, _ = portfolio(root, dict(mode="rescore", add=[recipe()], partition_seed=71))
    assert table["purpose"] == "reproduction" and table["partition_seed"] == 71
    assert "resampled train partitions" in table["interpretation"]
    raises("partition_seed", lambda: component.validate_config(dict(mode="rescore", add=[recipe()])))


def coverage(root):
    from eidos_recipes.templates import portfolio_specs
    for kind in ("classification", "regression"):
        values = portfolio_specs(kind)
        assert 150 <= len(values) <= 250, len(values)
        assert registry.applicable(kind) <= {registry.hypothesis(s) for s in values}
        for s in values:
            spec.validate(s)
    table, _, _ = portfolio(root, dict(include="auto", budget=dict(max_seconds=45), cv_folds=2))
    assert table["coverage_complete"], table["class_summary"]


def operators(root):
    x, y, _, _ = data(root)
    for name in registry.TRANSFORMS:
        step = {"op": name}
        if name == "importance_topk":
            step["k"] = 4
        model = models.build(recipe(steps=[step]), 11).fit(x, y)
        assert model.predict(x).shape == y.shape, name
        export.save(root / "operator.pt", model, recipe(steps=[step]))
        np.testing.assert_array_equal(export.load(root / "operator.pt")["model"].predict(x), model.predict(x))


def models_case(root):
    for kind in ("classification", "regression"):
        x, y, _, _ = data(root, kind)
        for family in registry.MODELS:
            if kind not in registry.REGISTRY[family]["task_types"]:
                continue
            s = recipe(kind, family)
            model = models.build(s, 11).fit(x, y)
            prediction = model.predict(x)
            assert prediction.shape == y.shape and np.isfinite(prediction).all(), family
            path = root / "model.pt"
            export.save(path, model, s)
            loaded = export.load(path)
            np.testing.assert_array_equal(loaded["model"].predict(x), prediction)


def refine(root):
    table, _, _ = portfolio(root, dict(mode="refine", add=[recipe(C=[1., 1., 1., 1., 1., 1., 1., 1.])]))
    assert table["stop_reason"] == "family-gain-below-se"
    assert len(table["recipes"]) == 7


def shift_case(root):
    rng = np.random.default_rng(11)
    x = rng.normal(size=(250, 8))
    assert shift.detect(x, x + 4)["shift_detected"]
    assert not shift.detect(x, rng.normal(size=(250, 8)))["shift_detected"]


def toolkit(root):
    from study.exp.treatment import resolve
    from study.toolkit.build_toolkit import build_toolkit
    toolkit_path = build_toolkit(root, treatment=resolve("v2-portfolio"))
    x, y, _, _ = data(root)
    config = dict(inputs={p: dict(file=str(root / f"{p}.npz"), media_type="application/x-npz") for p in ("train", "validation")},
                  config=dict(task_type="classification", include=["linear"], cv_folds=2, cv_repeats=1, scheme="cv-train"))
    completed = subprocess.run([sys.executable, str(toolkit_path / "run_component.py"), "eidos.search.recipe-portfolio", "--args", json.dumps(config), "--out", str(root / "run")], capture_output=True, text=True, timeout=80)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest = json.loads((root / "run/staging/eidos-output-manifest.json").read_text())
    assert manifest["receipt"]["metrics"]["round_evaluated"] == 1.
    assert {"portfolio_table", "promotion_source", "oof"} <= {r["slot"] for r in manifest["outputs"]}


def seed_zero(root):
    from types import SimpleNamespace

    import portfolio as component
    data(root)
    class Reader:
        def path(self, port):
            return root / (port + ".npz")
    class Writer:
        def stage_json(self, slot, value, **kwargs):
            if slot == "portfolio_table":
                assert value["model_seed"] == 0
    component.run(dict(train={}, validation={}), dict(task_type="classification", include=["linear"],
                    scheme="cv-train", cv_folds=2, cv_repeats=1), reader=Reader(), writer=Writer(),
                  context=SimpleNamespace(seed=0, scratch_dir=root / "scratch"))


def large_plan(root):
    from eidos_recipes.templates import portfolio_specs
    for kind in ("classification", "regression"):
        values = portfolio_specs(kind, n=20000, d=10)
        trees = [s for s in values if registry.hypothesis(s) == "trees"]
        assert {s["model"]["family"] for s in trees} == {"extra_trees", "random_forest", "hgb"}
        assert trees[0]["model"]["family"] == "hgb"
        assert trees[0]["model"]["params"]["max_iter"] == 150
        assert models.build(trees[0], 11).named_steps["model"].early_stopping is False
        assert len({spec.recipe_id(s) for s in values}) == len(values)
        assert registry.applicable(kind) <= {registry.hypothesis(s) for s in values}
        kernels = [s for s in values if registry.hypothesis(s) == "kernel"]
        assert kernels[0]["model"]["family"] == "svm_linear"
        medium = portfolio_specs(kind, n=4000, d=10)
        assert next(s for s in medium if registry.hypothesis(s) == "kernel")["model"]["family"] == "svm_linear"


def multiclass_checkpoint(root):
    import warnings
    rng = np.random.default_rng(32)
    x = rng.normal(size=(200, 8))
    y = np.argmax(x[:, :3], axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for family, params in [("hgb", {"max_iter": 150}), ("mlp", {"width": 32, "members": 2, "max_iter": 50}),
                               ("knn", {}), ("gmm_class", {"k": 3, "covariance_type": "full"})]:
            s = recipe(family=family, **params)
            model = models.build(s, 11).fit(x, y)
            path = root / (family + ".pt")
            export.save(path, model, s)
            np.testing.assert_array_equal(export.load(path)["model"].predict(x), model.predict(x))
        x[:, 0] = np.arange(len(x)) % 4
        s = recipe(steps=[{"op": "onehot_lowcard"}])
        model = models.build(s, 11).fit(x, y)
        path = root / "onehot.pt"
        export.save(path, model, s)
        np.testing.assert_array_equal(export.load(path)["model"].predict(x), model.predict(x))


if __name__ == "__main__":
    case, directory = sys.argv[1:]
    checks = dict(registry=registry_case, models=models_case, shift=shift_case)
    checks.get(case, globals().get(case))(Path(directory))
