"""Validate the feature lifecycle and inspect reproducible study treatments."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = Path(__file__).with_name("flags.json")
TREATMENTS = Path(__file__).with_name("treatments")
STATES = {"experimental", "adopted", "rejected"}


def repo_path(value: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("registry path must be a string")
    path = ROOT / value
    if not path.resolve().is_relative_to(ROOT):
        raise ValueError(f"path escapes repository: {value!r}")
    return path


def load_registry() -> dict:
    registry = json.loads(REGISTRY.read_text())
    validate_registry(registry, check_paths=False)
    return registry


def validate_registry(registry: dict | None = None, *, check_paths: bool = True) -> None:
    registry = json.loads(REGISTRY.read_text()) if registry is None else registry
    if registry.get("schema") != "eidos-flags/1" or not isinstance(registry.get("flags"), dict):
        raise ValueError("expected eidos-flags/1 registry")
    flags = registry["flags"]
    required = {"state", "summary", "experiment", "introduced", "requires", "patch", "code", "removal"}
    for name, flag in flags.items():
        if not isinstance(flag, dict) or not required <= flag.keys():
            raise ValueError(f"{name}: missing required flag keys")
        if not isinstance(flag["state"], str) or flag["state"] not in STATES:
            raise ValueError(f"{name}: unknown lifecycle state")
        if not isinstance(flag["requires"], list) or any(
            not isinstance(dep, str) or dep not in flags for dep in flag["requires"]
        ):
            raise ValueError(f"{name}: unknown required flag")
        if not isinstance(flag["summary"], str) or not flag["summary"].strip():
            raise ValueError(f"{name}: summary must be nonempty")
        if flag["experiment"] is not None and not isinstance(flag["experiment"], str):
            raise ValueError(f"{name}: experiment must be a string or null")
        date.fromisoformat(flag["introduced"])
        removal = flag["removal"]
        if flag["state"] == "rejected":
            if (
                not isinstance(removal, dict)
                or set(removal) != {"decided", "experiment", "reason"}
                or not all(isinstance(value, str) and value.strip() for value in removal.values())
            ):
                raise ValueError(f"{name}: rejected flag requires a removal decision")
            date.fromisoformat(removal["decided"])
        elif removal is not None:
            raise ValueError(f"{name}: removal is only present for rejected flags")
        if not isinstance(flag["patch"], dict) or not isinstance(flag["code"], list):
            raise ValueError(f"{name}: patch must be an object and code a list")
        allowed_patch = {"investigator", "prompts", "components", "toolkit"}
        if set(flag["patch"]) - allowed_patch:
            raise ValueError(f"{name}: unknown patch keys")
        for section, allowed in (
            ("prompts", {"tools_arm4"}),
            ("components", {"install", "bind"}),
            ("toolkit", {"add", "replace"}),
        ):
            value = flag["patch"].get(section, {})
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError(f"{name}: unknown {section} patch keys")
        for dep in flag.get("recommends", []):
            if dep not in flags:
                raise ValueError(f"{name}: unknown recommended flag {dep}")
        for entry in flag["code"]:
            entry = code_entry(entry)
            if check_paths and not repo_path(entry["path"]).exists():
                raise ValueError(f"{name}: missing code path {entry['path']}")
            if not set(entry.get("shared_with", [])) <= flags.keys():
                raise ValueError(f"{name}: unknown shared flag")
        for path in flag["patch"].get("prompts", {}).values():
            if not repo_path(path).is_file():
                raise ValueError(f"{name}: missing prompt {path}")
        if flag["state"] == "adopted" and flag["patch"]:
            raise ValueError(f"{name}: adopted aliases must have an empty patch")

    def visit(name, ancestors):
        if name in ancestors:
            raise ValueError(f"{name}: cyclic flag requirements")
        for dep in flags[name]["requires"]:
            visit(dep, ancestors | {name})

    for name in flags:
        visit(name, set())


def code_entry(entry) -> dict:
    # Legacy string entries remain readable for historical registries.
    entry = {"path": entry, "kind": "delete"} if isinstance(entry, str) else entry
    if (
        not isinstance(entry, dict)
        or entry.get("kind") not in {"delete", "procedure"}
        or set(entry) - {"path", "kind", "text", "shared_with"}
    ):
        raise ValueError("invalid code entry")
    repo_path(entry["path"])
    if entry["kind"] == "procedure" and not entry.get("text"):
        raise ValueError("procedure needs text")
    return entry


def _core_imports(path: str, deleted: set[str], *, active_policies=()) -> list[str]:
    """Conservatively identify surviving unconditional imports of deleted modules.

    Policy-guarded imports are safe to retain with dormant inline branches. A
    function-local import without a surrounding conditional is still a blocker.
    """
    if not path.startswith("src/") or not path.endswith(".py"):
        return []
    module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    blockers = []
    for source in sorted((ROOT / "src").rglob("*.py")):
        if any(source.is_relative_to(repo_path(item)) for item in deleted):
            continue

        def policy_guard(test):
            if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
                return any(policy_guard(value) for value in test.values)
            if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                return "exploration-policy/3" not in active_policies and any(
                    isinstance(value, ast.Constant) and value.value == "exploration-policy/3"
                    for value in [test.left, *test.comparators]
                )
            return (
                "context-policy/4" not in active_policies
                and isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "get"
                and bool(test.args)
                and isinstance(test.args[0], ast.Constant)
                and test.args[0].value == "_v4"
            )

        def walk(node, guarded=False, source=source):
            if isinstance(node, ast.Import):
                matches = any(alias.name == module for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = source.relative_to(ROOT / "src").parent.parts
                    base = ".".join((*package[: len(package) - node.level + 1], *base.split("."))).rstrip(".")
                matches = base == module or any(f"{base}.{alias.name}" == module for alias in node.names)
            else:
                matches = False
            if matches and not guarded:
                blockers.append(f"core import:{source.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node, ast.If):
                walk(node.test, guarded)
                for child in node.body:
                    walk(child, guarded or policy_guard(node.test))
                for child in node.orelse:
                    walk(child, guarded)
            else:
                for child in ast.iter_child_nodes(node):
                    walk(child, guarded)

        walk(ast.parse(source.read_text()))
    return blockers


def removal_list(registry: dict | None = None, *, assume_rejected=()) -> list[dict]:
    from study.exp import treatment as resolver

    registry = copy.deepcopy(registry if registry is not None else load_registry())
    validate_registry(registry)
    flags = registry["flags"]
    unknown = set(assume_rejected) - flags.keys()
    if unknown:
        raise ValueError(f"unknown assumed flags: {sorted(unknown)}")
    rejected = {name for name, flag in flags.items() if flag["state"] == "rejected"} | set(assume_rejected)
    definitions = [json.loads(path.read_text()) for path in sorted(TREATMENTS.glob("*.json"))]
    # Treatments naming a removed flag are retired, never silently rewritten.
    retired_flags = set(rejected)
    while True:
        dependents = {name for name, flag in flags.items() if set(flag["requires"]) & retired_flags}
        expanded = retired_flags | dependents
        if expanded == retired_flags:
            break
        retired_flags = expanded
    remaining = [t for t in definitions if not set(t["flags"]) & retired_flags]
    if not any(t["id"] == resolver.DEFAULT for t in remaining):
        raise ValueError("removal must preserve the default treatment")

    def overlaps(left, right):
        return left.is_relative_to(right) or right.is_relative_to(left)

    def patch_paths(flag):
        patch = flag["patch"]
        paths = [repo_path(p) for p in patch.get("prompts", {}).values()]
        component_patch = patch.get("components", {})
        for name in component_patch.get("install", []):
            paths.append(ROOT / "components" / name)
            paths.extend((ROOT / "components").glob(f"{name}@*"))
        for name, release in component_patch.get("bind", {}).items():
            paths.append(resolver._directory(name, release))
        toolkit = patch.get("toolkit", {})
        paths.extend(ROOT / "components" / name for name in toolkit.get("add", []))
        paths.extend(ROOT / "components" / name for name in toolkit.get("replace", {}).values())
        return paths

    active_policies = set()
    with patch.object(resolver.registry_module, "load_registry", return_value=registry):
        for definition in remaining:
            investigator = resolver.resolve(definition["id"]).investigator
            active_policies.add(investigator.get("context_policy", "context-policy/3"))
            active_policies.add((investigator.get("exploration") or {}).get("policy", "exploration-policy/2"))
    records = []
    entries = {
        code_entry(e)["path"]
        for name in rejected
        for e in flags[name]["code"]
        if code_entry(e)["kind"] == "delete"
    }
    for name in sorted(rejected):
        flag = flags[name]
        rows = []
        for raw in flag["code"]:
            entry = copy.deepcopy(code_entry(raw))
            blockers = {
                f"flag:{other}"
                for other, other_flag in flags.items()
                if other not in rejected
                and (
                    other in entry.get("shared_with", [])
                    or any(code_entry(e)["path"] == entry["path"] for e in other_flag["code"])
                )
            }
            if entry["kind"] == "delete":
                for other, other_flag in flags.items():
                    if other not in rejected and any(
                        overlaps(p, repo_path(entry["path"])) for p in patch_paths(other_flag)
                    ):
                        blockers.add(f"flag:{other}")
                blockers.update(_core_imports(entry["path"], entries, active_policies=active_policies))
            # Verify every remaining treatment's actual plan before virtual deletion.
            for definition in remaining:
                with patch.object(resolver.registry_module, "load_registry", return_value=registry):
                    treatment = resolver.resolve(definition["id"])
                    needed = [p for card in ("N01", "N02", "D03") for p, _ in treatment.component_plan(card)]
                    needed += list(treatment.toolkit_plan().values()) + [treatment.tools_arm4_path]
                if entry["kind"] == "delete" and any(overlaps(p, repo_path(entry["path"])) for p in needed):
                    blockers.add(f"treatment:{definition['id']}")
            rows.append(
                {
                    **entry,
                    "removable": entry["kind"] == "delete" and not blockers,
                    "blocked_by": sorted(blockers),
                }
            )
        records.append(
            dict(
                id=name,
                removal=flag["removal"],
                paths=rows,
                blocked_by=[
                    f"flag:{other}"
                    for other in sorted(set(flags) - rejected)
                    if name in flags[other]["requires"]
                ],
                treatments=[t["id"] for t in definitions if name in t["flags"]],
            )
        )
    if not rejected:
        return records
    # An imported candidate may itself be blocked. Propagate that fact until no
    # surviving candidate imports a path still claimed to be removable.
    while True:
        deleted = {entry["path"] for row in records for entry in row["paths"] if entry["removable"]}
        changed = False
        for row in records:
            for entry in row["paths"]:
                if entry["removable"]:
                    blockers = _core_imports(entry["path"], deleted, active_policies=active_policies)
                    if blockers:
                        entry["blocked_by"].extend(blockers)
                        entry["removable"] = False
                        changed = True
        if not changed:
            break
    # Execute the proposed deletions/restorations only in a disposable public-source
    # tree, then resolve and build every surviving treatment there.
    with tempfile.TemporaryDirectory(prefix=".removal-", dir=ROOT) as scratch:
        virtual = Path(scratch)
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        for directory in (
            "components",
            "packages",
            "src",
            "study/exp",
            "study/eidos_arms",
            "study/prompts",
            "study/toolkit",
        ):
            shutil.copytree(ROOT / directory, virtual / directory, ignore=ignore)
        shutil.copyfile(ROOT / "LICENSE", virtual / "LICENSE")
        for row in records:
            for entry in row["paths"]:
                dest = virtual / entry["path"]
                if entry["removable"]:
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink(missing_ok=True)
                elif entry["kind"] == "procedure" and not entry["blocked_by"]:
                    if entry["path"] == "components/eidos.diagnose.baseline-errors/":
                        shutil.rmtree(dest)
                        shutil.copytree(virtual / "components/eidos.diagnose.baseline-errors@1.0.0", dest)
                    else:
                        entry["blocked_by"].append("procedure requires manual verification")
        virtual_registry = copy.deepcopy(registry)
        virtual_registry["flags"] = {k: v for k, v in flags.items() if k not in retired_flags}
        for value in virtual_registry["flags"].values():
            value["recommends"] = [k for k in value.get("recommends", []) if k not in retired_flags]
            for entry in value["code"]:
                if isinstance(entry, dict):
                    entry["shared_with"] = [k for k in entry.get("shared_with", []) if k not in retired_flags]
        with (
            patch.object(resolver, "ROOT", virtual),
            patch.object(resolver.registry_module, "ROOT", virtual),
            patch.object(resolver.registry_module, "load_registry", return_value=virtual_registry),
        ):
            for definition in remaining:
                try:
                    treatment = resolver.resolve(definition["id"])
                    for card in ("N01", "N02", "D03"):
                        treatment.component_plan(card)
                    treatment.toolkit_plan()
                    treatment.canonical()  # reads every bundle and toolkit manifest source
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    for row in records:
                        for entry in row["paths"]:
                            entry["removable"] = False
                            entry["blocked_by"].append(f"treatment:{definition['id']}: {exc}")
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--state", choices=sorted(STATES))
    sub.add_parser("check")
    sub.add_parser("removal-list").add_argument("--assume-rejected", default="")
    sub.add_parser("show").add_argument("treatment")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        from study.exp.treatment import resolve

        if args.command == "check":
            validate_registry(registry)
            for path in sorted(TREATMENTS.glob("*.json")):
                treatment = resolve(path.stem)
                for card in ("N01", "N02", "D03"):
                    treatment.component_plan(card)
                treatment.toolkit_plan()
                treatment.canonical()
                for name in treatment.flags:
                    for recommended in registry["flags"][name].get("recommends", []):
                        if recommended not in treatment.flags:
                            print(f"WARN {treatment.id}: {name} recommends {recommended}")
            print("Registry and all treatments valid")
        elif args.command == "list":
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in registry["flags"].items()
                        if not args.state or v["state"] == args.state
                    },
                    indent=2,
                )
            )
        elif args.command == "removal-list":
            plan = removal_list(registry, assume_rejected=[s for s in args.assume_rejected.split(",") if s])
            print(json.dumps(plan, indent=2))
            if any(row["blocked_by"] or any(entry["blocked_by"] for entry in row["paths"]) for row in plan):
                return 1
        else:
            treatment = resolve(args.treatment)
            print(json.dumps(dict(**treatment.canonical(), sha256=treatment.sha256), indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
