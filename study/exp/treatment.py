"""Resolve prospective study treatments without importing lab or research packages."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from study.exp import flags as registry_module
from study.exp.artifacts import bundle_sha256, toolkit_manifest_sha256

ROOT = registry_module.ROOT
DEFAULT = "pilot4-v1"
BASE_COMPONENTS = (
    "eidos.task.score-classification",
    "eidos.search.recipe-sweep",
    "eidos.diagnose.baseline-errors",
)


def merge_patch(target, patch):
    """RFC 7396; never mutate either input."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def _directory(component: str, release: str) -> Path:
    current = ROOT / "components" / component
    if release == "current" or (
        (current / "eidos-component.json").is_file()
        and json.loads((current / "eidos-component.json").read_text())["release"] == release
    ):
        return current
    pinned = ROOT / "components" / f"{component}@{release}"
    if not pinned.is_dir():
        raise ValueError(f"missing component {component}@{release}")
    return pinned


@dataclass(frozen=True)
class ComponentInstall:
    directory: Path
    bind: bool
    upload_name: str

    def __iter__(self):
        # Preserve the directory/bind unpacking used by existing plan goldens.
        yield self.directory
        yield self.bind


@dataclass(frozen=True)
class Treatment:
    id: str
    flags: tuple[str, ...]
    investigator: dict
    tools_arm4_path: Path
    patch: dict
    definition: dict
    selected_flags: dict

    def component_plan(self, card: str) -> list[ComponentInstall]:
        from study.private_card_registry import is_classification

        install = set(BASE_COMPONENTS) | set(self.patch.get("components", {}).get("install", []))
        if card in ("D03", "D06", "N01", "N04") or is_classification(card):
            install.add("eidos.task.paired-classification-report")
        bindings = {"eidos.diagnose.baseline-errors": "1.0.0"}
        bindings.update(self.patch.get("components", {}).get("bind", {}))
        # Main's sorted current sweep directory precedes its pinned @1.0.0 directory.
        plan = [(ROOT / "study/eidos_arms/components/eidos.project.command@1.0.9", True)]
        for name in sorted(install):
            directory = _directory(name, bindings.get(name, "current"))
            plan.append((directory, True))
            for companion in sorted((ROOT / "components").glob(f"{name}@*")):
                if (
                    companion.is_dir()
                    and json.loads((companion / "eidos-component.json").read_text())["release"]
                    != json.loads((directory / "eidos-component.json").read_text())["release"]
                ):
                    plan.append((companion, False))
        return [ComponentInstall(path, bind, self.upload_name(path)) for path, bind in plan]

    def toolkit_plan(self) -> dict[str, Path]:
        names = json.loads((ROOT / "study/exp/toolkit-base.json").read_text())["directories"]
        plan = {name: ROOT / "components" / name for name in names}
        plan["eidos.diagnose.baseline-errors"] = _directory("eidos.diagnose.baseline-errors", "1.0.0")
        patch = self.patch.get("toolkit", {})
        for name in patch.get("add", []):
            plan[name] = ROOT / "components" / name
        for name, source in patch.get("replace", {}).items():
            plan[name] = ROOT / "components" / source
        if any(not (path / "eidos-component.json").is_file() for path in plan.values()):
            raise ValueError("toolkit source lacks component manifest")
        return dict(sorted(plan.items()))

    def upload_name(self, directory: Path) -> str:
        # Main uploaded the bound 1.0.0 bytes from the unpinned directory.
        if (
            directory.name == "eidos.diagnose.baseline-errors@1.0.0"
            and self.patch.get("components", {})
            .get("bind", {})
            .get("eidos.diagnose.baseline-errors", "1.0.0")
            == "1.0.0"
        ):
            return "eidos.diagnose.baseline-errors"
        return directory.name

    def canonical(self) -> dict:
        return dict(
            schema="eidos-resolved-treatment/1",
            id=self.id,
            flags=list(self.flags),
            investigator=self.investigator,
            tools_arm4=str(self.tools_arm4_path.relative_to(ROOT)),
            tools_arm4_sha256=hashlib.sha256(self.tools_arm4_path.read_bytes()).hexdigest(),
            components=self.patch.get("components", {}),
            component_plans={
                card: [
                    dict(
                        path=str(path.relative_to(ROOT)),
                        bind=bind,
                        upload_name=self.upload_name(path),
                        bundle_sha256=bundle_sha256(path),
                    )
                    for path, bind in self.component_plan(card)
                ]
                for card in ("N01", "N02", "D03")
            },
            toolkit_manifest_sha256=toolkit_manifest_sha256(self),
            toolkit={k: str(v.relative_to(ROOT)) for k, v in self.toolkit_plan().items()},
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def record(self) -> dict:
        return dict(id=self.id, flags=list(self.flags), sha256=self.sha256)


def resolve(treatment_id: str | None = None, *, allow_rejected: bool = False) -> Treatment:
    treatment_id = (
        treatment_id if treatment_id is not None else os.environ.get("EIDOS_STUDY_TREATMENT", DEFAULT)
    )
    if not treatment_id or Path(treatment_id).name != treatment_id:
        raise ValueError(f"unknown treatment: {treatment_id!r}")
    path = registry_module.TREATMENTS / f"{treatment_id}.json"
    if not path.is_file():
        raise ValueError(f"unknown treatment: {treatment_id}")
    definition = json.loads(path.read_text())
    if (
        set(definition) != {"schema", "id", "summary", "flags", "overrides"}
        or definition.get("schema") != "eidos-treatment/1"
        or definition.get("id") != treatment_id
        or not isinstance(definition.get("summary"), str)
        or not isinstance(definition.get("flags"), list)
        or not isinstance(definition.get("overrides"), dict)
    ):
        raise ValueError(f"invalid treatment: {treatment_id}")
    flags = registry_module.load_registry()["flags"]
    selected = definition["flags"]
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate treatment flags")
    for name in selected:
        if name not in flags:
            raise ValueError(f"unknown flag: {name}")
        if flags[name]["state"] == "rejected" and not allow_rejected:
            raise ValueError(f"rejected flag marked for removal: {name}")
        if not set(flags[name]["requires"]) <= set(selected):
            raise ValueError(f"{name}: treatment lacks required flags")
    if treatment_id == DEFAULT and (selected or definition["overrides"]):
        raise ValueError("default treatment cannot contain flags or overrides")
    patch = {}
    applied = set()
    ordered = []

    def apply(name):
        nonlocal patch
        if name in applied:
            return
        for dep in flags[name]["requires"]:
            apply(dep)
        previous = patch
        patch = merge_patch(patch, flags[name]["patch"])
        # Install/add describe additive operations across independent flags.
        for section, key in (("components", "install"), ("toolkit", "add")):
            added = flags[name]["patch"].get(section, {}).get(key)
            if added is not None:
                patch[section][key] = list(dict.fromkeys(previous.get(section, {}).get(key, []) + added))
        applied.add(name)
        ordered.append(name)

    for name in selected:
        apply(name)
    base = json.loads((ROOT / "study/eidos_arms/investigator.json").read_text())
    investigator = base
    for name in ordered:
        investigator = merge_patch(investigator, flags[name]["patch"].get("investigator", {}))
    investigator = merge_patch(investigator, definition["overrides"])
    if not isinstance(investigator, dict):
        raise ValueError("resolved investigator must be an object")
    prompt = registry_module.repo_path(
        patch.get("prompts", {}).get("tools_arm4", "study/prompts/tools_arm4.md")
    )
    if not prompt.is_file():
        raise ValueError(f"missing prompt {prompt}")
    treatment = Treatment(
        treatment_id,
        tuple(selected),
        investigator,
        prompt,
        patch,
        definition,
        {name: flags[name] for name in selected},
    )
    treatment.toolkit_plan()
    return treatment


def for_run(meta: dict) -> Treatment:
    """Refuse workers whose selected treatment differs from the prepared run."""
    if os.environ.get("EIDOS_STUDY_LIVE_DISPATCH") == "1" and meta.get("treatment") is None:
        raise ValueError("live dispatch requires a prepared treatment record")
    treatment = resolve()
    if meta.get("treatment") is not None and meta["treatment"] != treatment.record():
        raise ValueError("prepared study treatment differs from worker treatment")
    return treatment
