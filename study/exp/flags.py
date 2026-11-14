"""Validate the feature lifecycle and inspect reproducible study treatments."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

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
    validate_registry(registry)
    return registry


def validate_registry(registry: dict | None = None) -> None:
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
        if not isinstance(flag["requires"], list) or any(not isinstance(dep, str) or dep not in flags for dep in flag["requires"]):
            raise ValueError(f"{name}: unknown required flag")
        if not isinstance(flag["summary"], str) or not flag["summary"].strip():
            raise ValueError(f"{name}: summary must be nonempty")
        if flag["experiment"] is not None and not isinstance(flag["experiment"], str):
            raise ValueError(f"{name}: experiment must be a string or null")
        date.fromisoformat(flag["introduced"])
        removal = flag["removal"]
        if flag["state"] == "rejected":
            if not isinstance(removal, dict) or set(removal) != {"decided", "experiment", "reason"} or not all(isinstance(value, str) and value.strip() for value in removal.values()):
                raise ValueError(f"{name}: rejected flag requires a removal decision")
            date.fromisoformat(removal["decided"])
        elif removal is not None:
            raise ValueError(f"{name}: removal is only present for rejected flags")
        if not isinstance(flag["patch"], dict) or not isinstance(flag["code"], list):
            raise ValueError(f"{name}: patch must be an object and code a list")
        for path in flag["code"]:
            if not repo_path(path).exists():
                raise ValueError(f"{name}: missing code path {path}")
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


def removal_list(registry: dict | None = None) -> list[dict]:
    flags = (registry or load_registry())["flags"]
    treatments = [json.loads(path.read_text()) for path in sorted(TREATMENTS.glob("*.json"))]
    return [dict(id=name, code=flag["code"], removal=flag["removal"],
                 treatments=[t["id"] for t in treatments if name in t["flags"]])
            for name, flag in flags.items() if flag["state"] == "rejected"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--state", choices=sorted(STATES))
    sub.add_parser("check")
    sub.add_parser("removal-list")
    sub.add_parser("show").add_argument("treatment")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        from study.exp.treatment import resolve
        if args.command == "check":
            for path in sorted(TREATMENTS.glob("*.json")):
                resolve(path.stem)
            print("Registry and all treatments valid")
        elif args.command == "list":
            print(json.dumps({k: v for k, v in registry["flags"].items()
                              if not args.state or v["state"] == args.state}, indent=2))
        elif args.command == "removal-list":
            print(json.dumps(removal_list(registry), indent=2))
        else:
            treatment = resolve(args.treatment)
            print(json.dumps(dict(**treatment.canonical(), sha256=treatment.sha256), indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
