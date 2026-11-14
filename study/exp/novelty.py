"""Mechanical novelty, without importing or executing inspected code.

Decision table (ordered): parse failure/incomplete catalog/ambiguous resolution
=> unknown; outside estimator/transform constructor, authored estimator class or
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
from pathlib import Path

import numpy as np

from study.exp.stats import paired_rows_delta

PREFIXES = ("sklearn.", "numpy.", "scipy.", "torch.")


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
                if item.name == "*" and node.module.split(".")[0] in {"sklearn", "numpy", "scipy", "torch"}:
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


class _Normalise(ast.NodeTransformer):
    def __init__(self):
        self.scopes = [{}]

    def _rename(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return name

    def visit_Name(self, node):
        node.id = self._rename(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._rename(node.arg)
        return self.generic_visit(node)

    def _body(self, node):
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:]
        return node

    def visit_FunctionDef(self, node):
        self._body(node)
        names = [a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
        names += [a.arg for a in (node.args.vararg, node.args.kwarg) if a]
        # Collect this scope's bindings without swallowing nested scopes.
        def bindings(part):
            if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store) and part.id not in names:
                names.append(part.id)
            for child in ast.iter_child_nodes(part):
                bindings(child)
        for part in node.body:
            bindings(part)
        called = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.scopes.append({name: f"local_{i}" for i, name in enumerate(names) if name not in called})
        node = self.generic_visit(node)
        self.scopes.pop()
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        return self.generic_visit(self._body(node))

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


def catalog(repo_root):
    root = Path(repo_root)
    result = dict(schema="eidos-exp-catalog/1", components=[], operators=[], models=[],
                  sweep_families=[], sweep_templates=[], atoms=[], definition_hashes=[],
                  file_hashes={}, module_hashes={}, missing=[], parse_errors=[], inputs={})
    atoms, hashes = set(), set()
    for name in ("eidos.search.recipe-portfolio", "eidos.search.recipe-sweep"):
        component = root / "components" / name
        if not component.is_dir():
            result["missing"].append(name)
            continue
        if name.endswith("recipe-portfolio"):
            for required in ("eidos_recipes/registry.py", "templates/project.py"):
                if not (component / required).is_file():
                    result["missing"].append(name + "/" + required)
        manifest = component / "eidos-component.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text())
            result["components"].append({k: data.get(k) for k in ("id", "release")})
            result["inputs"][str(manifest.relative_to(root))] = _sha(manifest.read_bytes())
        else:
            result["missing"].append(name + "/eidos-component.json")
        for path in sorted(component.rglob("*.py")):
            key = str(path.relative_to(component))
            data = path.read_bytes()
            result["inputs"][str(path.relative_to(root))] = _sha(data)
            try:
                tree = ast.parse(data)
            except (SyntaxError, ValueError, UnicodeError):
                result["parse_errors"].append(str(path.relative_to(root)))
                continue
            found, _ = _atoms(tree)
            atoms.update(found)
            hashes.update(normalised_hash(n) for n in _definitions(tree))
            result["file_hashes"].setdefault(key, []).append(_sha(data))
            result["module_hashes"].setdefault(key, []).append(normalised_hash(tree))
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
    result["atoms"], result["definition_hashes"] = sorted(atoms), sorted(hashes)
    for field in ("operators", "models", "sweep_families", "sweep_templates"):
        result[field] = sorted(set(result[field]))
    result["sha256"] = _sha(_canonical(result))
    return result


def fingerprint(candidate_dir, catalog):
    root = Path(candidate_dir)
    result = dict(atoms=[], authored=[], atoms_outside_catalog=[], authored_outside_catalog=[],
                  defines_estimator=False, authored_fit_predict=False, parse_errors=[],
                  ambiguous=False, template_identical=False, changed_templates=[],
                  provenance=None, inputs={}, catalog_sha256=catalog.get("sha256"),
                  catalog_incomplete=bool(catalog.get("missing") or catalog.get("parse_errors")))
    atoms, authored = set(), set()
    known = set(catalog.get("definition_hashes", []))
    known_modules = {digest for hashes in catalog.get("module_hashes", {}).values() for digest in hashes}
    identical = True
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
        except (SyntaxError, ValueError, UnicodeError):
            result["parse_errors"].append(key)
            continue
        found, ambiguous = _atoms(tree)
        atoms.update(found)
        result["ambiguous"] |= ambiguous
        module_hash = normalised_hash(tree)
        # The launcher nests sources under out/candidate/source/. Code identity
        # is independent of this packaging prefix (and of a renamed filename).
        match = module_hash in known_modules
        identical &= match
        if not match:
            result["changed_templates"].append(key)
        for node in _definitions(tree):
            digest = normalised_hash(node)
            if digest in known:
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
    result["atoms"] = sorted(atoms)
    result["authored"] = result["authored_outside_catalog"] = sorted(authored)
    result["atoms_outside_catalog"] = sorted(atoms - set(catalog.get("atoms", [])))
    result["class"] = classify(result)
    return result


def classify(fingerprint):
    fp = fingerprint
    if fp.get("parse_errors") or fp.get("ambiguous") or fp.get("catalog_incomplete") or not fp.get("inputs"):
        return "unknown"
    outside = fp.get("atoms_outside_catalog", [])
    constructors = [a for a in outside if a.startswith(("sklearn.", "torch.nn.", "scipy.")) and a.rsplit(".", 1)[-1][:1].isupper()]
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
    command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    data = catalog(args.repo_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
