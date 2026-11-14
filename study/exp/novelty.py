"""Mechanical novelty, without importing or executing inspected code.

Decision table (ordered): parse failure/incomplete catalog/ambiguous resolution
=> unknown; byte/normalised-AST-identical supplied baseline => baseline (never
a win); outside estimator/transform constructor, authored estimator class or
authored fit/predict/transform method => external; known atoms with authored
glue, changed template code, or non-rule provenance => recombination; an intact
template/library export with one-se-choice provenance => catalog. Insufficient
identity evidence => unknown. Unknown receives catalog credit (zero novelty).
Provenance is a descriptor, never proof that modified code is a catalog export.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

from study.exp.stats import paired_rows_delta

PREFIXES = ("sklearn.", "numpy.", "scipy.", "torch.", "xgboost.", "lightgbm.", "catboost.")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _atoms(tree):
    aliases, ambiguous = {}, False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for item in node.names:
                if item.name == "*" and node.module.split(".")[0] in {p.split(".")[0] for p in PREFIXES}:
                    ambiguous = True
                aliases[item.asname or item.name] = node.module + "." + item.name
    # A shadowed import cannot establish which constructor was called.
    bound = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    if bound & aliases.keys():
        ambiguous = True
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    assignments.setdefault(target.id, []).append(node.value)

    def names(node, seen=frozenset()):
        if isinstance(node, ast.IfExp):
            return names(node.body, seen) | names(node.orelse, seen)
        if isinstance(node, ast.Name):
            if node.id in aliases:
                return {aliases[node.id]}
            if node.id in assignments and node.id not in seen:
                return set().union(*(names(value, seen | {node.id}) for value in assignments[node.id]))
            return {node.id}
        if isinstance(node, ast.Attribute):
            return {base + "." + node.attr for base in names(node.value, seen)}
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) == 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            return {base + "." + node.args[1].value for base in names(node.args[0], seen)}
        return set()

    atoms = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for name in names(node.func):
                if name.startswith(PREFIXES):
                    atoms.add(name)
                if name in {"eval", "exec", "__import__", "importlib.import_module"}:
                    ambiguous = True
    return atoms, ambiguous


class _Bindings(ast.NodeVisitor):
    """Collect bindings in one lexical scope, in source order."""

    def __init__(self):
        self.names = []
        self.globals = set()
        self.nonlocals = set()

    def add(self, name):
        if name and name not in self.names:
            self.names.append(name)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id)

    def visit_arg(self, node):
        self.add(node.arg)

    def visit_FunctionDef(self, node):
        self.add(node.name)
        for part in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if part is not None:
                self.visit(part)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.add(node.name)
        for part in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(part)

    def visit_Lambda(self, node):
        for part in [*node.args.defaults, *node.args.kw_defaults]:
            if part is not None:
                self.visit(part)

    def visit_ListComp(self, node):
        # Assignment expressions bind in the containing function.
        for part in ast.walk(node):
            if isinstance(part, ast.NamedExpr):
                self.visit(part.target)

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_ExceptHandler(self, node):
        self.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for item in node.names:
            self.add(item.asname or item.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for item in node.names:
            self.add(item.asname or item.name)

    def visit_Global(self, node):
        self.globals.update(node.names)

    def visit_Nonlocal(self, node):
        self.nonlocals.update(node.names)

    def visit_MatchAs(self, node):
        self.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        self.add(node.name)

    def visit_MatchMapping(self, node):
        self.add(node.rest)
        self.generic_visit(node)


class _Normalise(ast.NodeTransformer):
    def __init__(self):
        self.scopes = [("module", {})]

    def _rename(self, name):
        for index in range(len(self.scopes) - 1, -1, -1):
            kind, scope = self.scopes[index]
            if kind == "class" and index != len(self.scopes) - 1:
                continue  # Class namespaces are not closure scopes.
            if name in scope:
                return scope[name]
        return name

    def visit_Name(self, node):
        node.id = self._rename(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._rename(node.arg)
        return node

    def visit_Global(self, node):
        return node

    def visit_Nonlocal(self, node):
        node.names = [self._rename(name) for name in node.names]
        return node

    def visit_ExceptHandler(self, node):
        node.name = self._rename(node.name)
        return self.generic_visit(node)

    def visit_alias(self, node):
        bound = node.asname or node.name.split(".")[0]
        renamed = self._rename(bound)
        if renamed != bound:
            node.asname = renamed
        return node

    def visit_MatchAs(self, node):
        node.name = self._rename(node.name)
        return self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        node.rest = self._rename(node.rest)
        return self.generic_visit(node)

    def _body(self, node):
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:]
        return node

    def _enter(self, kind, parts, args=None):
        bindings = _Bindings()
        if args:
            bindings.visit(args)
        for part in parts:
            bindings.visit(part)
        names = [n for n in bindings.names if n not in bindings.globals | bindings.nonlocals]
        scope = ({n: f"@local:{len(self.scopes)}:{i}" for i, n in enumerate(names)}
                 if kind != "class" else {n: n for n in names})
        scope.update({n: n for n in bindings.globals})
        self.scopes.append((kind, scope))

    def _arguments_outer(self, args):
        args.defaults = [self.visit(n) for n in args.defaults]
        args.kw_defaults = [self.visit(n) if n is not None else None for n in args.kw_defaults]
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg and arg.annotation:
                arg.annotation = self.visit(arg.annotation)

    def _function(self, node):
        self._arguments_outer(node.args)
        parts = [node.body] if isinstance(node, ast.Lambda) else node.body
        self._enter("function", parts, node.args)
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg]:
            if arg:
                self.visit_arg(arg)
        node.body = self.visit(node.body) if isinstance(node, ast.Lambda) else [self.visit(n) for n in node.body]
        self.scopes.pop()
        return node

    def visit_FunctionDef(self, node):
        node.name = self._rename(node.name)
        self._body(node)
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        if node.returns:
            node.returns = self.visit(node.returns)
        return self._function(node)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = _function

    def visit_ListComp(self, node):
        node.generators[0].iter = self.visit(node.generators[0].iter)
        self._enter("function", [g.target for g in node.generators])
        for index, gen in enumerate(node.generators):
            if index:
                gen.iter = self.visit(gen.iter)
            gen.target = self.visit(gen.target)
            gen.ifs = [self.visit(n) for n in gen.ifs]
        if isinstance(node, ast.DictComp):
            node.key, node.value = self.visit(node.key), self.visit(node.value)
        else:
            node.elt = self.visit(node.elt)
        self.scopes.pop()
        return node

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_ClassDef(self, node):
        node.name = self._rename(node.name)
        self._body(node)
        node.bases = [self.visit(n) for n in node.bases]
        node.keywords = [self.visit(n) for n in node.keywords]
        node.decorator_list = [self.visit(n) for n in node.decorator_list]
        self._enter("class", node.body)
        node.body = [self.visit(n) for n in node.body]
        self.scopes.pop()
        return node

    def visit_Module(self, node):
        return self.generic_visit(self._body(node))


def normalised_hash(node):
    node = copy.deepcopy(node)
    # Definition names do not make renamed copies novel; method/call names do.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        node.name = "definition"
    node = _Normalise().visit(node)
    return _sha(ast.dump(node, include_attributes=False).encode())


def _definitions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _hashed_definitions(tree):
    # Resolve closure references while their enclosing bindings are available.
    # Hash both isolated and contextual forms, so copied standalone helpers and
    # whole renamed modules receive the same credit as the library.
    transformed = _Normalise().visit(copy.deepcopy(tree))
    return [(original, normalised_hash(original), normalised_hash(normalised))
            for original, normalised in zip(_definitions(tree), _definitions(transformed), strict=True)]


def catalog(repo_root, commit=None):
    root = Path(repo_root)
    sources = {}
    if commit is not None:
        def git(*args):
            try:
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout
            except subprocess.CalledProcessError as exc:
                raise ValueError("cannot read catalog build commit") from exc
        commit = git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
        paths = git("ls-tree", "-r", "--name-only", "-z", commit, "--",
                    "components/eidos.search.recipe-portfolio", "components/eidos.search.recipe-sweep",
                    "study/exp/cards/baseline_project.py").decode().split("\0")
        for key in paths:
            if key.endswith(".py") or key.endswith("/eidos-component.json"):
                sources[key] = git("show", f"{commit}:{key}")
    else:
        for name in ("eidos.search.recipe-portfolio", "eidos.search.recipe-sweep"):
            component = root / "components" / name
            for path in sorted(component.rglob("*")):
                if path.is_file() and (path.suffix == ".py" or path.name == "eidos-component.json"):
                    sources[str(path.relative_to(root))] = path.read_bytes()
        baseline = root / "study/exp/cards/baseline_project.py"
        if baseline.is_file():
            sources["study/exp/cards/baseline_project.py"] = baseline.read_bytes()
    result = dict(schema="eidos-exp-catalog/1", components=[], operators=[], models=[],
                  sweep_families=[], sweep_templates=[], atoms=[], definition_hashes=[],
                  file_hashes={}, module_hashes={}, missing=[], parse_errors=[], inputs={}, commit=commit,
                  baseline_hashes=[])
    atoms, hashes = set(), set()
    for name in ("eidos.search.recipe-portfolio", "eidos.search.recipe-sweep"):
        prefix = "components/" + name + "/"
        files = {k: v for k, v in sources.items() if k.startswith(prefix)}
        if not files:
            result["missing"].append(name)
            continue
        if name.endswith("recipe-portfolio"):
            for required in ("eidos_recipes/registry.py", "templates/project.py"):
                if prefix + required not in files:
                    result["missing"].append(name + "/" + required)
        manifest = prefix + "eidos-component.json"
        if manifest in files:
            data = json.loads(files[manifest])
            result["components"].append({k: data.get(k) for k in ("id", "release")})
            result["inputs"][manifest] = _sha(files[manifest])
        else:
            result["missing"].append(name + "/eidos-component.json")
        for path, data in sorted(files.items()):
            if not path.endswith(".py"):
                continue
            key = path[len(prefix):]
            result["inputs"][path] = _sha(data)
            try:
                tree = ast.parse(data)
            except (SyntaxError, ValueError, UnicodeError, RecursionError):
                result["parse_errors"].append(path)
                continue
            try:
                found, _ = _atoms(tree)
                definition_hashes = [h for _, isolated, contextual in _hashed_definitions(tree) for h in (isolated, contextual)]
                module_hash = normalised_hash(tree)
            except RecursionError:
                result["parse_errors"].append(path)
                continue
            atoms.update(found)
            hashes.update(definition_hashes)
            result["file_hashes"].setdefault(key, []).append(_sha(data))
            result["module_hashes"].setdefault(key, []).append(module_hash)
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                for target, field in (("TRANSFORMS", "operators"), ("MODELS", "models"),
                                      ("TEMPLATE_FAMILIES", "sweep_families"), ("TEMPLATE_SETTINGS", "sweep_templates")):
                    if target not in names:
                        continue
                    values = node.value.keys if isinstance(node.value, ast.Dict) else getattr(node.value, "elts", [])
                    result[field].extend(n.value for n in values if isinstance(n, ast.Constant) and isinstance(n.value, str))
    baseline_key = "study/exp/cards/baseline_project.py"
    if baseline_key in sources:
        result["baseline_hashes"] = [_sha(sources[baseline_key])]
        try:
            result["baseline_module_hashes"] = [normalised_hash(ast.parse(sources[baseline_key]))]
        except (SyntaxError, ValueError, RecursionError):
            result["parse_errors"].append(baseline_key)
        result["inputs"][baseline_key] = _sha(sources[baseline_key])
    result["atoms"], result["definition_hashes"] = sorted(atoms), sorted(hashes)
    for field in ("operators", "models", "sweep_families", "sweep_templates"):
        result[field] = sorted(set(result[field]))
    result["sha256"] = _sha(_canonical(result))
    return result


def fingerprint(candidate_dir, catalog):
    root = Path(candidate_dir)
    result = dict(atoms=[], authored=[], atoms_outside_catalog=[], authored_outside_catalog=[],
                  defines_estimator=False, authored_fit_predict=False, parse_errors=[],
                  ambiguous=False, template_identical=False, baseline_identical=False, changed_templates=[],
                  provenance=None, inputs={}, catalog_sha256=catalog.get("sha256"),
                  catalog_incomplete=bool(catalog.get("missing") or catalog.get("parse_errors")))
    atoms, authored = set(), set()
    known = set(catalog.get("definition_hashes", []))
    known_modules = {digest for hashes in catalog.get("module_hashes", {}).values() for digest in hashes}
    identical = True
    baseline_files = []
    paths = sorted(root.rglob("*.py")) if root.is_dir() else []
    for path in paths:
        key = str(path.relative_to(root))
        if path.is_symlink():
            result["parse_errors"].append(key)
            continue
        data = path.read_bytes()
        result["inputs"][key] = _sha(data)
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError, UnicodeError, RecursionError):
            result["parse_errors"].append(key)
            continue
        try:
            found, ambiguous = _atoms(tree)
            module_hash = normalised_hash(tree)
            definitions = _hashed_definitions(tree)
        except RecursionError:
            result["parse_errors"].append(key)
            continue
        baseline_files.append(_sha(data) in catalog.get("baseline_hashes", [])
                              or module_hash in catalog.get("baseline_module_hashes", []))
        atoms.update(found)
        result["ambiguous"] |= ambiguous
        # The launcher nests sources under out/candidate/source/. Code identity
        # is independent of this packaging prefix (and of a renamed filename).
        match = module_hash in known_modules
        identical &= match
        if not match:
            result["changed_templates"].append(key)
        for node, digest, contextual in definitions:
            if digest in known or contextual in known:
                continue
            authored.add(digest)
            if isinstance(node, ast.ClassDef):
                methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                result["defines_estimator"] |= "fit" in methods and bool(methods & {"predict", "transform"})
            elif node.name in {"fit", "predict", "transform", "fit_transform", "predict_proba"}:
                result["authored_fit_predict"] = True
    provenance = root / "candidate_provenance.json"
    if not provenance.is_file():
        provenance = root.parent / "candidate_provenance.json"
    if provenance.is_file():
        if provenance.is_symlink():
            result["parse_errors"].append("candidate_provenance.json")
        else:
            data = provenance.read_bytes()
            result["provenance_sha256"] = _sha(data)
            try:
                stamp = json.loads(data)
                if not isinstance(stamp, dict):
                    raise ValueError("invalid provenance")
                if any(value is not None and not isinstance(value, str) for key, value in stamp.items()
                       if key in {"basis", "preparation", "job_id", "decision_id"}):
                    raise ValueError("invalid provenance fields")
                result["provenance"] = {k: stamp[k] for k in ("basis", "preparation", "job_id", "decision_id") if k in stamp}
            except (ValueError, UnicodeError):
                result["parse_errors"].append("candidate_provenance.json")
    result["template_identical"] = bool(paths) and identical
    result["baseline_identical"] = bool(baseline_files) and all(baseline_files)
    result["atoms"] = sorted(atoms)
    result["authored"] = result["authored_outside_catalog"] = sorted(authored)
    result["atoms_outside_catalog"] = sorted(atoms - set(catalog.get("atoms", [])))
    result["class"] = classify(result)
    return result


def classify(fingerprint):
    fp = fingerprint
    if fp.get("parse_errors") or fp.get("ambiguous") or fp.get("catalog_incomplete") or not fp.get("inputs"):
        return "unknown"
    if fp.get("baseline_identical"):
        return "baseline"
    outside = fp.get("atoms_outside_catalog", [])
    constructors = [a for a in outside if a.startswith(("sklearn.", "torch.nn.", "scipy.", "xgboost.", "lightgbm.", "catboost.")) and a.rsplit(".", 1)[-1][:1].isupper()]
    if constructors or fp.get("defines_estimator") or fp.get("authored_fit_predict"):
        return "external"
    if outside:
        return "unknown"
    basis = (fp.get("provenance") or {}).get("basis")
    preparation = (fp.get("provenance") or {}).get("preparation")
    if (fp.get("authored_outside_catalog") or fp.get("changed_templates") or basis not in {None, "one-se-choice"}
            or (basis is None and preparation in {"authored", "baseline-or-direct"})):
        return "recombination"
    return "catalog" if fp.get("template_identical") and basis == "one-se-choice" else "unknown"


def novelty_win(run_row, autopilot_row, labels, metric):
    """Return win evidence on supplied aligned predictions; no inference is run."""
    result = dict(novelty_win=False, delta=None, se=None, behavioural_distance=None)
    a = run_row.get("candidate_predictions", run_row.get("predictions"))
    b = autopilot_row.get("candidate_predictions", autopilot_row.get("predictions"))
    if a is None or b is None or labels is None:
        return result
    delta, se = paired_rows_delta(a, b, labels, metric)
    a, b, y = (np.asarray(v) for v in (a, b, labels))
    if metric == "accuracy":
        distance = float(np.mean(a != b))
    else:
        sd = float(np.std(y.astype(float)))
        distance = float(np.mean(np.abs(a - b)) / sd) if sd > 0 else None
    novelty_class = run_row.get("novelty", {}).get("class", run_row.get("class"))
    result.update(delta=delta, se=se, behavioural_distance=distance,
                  novelty_win=bool(run_row.get("verified_success", run_row.get("verified")) is True
                                  and novelty_class in {"recombination", "external"}
                                  and se is not None and delta > se))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("catalog")
    command.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    command.add_argument("--commit", required=True, help="build commit whose sources define the catalog")
    command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = catalog(args.repo_root, args.commit)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError, RecursionError, OverflowError) as exc:
        parser.exit(2, f"catalog input error: {type(exc).__name__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
