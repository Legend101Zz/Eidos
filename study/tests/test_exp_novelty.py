import ast
import json
from pathlib import Path

import pytest

from study.exp.novelty import catalog, fingerprint, normalised_hash, novelty_win


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "repo"
    component = root / "components/eidos.search.recipe-portfolio"
    write(component / "eidos-component.json", '{"id":"portfolio","release":"1"}')
    write(component / "eidos_recipes/registry.py", 'TRANSFORMS={"scale":{}}\nMODELS={"ridge":{}}\n')
    source = ('from sklearn.linear_model import Ridge\n'
              'def train(data):\n    """A comment."""\n    model = Ridge(alpha=1)\n    return model.fit(data, data)\n')
    write(component / "templates/project.py", source)
    sweep = root / "components/eidos.search.recipe-sweep"
    write(sweep / "eidos-component.json", '{"id":"sweep","release":"1"}')
    write(sweep / "recipe_sweep.py", 'TEMPLATE_FAMILIES=("linear",)\nTEMPLATE_SETTINGS={"quick":{}}\n')
    return root, source


def candidate(tmp_path, source, basis="one-se-choice"):
    root = tmp_path / "candidate"
    write(root / "project.py", source)
    write(root / "candidate_provenance.json", json.dumps({"basis": basis}))
    return root


def test_exp_novelty_template_and_grid(tmp_path, library):
    repo, source = library
    cat = catalog(repo)
    assert cat["operators"] == ["scale"]
    assert cat["models"] == ["ridge"]
    assert cat["sweep_families"] == ["linear"]
    assert cat["sweep_templates"] == ["quick"]
    assert catalog(repo) == cat
    root = candidate(tmp_path, source)
    assert fingerprint(root, cat)["class"] == "catalog"
    write(root / "project.py", source.replace("alpha=1", "alpha=2"))
    assert fingerprint(root, cat)["class"] == "recombination"


@pytest.mark.parametrize("source,expected", [
    ('class Custom:\n def fit(self, x): return self\n def transform(self, x): return x\n', "external"),
    ('from sklearn.svm import SVC as S\nmodel = S()\n', "external"),
    ('import sklearn.svm as svm\nmodel = svm.SVC()\n', "external"),
    ('from sklearn.svm import *\nmodel=SVC()\n', "unknown"),
    ('def broken(', "unknown"),
])
def test_exp_novelty_classifications(tmp_path, library, source, expected):
    cat = catalog(library[0])
    fp = fingerprint(candidate(tmp_path, source), cat)
    assert fp["class"] == expected
    if " as S" in source:
        assert fp["atoms"] == ["sklearn.svm.SVC"]


def test_exp_novelty_copy_renamed_locals_is_known(tmp_path, library):
    repo, source = library
    renamed = source.replace("data", "values").replace("model =", "estimator =").replace("model.fit", "estimator.fit").replace('"""A comment."""', '"""New docstring."""')
    fp = fingerprint(candidate(tmp_path, renamed), catalog(repo))
    assert fp["authored"] == []
    assert fp["class"] == "catalog"
    assert normalised_hash(ast.parse(source).body[1]) == normalised_hash(ast.parse(renamed).body[1])


def test_exp_novelty_explicit_selection_and_missing_library(tmp_path, library):
    repo, source = library
    root = candidate(tmp_path, source, "explicit-export")
    assert fingerprint(root, catalog(repo))["class"] == "recombination"
    missing = catalog(tmp_path / "absent")
    assert len(missing["missing"]) == 2
    assert fingerprint(root, missing)["class"] == "unknown"


def test_exp_novelty_real_catalog_is_ast_only():
    cat = catalog(Path(__file__).parents[2])
    assert cat["missing"] == []
    assert "ridge" in cat["models"]
    assert "sklearn.svm.SVC" in cat["atoms"]
    assert "sklearn.ensemble.RandomForestRegressor" in cat["atoms"]
    assert "sklearn.neural_network.MLPClassifier" in cat["atoms"]
    assert cat["definition_hashes"]


def test_exp_novelty_real_portfolio_export_is_catalog(tmp_path):
    import shutil
    repo = Path(__file__).parents[2]
    component = repo / "components/eidos.search.recipe-portfolio"
    root = tmp_path / "candidate"
    shutil.copytree(component / "eidos_recipes", root / "eidos_recipes", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(component / "templates/project.py", root / "project.py")
    write(root / "candidate_provenance.json", '{"basis":"one-se-choice"}')
    fp = fingerprint(root, catalog(repo))
    assert fp["authored"] == []
    assert fp["class"] == "catalog"
    out = tmp_path / "run/out"
    shutil.copytree(root, out / "candidate/source")
    write(out.parent / "candidate_provenance.json", '{"basis":"one-se-choice"}')
    assert fingerprint(out, catalog(repo))["class"] == "catalog"


def test_exp_novelty_conditional_constructor_alias(tmp_path, library):
    source = ('from sklearn.svm import SVC as S\nfrom sklearn.svm import SVR\n'
              'constructor = S if classification else SVR\nmodel = constructor()\n')
    fp = fingerprint(candidate(tmp_path, source), catalog(library[0]))
    assert fp["atoms"] == ["sklearn.svm.SVC", "sklearn.svm.SVR"]
    assert fp["class"] == "external"


def test_exp_novelty_win_requires_verification_class_and_one_se():
    run = {"verified_success": True, "novelty": {"class": "external"}, "predictions": [0, 1, 0, 1]}
    auto = {"predictions": [1, 0, 1, 0]}
    result = novelty_win(run, auto, [0, 1, 0, 1], "accuracy")
    assert result == dict(novelty_win=True, delta=1, se=0, behavioural_distance=1)
    run["verified_success"] = False
    assert not novelty_win(run, auto, [0, 1, 0, 1], "accuracy")["novelty_win"]
    run["verified_success"] = True
    run["novelty"]["class"] = "catalog"
    assert not novelty_win(run, auto, [0, 1, 0, 1], "accuracy")["novelty_win"]
    assert novelty_win({}, {}, None, "rmse")["behavioural_distance"] is None
