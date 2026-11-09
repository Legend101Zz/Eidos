"""Worker invocation and output-manifest messages.

A runner (trusted launcher) writes an ``Invocation`` JSON file, starts the worker process in the
research project's own environment, and afterwards reads the ``OutputManifest`` the worker staged.
The runner verifies every staged file's size and digest before submitting the manifest to the
command service with the attempt's fencing epoch. Workers never hold ledger credentials.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

WIRE_MAJOR = 1
WIRE_MINOR = 0
WIRE_SCHEMA = f"eidos-wire/{WIRE_MAJOR}"


class WireError(ValueError):
    """A message does not satisfy the wire contract."""


def _check_schema(data: dict, kind: str) -> None:
    schema = data.get("wire")
    if not isinstance(schema, str) or not schema.startswith("eidos-wire/"):
        raise WireError(f"{kind}: missing or invalid 'wire' field")
    try:
        major = int(schema.split("/", 1)[1].split(".")[0])
    except ValueError as exc:
        raise WireError(f"{kind}: invalid wire version {schema!r}") from exc
    if major != WIRE_MAJOR:
        raise WireError(f"{kind}: unsupported wire major {major}; this package speaks {WIRE_MAJOR}")


def _build(cls, data: dict, kind: str):
    if not isinstance(data, dict):
        raise WireError(f"{kind}: expected an object")
    names = {f.name for f in dataclasses.fields(cls)}
    required = {
        f.name
        for f in dataclasses.fields(cls)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    missing = required - set(data)
    if missing:
        raise WireError(f"{kind}: missing fields {sorted(missing)}")
    # Unknown fields from a newer minor version are tolerated but not interpreted.
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class InputBinding:
    """One immutable input artifact made available read-only to the worker."""

    artifact_id: str
    digest: str
    path: str
    media_type: str = "application/octet-stream"
    schema: Optional[str] = None
    size: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> InputBinding:
        return _build(cls, data, "InputBinding")


@dataclass(frozen=True)
class Invocation:
    """Everything a worker may know about one attempt. Written by the trusted launcher."""

    job_id: str
    attempt_id: str
    epoch: int
    project_id: str
    component: Dict[str, Any]  # id, release, digest, entry, kind, implements
    inputs: Dict[str, InputBinding]
    config: Dict[str, Any]
    staging_dir: str
    scratch_dir: str
    limits: Dict[str, Any] = field(default_factory=dict)
    ports: Dict[str, Any] = field(default_factory=dict)  # resolved port bindings (descriptive)
    seed: Optional[int] = None
    wire: str = WIRE_SCHEMA

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Invocation:
        _check_schema(data, "Invocation")
        inv = _build(cls, data, "Invocation")
        inputs = {k: InputBinding.from_dict(v) for k, v in (data.get("inputs") or {}).items()}
        if not isinstance(inv.epoch, int) or inv.epoch < 1:
            raise WireError("Invocation: epoch must be a positive integer")
        for key in ("id", "entry"):
            if key not in inv.component:
                raise WireError(f"Invocation: component.{key} is required")
        return dataclasses.replace(inv, inputs=inputs)

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def read(cls, path: str | Path) -> Invocation:
        return cls.from_dict(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class OutputEntry:
    """One staged output file. ``filename`` is relative to the staging directory."""

    slot: str
    filename: str
    digest: str
    size: int
    media_type: str = "application/octet-stream"
    schema: Optional[str] = None
    role: str = "output"  # output | checkpoint | model-export | log | context

    @classmethod
    def from_dict(cls, data: dict) -> OutputEntry:
        entry = _build(cls, data, "OutputEntry")
        name = entry.filename
        if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
            raise WireError(f"OutputEntry: unsafe filename {name!r}")
        if not isinstance(entry.size, int) or entry.size < 0:
            raise WireError("OutputEntry: size must be a non-negative integer")
        return entry


@dataclass(frozen=True)
class OutputManifest:
    """The worker's claim about what it produced. Verified by the launcher, fenced by the server."""

    job_id: str
    attempt_id: str
    epoch: int
    status: str  # succeeded | failed
    outputs: List[OutputEntry] = field(default_factory=list)
    receipt: Dict[str, Any] = field(default_factory=dict)  # realized conditions, counts, usage
    error: Optional[Dict[str, Any]] = None  # {"category": ..., "message": ...}
    wire: str = WIRE_SCHEMA

    FILENAME = "eidos-output-manifest.json"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> OutputManifest:
        _check_schema(data, "OutputManifest")
        man = _build(cls, data, "OutputManifest")
        if man.status not in ("succeeded", "failed"):
            raise WireError(f"OutputManifest: invalid status {man.status!r}")
        outputs = [OutputEntry.from_dict(o) for o in (data.get("outputs") or [])]
        slots = [o.slot for o in outputs]
        if len(slots) != len(set(slots)):
            raise WireError("OutputManifest: duplicate output slot")
        return dataclasses.replace(man, outputs=outputs)

    def write(self, staging_dir: str | Path) -> Path:
        path = Path(staging_dir) / self.FILENAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)
        return path

    @classmethod
    def read(cls, staging_dir: str | Path) -> OutputManifest:
        return cls.from_dict(json.loads((Path(staging_dir) / cls.FILENAME).read_text()))


def json_schema(cls) -> dict:
    """Generate a JSON Schema (draft 2020-12 subset) for a wire dataclass, standard library only.

    Descriptive helper. The published schemas (``schemas/eidos-wire/v<major>/`` in the repository) are
    generated from the server boundary models, which also carry patterns, enums and bounds.
    """
    import typing

    hints = typing.get_type_hints(cls)

    def conv(tp) -> dict:
        origin = typing.get_origin(tp)
        args = typing.get_args(tp)
        if tp is str:
            return {"type": "string"}
        if tp is int:
            return {"type": "integer"}
        if tp is bool:
            return {"type": "boolean"}
        if tp is float:
            return {"type": "number"}
        if tp is Any:
            return {}
        if origin is typing.Union:
            non_none = [a for a in args if a is not type(None)]
            inner = conv(non_none[0]) if len(non_none) == 1 else {"anyOf": [conv(a) for a in non_none]}
            return {"anyOf": [inner, {"type": "null"}]}
        if origin in (dict, Dict):
            return {"type": "object", "additionalProperties": conv(args[1]) if args else {}}
        if origin in (list, List):
            return {"type": "array", "items": conv(args[0]) if args else {}}
        if isinstance(tp, type) and dataclasses.is_dataclass(tp):
            return {"$ref": f"#/$defs/{tp.__name__}"}
        return {}

    defs: dict = {}

    def build(c) -> dict:
        h = typing.get_type_hints(c)
        props, req = {}, []
        for f in dataclasses.fields(c):
            props[f.name] = conv(h[f.name])
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                req.append(f.name)
            for a in [h[f.name], *typing.get_args(h[f.name]), *[x for t in typing.get_args(h[f.name]) for x in typing.get_args(t)]]:
                if isinstance(a, type) and dataclasses.is_dataclass(a) and a.__name__ not in defs:
                    defs[a.__name__] = {}
                    defs[a.__name__] = build(a)
        return {"type": "object", "properties": props, "required": req}

    del hints
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": cls.__name__, **build(cls)}
    if defs:
        schema["$defs"] = defs
    schema["x-eidos-wire"] = WIRE_SCHEMA
    return schema
