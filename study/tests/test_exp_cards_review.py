"""Regression probes for the independent review, including artifact-level privacy attacks."""

import gzip
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import numpy as np
import pytest

from study.exp.cards import intake, qualify, reference
from study.exp.cards.common import read_json, sha, write_json
from study.exp.cards.package import package_card, prepare_arrays
from study.exp.cards.pools import assign
from study.exp.cards.select import feature_signature, likely_synthetic, same_feature_family
from study.tests.test_exp_cards_build import catalog, run
from study.tests.test_exp_cards_pools import fake_card


def test_train_only_constants_stratification_and_random_ids():
    rng = np.random.default_rng(3)
    x = rng.normal(size=(2400, 5)).astype(np.float32)
    y = np.arange(2400) % 10
    data, provenance = prepare_arrays(x, y, "classification", 7)
    test = provenance["split_source_rows"]["test"]
    x[:, -1] = 0
    x[test, -1] = 1
    changed, p = prepare_arrays(x, y, "classification", 7)
    assert 4 in p["dropped_columns"]
    for split, values in data.items():
        counts = Counter(values["y"])
        assert max(counts.values()) - min(counts.values()) <= 1
        ids = values["ids"]
        assert ids.min() >= 0 and ids.max() > 2**50
        assert not np.array_equal(ids, provenance["split_source_rows"][split])
        assert changed[split]["x"].shape[1] == 4


def test_split_duplicates_targets_and_ids():
    arrays = {name: (np.array([[1., 2.]], dtype=np.float32), np.array([label]), np.array([i]))
              for i, (name, label) in enumerate((("train", 0), ("validation", 1), ("test", 2)))}
    result = qualify.split_check(arrays)
    assert result["passed"] and result["conflicting_feature_rows"] == 3
    arrays["validation"] = (arrays["validation"][0], np.array([0]), np.array([1]))
    assert not qualify.split_check(arrays)["passed"]
    # Multiple labels per feature must not turn the conflict count negative.
    both = {name: (np.repeat(data[0], 2, axis=0), np.array([0, 1]), np.array([2 * i, 2 * i + 1]))
            for i, (name, data) in enumerate(arrays.items())}
    assert qualify.split_check(both)["conflicting_feature_rows"] == 3
    arrays["validation"] = (arrays["validation"][0], np.array([1]), np.array([0]))
    assert not qualify.split_check(arrays)["passed"]


@pytest.fixture(scope="module")
def control_card(tmp_path_factory):
    root = tmp_path_factory.mktemp("review-control")
    rng = np.random.default_rng(5)
    x = rng.normal(size=(1600, 4)).astype(np.float32)
    y = rng.integers(0, 3, len(x))
    card = package_card(x, y, "classification", "R9001", root, seed=79)
    controller = root / "controller/R9001"
    project = controller / "reference_project.py"
    project.write_text("# controlled reference\n")
    write_json(controller / "reference.json", dict(reference_py_sha256=sha(project.read_bytes())))
    return card, controller


def test_qualification_planted_null_and_size_independently(control_card, monkeypatch):
    card, controller = control_card
    with np.load(card / "baseline_test.npz") as data:
        predictions, ids = data["predictions"].copy(), data["ids"].copy()
    with np.load(card / "test_labels.npz") as data:
        truth = data["y"].copy()
    assert read_json(card / "manifest.json")["nclasses"] == 3
    assert read_json(card / "bundle/task.json")["nclasses"] == 3
    def execute(project, bundle, inputs, output, seed):
        planted = project.name == "project.py" and seed == 23
        return (truth if planted else predictions), ids, 1., 100
    monkeypatch.setattr(qualify, "run_cli", execute)
    result = qualify.qualify(card, controller)
    assert not result["qualified"] and not result["checks"]["baseline_23"]["passed"]
    monkeypatch.setattr(qualify, "run_cli", lambda *args: (predictions, ids, 1., 100))
    assert qualify.qualify(card, controller)["qualified"]
    monkeypatch.setattr(qualify, "run_cli", lambda *args: (predictions, ids, 1., 64 * 1024 * 1024 + 1))
    result = qualify.qualify(card, controller)
    assert not result["qualified"] and not result["checks"]["reference_11"]["passed"]


def test_redirect_allowlist_and_repository_path():
    for url in ("https://evil.example/data", "http://github.com/EpistasisLab/pmlb/data",
                "https://github.com/other/repo/file", "https://raw.githubusercontent.com/other/repo/file",
                "https://github.com/EpistasisLab/pmlb/../other", "https://github.com:444/EpistasisLab/pmlb/a"):
        with pytest.raises(ValueError):
            intake._Redirects().redirect_request(Request("https://www.openml.org/data"), None, 302, "Found", {}, url)
    for host, path in (("github.com", "/EpistasisLab/pmlb/raw/master/a"),
                       ("raw.githubusercontent.com", "/EpistasisLab/pmlb/master/a"),
                       ("media.githubusercontent.com", "/media/EpistasisLab/pmlb/master/a")):
        assert intake._Redirects().redirect_request(Request("https://www.openml.org/data"), None, 302, "Found", {},
                                                   f"https://{host}{path}") is not None


def test_intake_expansion_and_rows_bounded(monkeypatch):
    monkeypatch.setattr(intake, "MAX_BYTES", 64)
    with pytest.raises(ValueError, match="200 MB"):
        intake.parse_pmlb(gzip.compress(b"a" * 65))
    monkeypatch.setattr(intake, "MAX_ROWS", 2)
    with pytest.raises(ValueError, match="row limit"):
        intake.parse_pmlb(gzip.compress(b"a\ttarget\n1\t0\n2\t1\n3\t0\n"))


def test_feature_families_and_synthetic_flags():
    x = np.random.default_rng(4).normal(size=(1600, 5))
    a = feature_signature(x)
    assert a == feature_signature(x[::-1, ::-1])
    assert a == feature_signature(x.astype(str).astype(float))
    assert same_feature_family(a, feature_signature(np.column_stack([x, np.arange(len(x))])))
    assert same_feature_family(a, feature_signature(x[:, :4]))
    assert not same_feature_family(a, feature_signature(x + 10))
    for name in ("feynman_x", "strogatz_x", "BNGfoo", "2dplanes", "ring", "twonorm", "waveform",
                 "mv", "fried", "GAMETES_x", "parity", "monk", "led", "Hill_Valley", "clean1", "banana"):
        assert likely_synthetic(name)
    assert not likely_synthetic("phoneme")


def test_caruana_regression_ordering():
    y = np.arange(20, dtype=float)
    predictions = [y + error for error in (0.2, 0.1, 5, 4, 3, 2, 1, 6, 7, 8)]
    weights, evidence = reference.greedy_ensemble(predictions, y, "regression")
    assert evidence["best_single_index"] == 1
    assert evidence["top_indices"] == [1, 0]
    assert weights[1] == 1
    assert all(1 in bag for bag in evidence["bag_libraries"])


def test_search_size_constraint_and_family_floor(monkeypatch):
    rng = np.random.default_rng(9)
    x = rng.normal(size=(120, 4)).astype(np.float32)
    y = x[:, 0] * x[:, 1]
    # Linear fits, but the tree cannot meet this tiny constraint: inspect the real CV worker result.
    with pytest.raises(RuntimeError, match="three families") as failure:
        reference.select_reference(x, y, "regression", budget_seconds=20, max_configurations=3, size_limit_bytes=5000)
    assert "excluded_for_size" in str(failure.value)


def test_export_shrinks_actual_forest(control_card, tmp_path):
    card, _ = control_card
    spec = dict(task_type="classification", search_seconds=0, n_configurations=3,
                dropped_for_size=[], dropped_for_retrain_budget=[],
                members=[dict(config=dict(family="extra", params=dict(n_estimators=100)), weight=1.,
                              cv_seconds=1., estimated_bytes=1000000)])
    with np.load(card / "test_labels.npz") as data:
        y = data["y"].copy()
    with np.load(card / "baseline_test.npz") as data:
        baseline = data["predictions"].copy()
    result = reference.evaluate_spec(spec, card / "bundle", card / "test_inputs.npz", y, baseline,
                                     0.3, 0.02, tmp_path, size_limit_bytes=50000)
    assert result["dropped_for_size"]
    assert max(result["checkpoint_bytes"]) <= 50000
    assert spec["members"][0]["config"]["params"]["n_estimators"] < 100


def test_h_draw_no_thin_cell_forcing_and_quotas(tmp_path):
    cards = [dict(card_id=f"R{i:04d}", key=f"source-{i}", family=f"family-{i}", n_rows=2000,
                  task_type="classification" if i else "regression", synthetic=i < 10, null_card=10 <= i < 20)
             for i in range(80)]
    included = 0
    for seed in range(60):
        private = tmp_path / str(seed)
        private.mkdir()
        (private / "seed").write_bytes(seed.to_bytes(32, "big"))
        assign(cards, dict(D=8, E1=8, H=12), 19, private / "seed")
        pools = read_json(private / "assignments.json")["pools"]
        included += any(c["card_id"] == "R0000" for c in pools["H"])
        for pool, members in pools.items():
            assert sum(c["synthetic"] for c in members) <= (3 if pool == "H" else 2)
            assert sum(c["null_card"] for c in members) <= (2 if pool == "H" else 1)
    assert 0 < included < 35


def immediate_launch(action, request, directory):
    """Keep the large privacy attack fast; separate driver tests exercise real worker concurrency."""
    from study.exp.cards.select import check_table

    directory.mkdir(parents=True, exist_ok=True)
    response = directory / f"{action}.response.json"
    candidate = request["candidate"]
    if action == "preflight":
        source = Path(request["cache"]) / "raw" / candidate["source"] / candidate["key"]
        result = check_table(intake.load_table(source), candidate["task_type"], seed=request["seed"],
                             max_classes=request["max_classes"])
        result.update(ok=True, source_dir=str(source))
    else:
        card = fake_card(Path(request["root"]), request["card_id"])
        manifest = read_json(card / "manifest.json")
        manifest.update(task_type=candidate["task_type"],
                        metric="accuracy" if candidate["task_type"] == "classification" else "rmse")
        write_json(card / "manifest.json", manifest)
        write_json(card.parent.parent / "controller" / request["card_id"] / "provenance.json", candidate)
        result = dict(ok=True, qualified=True, null_card=False)
    write_json(response, result)
    return SimpleNamespace(poll=lambda: 0), response


def test_public_artifacts_elimination_attack_two_private_seeds(tmp_path):
    snapshots = []
    for seed in (31, 97):
        path, cache, private = catalog(tmp_path / str(seed), 80)
        (private / "assignment.seed").write_bytes(seed.to_bytes(32, "big"))
        result = run(path, cache, private, want=dict(D=8, E1=8, H=12), workers=3, launch=immediate_launch)
        assignment = read_json(private / "assignments.json")
        d = {c["key"] for c in assignment["pools"]["D"]}
        candidates = {c["key"] for c in read_json(path)["cards"]}
        public_files = {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob("*") if p.is_file()}
        public = b"\n".join(public_files.values())
        assert not (cache / "raw").exists() and not (cache / "staging").exists()
        assert not (cache / "pools/E1").exists() and not (cache / "manifests/H.json").exists()
        e = read_json(cache / "manifests/E1.json")
        assert set(e) == {"pool", "commitment", "cards"}
        assert all(set(c) == {"id", "task_type", "metric", "bar_rule"} for c in e["cards"])
        # The attacker has no per-candidate observation beyond D, including attempted/rejected outcomes.
        possible_h = set(candidates)
        for key in candidates:
            if key.encode() in public:
                assert key in d
                possible_h.remove(key)
        assert possible_h == candidates - d and len(possible_h) >= 60
        # E identifiers are intentionally public; H/reserve/unqualified identifiers are private.
        public_ids = {c["card_id"] for c in assignment["pools"]["D"]} | {c["id"] for c in e["cards"]}
        state = read_json(private / "build-state.json")
        for record in state["outcomes"].values():
            if "card_id" in record and record["card_id"] not in public_ids:
                assert record["card_id"].encode() not in public
        assert result["processed"] == len(candidates)
        snapshots.append((result, public_files["build-state.json"]))
    # Progress artifacts are counts only, independent of the identity permutation/H draw.
    assert snapshots[0] == snapshots[1]


def test_max_classes_and_cheap_checks_before_reference(control_card, tmp_path, monkeypatch):
    import shutil

    from study.exp.cards import build, package
    from study.exp.cards.select import check_table

    rng = np.random.default_rng(13)
    table = dict(x=rng.normal(size=(1600, 4)), y=np.arange(1600) % 3)
    assert check_table(table, "classification", max_classes=3)["n_rows"] == 1600
    with pytest.raises(ValueError, match="max-classes"):
        check_table(table, "classification", max_classes=2)
    card, _ = control_card
    root = tmp_path / "staging"
    shutil.copytree(card.parent.parent, root)
    monkeypatch.setattr(package, "package", lambda *args: root / "seal/R9001")
    monkeypatch.setattr(qualify, "qualify", lambda *args, **kwargs: dict(qualified=False, checks={"leakage": dict(passed=False)}))
    def unexpected(*args, **kwargs):
        pytest.fail("reference search ran before cheap checks passed")
    monkeypatch.setattr(reference, "reference", unexpected)
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    write_json(request, dict(candidate=dict(task_type="classification"), root=str(root), card_id="R9001",
                             source_dir="unused", seed=11, budget_seconds=1))
    # Worker discards interrupted output before invoking package; return a separate already packaged card.
    monkeypatch.setattr(package, "package", lambda *args: card)
    # Use the original controller via a harmless copied provenance, since the worker owns root/controller.
    def packaged(*args):
        shutil.copytree(card.parent.parent / "controller/R9001", root / "controller/R9001")
        return card
    monkeypatch.setattr(package, "package", packaged)
    build._worker("build", request, response)
    result = read_json(response)
    assert result["ok"] and not result["qualified"] and result["failed_checks"] == ["leakage"]


def test_card_api_selection_ignores_poisoned_validation_and_test(control_card, tmp_path, monkeypatch):
    import shutil

    original, _ = control_card
    captured = []
    monkeypatch.setattr(reference, "evaluate_spec", lambda spec, *args, **kwargs: captured.append(spec))
    for poison in (False, True):
        card = tmp_path / str(poison)
        shutil.copytree(original, card)
        if poison:
            with np.load(card / "test_labels.npz") as labels:
                ids = labels["ids"].copy()
            np.savez(card / "test_labels.npz", ids=ids, y=np.full(len(ids), np.nan))
            np.savez(card / "bundle/validation.npz", x=np.full((20, 4), np.nan), y=np.full(20, np.nan), ids=np.arange(20))
        reference.reference(card, tmp_path / "unused", budget_seconds=20, max_configurations=3)
    assert [m["config"] for m in captured[0]["members"]] == [m["config"] for m in captured[1]["members"]]
    assert captured[0]["ensemble"] == captured[1]["ensemble"]


def test_legacy_public_private_pool_is_refused(tmp_path):
    path, cache, private = catalog(tmp_path, 1)
    (cache / "pools/E1").mkdir(parents=True)
    with pytest.raises(ValueError, match="legacy private pools"):
        run(path, cache, private, want=dict(D=1))


def test_assignment_uses_anonymous_slots_not_source_name_ranks(tmp_path):
    cards = [dict(card_id=f"R{i:04d}", key=f"source-{i}", family=f"family-{i:03d}",
                  task_type="classification", n_rows=2000) for i in range(70)]
    expected = None
    for index, data in enumerate((cards, [dict(c, family=f"renamed-{69-i:03d}") for i, c in enumerate(cards)])):
        private = tmp_path / str(index)
        private.mkdir()
        (private / "seed").write_bytes(b"p" * 32)
        assign(data, dict(D=8, E1=8, H=12), 71, private / "seed")
        assignment = read_json(private / "assignments.json")
        allocated = {p: [c["card_id"] for c in members] for p, members in assignment["pools"].items()}
        if expected is None:
            expected = allocated
        else:
            assert allocated == expected
