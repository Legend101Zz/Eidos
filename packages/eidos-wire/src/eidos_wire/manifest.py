"""Component manifest (``eidos-component/1``) parsing and validation.

A manifest declares what a component needs, what it can change, what it produces and how its
behaviour can be checked (design §"The component contract"). Declarations inform planning and
resolution; process and service boundaries enforce access.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

KINDS = ("artifact-op", "training-extension", "decision-policy", "provider", "planning", "assessment")
EFFECTS = (
    "observe",
    "generate-data",
    "change-weights",
    "change-learning",
    "change-structure",
    "plan",
    "assess",
)
PHASES = (
    "before-model-construction",
    "after-model-construction",
    "before-optimizer-construction",
    "after-backward",
    "after-gradient-unscale",
    "before-optimizer-step",
    "after-optimizer-step",
    "on-checkpoint-save",
    "on-checkpoint-load",
    "on-evaluation",
)
ISOLATION = ("immutable-artifacts", "private-copy", "in-trial", "sealed")
_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{1,127}$")
_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9_.\-]*/\d+$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([.\-+][0-9A-Za-z.\-]+)?$")


class ManifestError(ValueError):
    """The manifest violates the component contract. ``problems`` lists every violation."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ComponentManifest:
    id: str
    release: str
    implements: str
    kind: str
    entry: str
    input: Dict[str, str]
    output: Dict[str, str]
    scientific_effects: List[str]
    requires: List[str] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)
    reads: List[str] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    isolation: str = "immutable-artifacts"
    card: Optional[str] = None
    controls: List[str] = field(default_factory=list)
    receipt: List[str] = field(default_factory=list)
    phases: List[str] = field(default_factory=list)  # training extensions
    state: Dict[str, Any] = field(default_factory=dict)  # {schema, restore_classes, mapping}
    cost: Dict[str, Any] = field(default_factory=dict)  # estimated envelope
    grants: List[str] = field(default_factory=list)  # requested grants (not granted by declaring)
    metrics: Dict[str, Any] = field(default_factory=dict)  # metric identities and meaning
    conflicts: List[str] = field(default_factory=list)
    llm: Optional[Dict[str, Any]] = None  # specialist model role, prompt revision, limits
    python: str = ">=3.9"
    wire: str = "eidos-wire/1"
    dependencies: List[str] = field(default_factory=list)
    license: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.release}"

    def to_dict(self) -> dict:
        return dict(self.raw)


def parse_manifest(data: Dict[str, Any]) -> ComponentManifest:
    problems: List[str] = []
    if not isinstance(data, dict):
        raise ManifestError(["manifest must be a table/object"])

    def need(key: str, typ, desc: str):
        if key not in data:
            problems.append(f"missing required field '{key}' ({desc})")
            return None
        if not isinstance(data[key], typ):
            problems.append(f"field '{key}' must be {desc}")
            return None
        return data[key]

    cid = need("id", str, "string")
    if cid is not None and not _ID.match(cid):
        problems.append(f"invalid id {cid!r}")
    rel = need("release", str, "semantic version string")
    if rel is not None and not _SEMVER.match(rel):
        problems.append(f"release {rel!r} is not a semantic version")
    impl = need("implements", str, "capability/major string")
    if impl is not None and not _CAPABILITY.match(impl):
        problems.append(f"implements {impl!r} must look like 'name/MAJOR'")
    kind = need("kind", str, "string")
    if kind is not None and kind not in KINDS:
        problems.append(f"kind {kind!r} not one of {KINDS}")
    entry = need("entry", str, "'module:function' string")
    if entry is not None and (":" not in entry or entry.startswith(":") or entry.endswith(":")):
        problems.append(f"entry {entry!r} must be 'module:function'")
    for key in ("input", "output"):
        val = data.get(key, {})
        if not isinstance(val, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _CAPABILITY.match(v) for k, v in val.items()
        ):
            problems.append(f"'{key}' must map port names to 'schema/MAJOR' identifiers")
    effects = need("scientific_effects", list, "list of effects")
    if effects is not None:
        if not effects:
            problems.append("scientific_effects must declare at least one effect")
        for e in effects:
            if e not in EFFECTS:
                problems.append(f"unknown scientific effect {e!r}")
    iso = data.get("isolation", "immutable-artifacts")
    if iso not in ISOLATION:
        problems.append(f"isolation {iso!r} not one of {ISOLATION}")
    phases = data.get("phases", [])
    for p in phases:
        if p not in PHASES:
            problems.append(f"unknown training phase {p!r}")
    if kind == "training-extension" and not phases:
        problems.append("training-extension must declare at least one phase")
    if kind != "training-extension" and phases:
        problems.append("only training-extension components declare phases")
    if effects and "change-learning" in effects and kind == "training-extension":
        st = data.get("state") or {}
        if st and "schema" not in st:
            problems.append("stateful learning rule must declare state.schema")
    for key in ("requires", "optional", "reads", "writes", "controls", "receipt", "grants", "conflicts", "dependencies"):
        val = data.get(key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            problems.append(f"'{key}' must be a list of strings")
    for cap in list(data.get("requires", [])) + list(data.get("optional", [])):
        if isinstance(cap, str) and not _CAPABILITY.match(cap):
            problems.append(f"capability {cap!r} must look like 'name/MAJOR'")
    if kind == "assessment" and effects and "assess" not in effects:
        problems.append("assessment components must declare the 'assess' effect")
    if kind == "planning" and effects and "plan" not in effects:
        problems.append("planning components must declare the 'plan' effect")
    llm = data.get("llm")
    if llm is not None:
        if not isinstance(llm, dict):
            problems.append("'llm' must be a table")
        else:
            for k in ("role", "prompt_revision", "max_calls", "max_tokens"):
                if k not in llm:
                    problems.append(f"llm.{k} is required for specialist model use")
    if problems:
        raise ManifestError(problems)
    known = set(ComponentManifest.__dataclass_fields__) - {"raw"}
    kwargs = {k: v for k, v in data.items() if k in known}
    return ComponentManifest(raw=dict(data), **kwargs)


def load_manifest(path: str | Path) -> ComponentManifest:
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised on 3.9/3.10
            raise ManifestError([f"{path.name}: TOML manifests need Python 3.11+; ship eidos-component.json"]) from exc
        data = tomllib.loads(text)
    return parse_manifest(data)
