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


@pytest.mark.parametrize("before,after", [
    ("def f(x, cls):\n return cls(x)\n", "def f(y, factory):\n return factory(y)\n"),
    ("def f(x):\n try: return int(x)\n except ValueError as e: return str(e)\n",
     "def f(y):\n try: return int(y)\n except ValueError as err: return str(err)\n"),
    ("def f(x):\n return [lambda z: z+i for i in x]\n",
     "def f(y):\n return [lambda arg: arg+j for j in y]\n"),
    ("def f(x):\n def helper(z): return z+x\n return helper(x)\n",
     "def f(y):\n def inner(arg): return arg+y\n return inner(y)\n"),
    ("def f(x):\n class Helper:\n  def run(self): return x\n return Helper()\n",
     "def f(y):\n class Inner:\n  def run(this): return y\n return Inner()\n"),
    ("def f(x):\n def g():\n  nonlocal x\n  x += 1\n return g\n",
     "def f(y):\n def inner():\n  nonlocal y\n  y += 1\n return inner\n"),
])
def test_exp_novelty_m1_all_local_binding_forms(before, after):
    assert normalised_hash(ast.parse(before)) == normalised_hash(ast.parse(after))


class _AlphaRename(ast.NodeTransformer):
    """Independent Python-symbol-table based consistent local renamer."""

    def __init__(self, table):
        self.stack = [table]
        self.maps = {}

    def name(self, value):
        if value is None:
            return value
        for table in reversed(self.stack):
            try:
                symbol = table.lookup(value)
            except KeyError:
                continue
            if symbol.is_global():
                return value
            if table.get_type() == "function" and symbol.is_local():
                mapping = self.maps.setdefault(table.get_id(), {
                    n: f"renamed_binding_{table.get_id()}_{i}"
                    for i, n in enumerate(sorted(table.get_identifiers()))})
                return mapping[value]
            if table.get_type() == "class" and symbol.is_local():
                if table is self.stack[-1]:
                    return value
                continue
        return value

    def enter(self, node, name):
        table = next((t for t in self.stack[-1].get_children()
                      if t.get_lineno() == node.lineno and t.get_name() == name), None)
        if table is not None:
            self.stack.append(table)
        return table is not None

    def visit_Name(self, node):
        node.id = self.name(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self.name(node.arg)
        return node

    def visit_FunctionDef(self, node):
        name = node.name
        node.name = self.name(name)
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        node.args.defaults = [self.visit(n) for n in node.args.defaults]
        node.args.kw_defaults = [self.visit(n) if n else None for n in node.args.kw_defaults]
        self.enter(node, name)
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg]:
            if arg:
                self.visit_arg(arg)
        node.body = [self.visit(n) for n in node.body]
        self.stack.pop()
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        name = node.name
        node.name = self.name(name)
        node.bases = [self.visit(n) for n in node.bases]
        node.keywords = [self.visit(n) for n in node.keywords]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        self.enter(node, name)
        node.body = [self.visit(n) for n in node.body]
        self.stack.pop()
        return node

    def visit_Lambda(self, node):
        self.enter(node, "lambda")
        node = self.generic_visit(node)
        self.stack.pop()
        return node

    def visit_ListComp(self, node):
        node.generators[0].iter = self.visit(node.generators[0].iter)
        names = {ast.ListComp: "listcomp", ast.SetComp: "setcomp",
                 ast.DictComp: "dictcomp", ast.GeneratorExp: "genexpr"}
        entered = self.enter(node, names[type(node)])
        for i, gen in enumerate(node.generators):
            gen.target = self.visit(gen.target)
            if i:
                gen.iter = self.visit(gen.iter)
            gen.ifs = [self.visit(n) for n in gen.ifs]
        if isinstance(node, ast.DictComp):
            node.key, node.value = self.visit(node.key), self.visit(node.value)
        else:
            node.elt = self.visit(node.elt)
        if entered:
            self.stack.pop()
        return node

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_ExceptHandler(self, node):
        node.name = self.name(node.name)
        return self.generic_visit(node)

    def visit_alias(self, node):
        value = node.asname or node.name.split(".")[0]
        if self.name(value) != value:
            node.asname = self.name(value)
        return node

    def visit_Nonlocal(self, node):
        node.names = [self.name(n) for n in node.names]
        return node


def test_exp_novelty_m1_every_library_definition_is_renaming_invariant():
    import copy
    import symtable

    repo = Path(__file__).parents[2]
    files = sorted((repo / "components/eidos.search.recipe-portfolio/eidos_recipes").rglob("*.py"))
    files += [repo / "components/eidos.search.recipe-portfolio/templates/project.py"]
    checked = 0
    for path in files:
        source = path.read_text()
        original = ast.parse(source)
        definitions = [n for n in ast.walk(original) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        for definition in definitions:
            # Each definition gets its own local rename. Free/module names stay
            # unchanged, as they must when copying an individual library helper.
            isolated = ast.unparse(definition)
            before = ast.parse(isolated).body[0]
            after = _AlphaRename(symtable.symtable(isolated, path.name, "exec")).visit(copy.deepcopy(ast.parse(isolated))).body[0]
            assert normalised_hash(before) == normalised_hash(after), (path.name, definition.name, definition.lineno)
            checked += 1
    assert checked >= 95


@pytest.mark.parametrize("source", [
    "from sklearn.svm import SVC\nSVC = factory\nSVC()\n",
    "eval('x')\n", "exec('x')\n",
])
def test_exp_novelty_ambiguous_resolution(tmp_path, library, source):
    assert fingerprint(candidate(tmp_path, source), catalog(library[0]))["class"] == "unknown"


@pytest.mark.parametrize("source", [
    "from xgboost import XGBClassifier\nmodel=XGBClassifier()\n",
    "from lightgbm import LGBMClassifier as L\nmodel=L()\n",
    "from catboost import CatBoostRegressor\nmodel=CatBoostRegressor()\n",
    "import sklearn.svm as svm\nmodel=getattr(svm, 'NuSVC')()\n",
])
def test_exp_novelty_third_party_and_getattr(tmp_path, library, source):
    assert fingerprint(candidate(tmp_path, source), catalog(library[0]))["class"] == "external"


def test_exp_novelty_deep_expression_is_unknown(tmp_path, library):
    fp = fingerprint(candidate(tmp_path, "x=" + "+".join(["1"] * 950)), catalog(library[0]))
    assert fp["class"] == "unknown"
    assert fp["parse_errors"]


def test_exp_novelty_win_positive_but_less_than_one_se():
    run = dict(verified=True, **{"class": "external"}, predictions=[1, 0, 1, 0])
    auto = dict(predictions=[0, 1, 0, 0])
    result = novelty_win(run, auto, [1, 1, 1, 0], "accuracy")
    assert 0 < result["delta"] < result["se"]
    assert not result["novelty_win"]


def test_exp_novelty_requires_identity_even_when_no_changed_template():
    from study.exp.novelty import classify
    fp = dict(inputs={"x": "digest"}, provenance={"basis": "one-se-choice"}, template_identical=False)
    assert classify(fp) == "unknown"


def test_exp_novelty_symlink_is_refused(tmp_path, library):
    repo, source = library
    target = tmp_path / "target.py"
    target.write_text(source)
    root = candidate(tmp_path, source)
    (root / "linked.py").symlink_to(target)
    fp = fingerprint(root, catalog(repo))
    assert fp["parse_errors"] == ["linked.py"]
    assert fp["class"] == "unknown"


def test_exp_novelty_unedited_baseline_is_never_a_win(tmp_path):
    repo = Path(__file__).parents[2]
    source = (repo / "study/exp/cards/baseline_project.py").read_text()
    fp = fingerprint(candidate(tmp_path, source, basis="baseline-or-direct"), catalog(repo))
    assert fp["class"] == "baseline"
    assert not novelty_win(dict(verified=True, novelty=fp, predictions=[1, 1]),
                           dict(predictions=[0, 0]), [1, 1], "accuracy")["novelty_win"]



def test_exp_novelty_m1_whole_renamed_library_has_no_authored_definitions(tmp_path):
    import copy
    import symtable

    repo = Path(__file__).parents[2]
    component = repo / "components/eidos.search.recipe-portfolio"
    root = tmp_path / "export"
    paths = list((component / "eidos_recipes").rglob("*.py")) + [component / "templates/project.py"]
    for path in paths:
        source = path.read_text()
        renamed = _AlphaRename(symtable.symtable(source, path.name, "exec")).visit(copy.deepcopy(ast.parse(source)))
        dest = root / path.relative_to(component)
        write(dest, ast.unparse(renamed))
    write(root / "candidate_provenance.json", '{"basis":"one-se-choice"}')
    fp = fingerprint(root, catalog(repo))
    assert fp["authored"] == []
    assert fp["class"] == "catalog"


def test_exp_novelty_catalog_reads_the_recorded_git_build(library):
    import subprocess

    repo, _ = library

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Legend101Zz",
                               "-c", "user.email=Legend101Zz@users.noreply.github.com", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("add", "components")
    git("commit", "-qm", "Synthetic catalog fixture")
    commit = git("rev-parse", "HEAD")
    old = catalog(repo, commit)
    path = repo / "components/eidos.search.recipe-portfolio/templates/project.py"
    path.write_text("from sklearn.svm import NuSVC\nmodel=NuSVC()\n")
    assert catalog(repo, commit) == old
    assert "sklearn.svm.NuSVC" not in old["atoms"]
    assert "sklearn.svm.NuSVC" in catalog(repo)["atoms"]
    assert old["commit"] == commit


@pytest.mark.parametrize("before,after", [
    ("def f(path):\n with open(path) as handle: return handle.read()\n",
     "def f(filename):\n with open(filename) as stream: return stream.read()\n"),
    ("def f(value):\n global G\n G=value\n return G\n",
     "def f(item):\n global G\n G=item\n return G\n"),
    ("def f(value):\n import numpy as np\n return np.sum(value)\n",
     "def f(item):\n import numpy as array_library\n return array_library.sum(item)\n"),
    ("def f(value):\n match value:\n  case [a, *b]: return a,b\n",
     "def f(item):\n match item:\n  case [head, *tail]: return head,tail\n"),
    ("def f(*args, **kw):\n return args,kw\n", "def f(*values, **options):\n return values,options\n"),
])
def test_exp_novelty_m1_remaining_binding_syntax(before, after):
    assert normalised_hash(ast.parse(before)) == normalised_hash(ast.parse(after))


@pytest.mark.parametrize("before,after", [
    ("def f(x): return x+OFFSET", "def f(x): return x+SHIFT"),
    ("def f(x): return x.left", "def f(x): return x.right"),
    ("def f(x): return library_call(x)", "def f(x): return another_call(x)"),
    ("def f(x):\n a=1\n b=2\n return a", "def f(x):\n a=1\n b=2\n return b"),
])
def test_exp_novelty_m1_preserves_nonlocal_semantics(before, after):
    assert normalised_hash(ast.parse(before)) != normalised_hash(ast.parse(after))
