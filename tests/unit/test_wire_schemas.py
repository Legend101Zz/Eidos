"""Versioned wire JSON Schemas (schemas/eidos-wire/v<major>) and the golden fixtures (tests/fixtures/wire).

(a) the schema files are exactly what the generator produces from the server boundary models;
(b) valid fixtures validate against the schemas and invalid ones fail (jsonschema, server environment);
(d) the server-side validator refuses schema violations with a clear message;
(e) a newer-minor message with unknown fields is accepted by the schema, the server and eidos_wire;
plus: the boundary models stay in sync with the standard-library eidos_wire dataclasses.
The SDK-emits-the-fixtures check (c) is packages/eidos-worker-sdk/tests/test_wire_fixtures.py.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import types
import typing
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import eidos_wire
from eidos.boundary import wire
from eidos_wire import manifest as wire_manifest

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas" / "eidos-wire" / f"v{eidos_wire.WIRE_MAJOR}"
FIXTURES = ROOT / "tests" / "fixtures" / "wire" / f"v{eidos_wire.WIRE_MAJOR}"
# Fixtures that eidos_wire's own (semantic) checks accept: only the published schema catches them.
SCHEMA_ONLY = {"invalid-output-manifest-receipt-not-object", "invalid-component-manifest-card-type"}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kind(stem: str) -> str:
    name = stem.removeprefix("invalid-")
    return max((k for k in wire.MESSAGES if name.startswith(k)), key=len)


def _fixtures(valid: bool) -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json") if p.name.startswith("invalid-") != valid)


def _validator(kind: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / f"{kind}.schema.json").read_text()))


def _stdlib_parse(kind: str, data: dict):
    return {"invocation": eidos_wire.Invocation.from_dict, "output-manifest": eidos_wire.OutputManifest.from_dict,
            "component-manifest": eidos_wire.parse_manifest}[kind](data)


def test_a_schema_files_are_generated_and_current():
    gen = _load(ROOT / "scripts" / "dev" / "gen_wire_schemas.py")
    assert gen.main(["--check"]) == 0
    rendered = gen.render()
    assert sorted(rendered) == sorted(p.name for p in SCHEMAS.glob("*.schema.json"))
    assert rendered == gen.render()  # deterministic within a process
    for name, text in rendered.items():
        doc = json.loads(text)
        Draft202012Validator.check_schema(doc)
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert doc["x-eidos"]["generator"] == "scripts/dev/gen_wire_schemas.py"
        assert doc["x-eidos"]["wire_major"] == eidos_wire.WIRE_MAJOR == int(SCHEMAS.name[1:])
        assert doc["$id"].endswith(f":v{eidos_wire.WIRE_MAJOR}:{name.removesuffix('.schema.json')}")


def test_fixtures_are_current_in_the_server_environment():
    assert _load(ROOT / "scripts" / "dev" / "emit_wire_fixtures.py").main(["--check"]) == 0


def test_fixture_set_covers_every_message_valid_and_invalid():
    kinds_valid = {_kind(p.stem) for p in _fixtures(True)}
    kinds_invalid = {_kind(p.stem) for p in _fixtures(False)}
    assert kinds_valid == kinds_invalid == set(wire.MESSAGES)


@pytest.mark.parametrize("path", _fixtures(True), ids=lambda p: p.stem)
def test_b_valid_fixtures_pass_schema_server_and_stdlib(path):
    kind, data = _kind(path.stem), json.loads(path.read_text())
    errors = [e.message for e in _validator(kind).iter_errors(data)]
    assert not errors, errors
    wire.validate_wire(kind, data)
    _stdlib_parse(kind, data)


@pytest.mark.parametrize("path", _fixtures(False), ids=lambda p: p.stem)
def test_b_invalid_fixtures_fail_schema_and_server(path):
    kind, data = _kind(path.stem), json.loads(path.read_text())
    assert list(_validator(kind).iter_errors(data)), f"{path.name} unexpectedly satisfies the schema"
    with pytest.raises(wire.WireSchemaError) as exc:
        wire.validate_wire(kind, data)
    assert exc.value.problems and str(exc.value).startswith(f"{kind} violates the eidos-wire/1 schema: ")
    if path.stem in SCHEMA_ONLY:
        _stdlib_parse(kind, data)  # documents why the schema check is needed on receipt
    else:
        with pytest.raises(ValueError):
            _stdlib_parse(kind, data)


def test_d_schema_error_names_the_field():
    data = json.loads((FIXTURES / "output-manifest-succeeded.json").read_text())
    data["outputs"][1]["size"] = -1
    data["receipt"] = ["not", "an", "object"]
    with pytest.raises(wire.WireSchemaError) as exc:
        wire.validate_wire("output-manifest", data)
    text = str(exc.value)
    assert "outputs.1.size" in text and "receipt" in text
    with pytest.raises(wire.WireSchemaError, match="not strict JSON"):
        wire.validate_wire("output-manifest", {**data, "receipt": {"x": float("nan")}})


@pytest.mark.parametrize("kind", ["invocation", "output-manifest"])
def test_e_newer_minor_unknown_fields_are_accepted(kind):
    data = json.loads((FIXTURES / f"{kind}-newer-minor.json").read_text())
    base = json.loads((FIXTURES / ("invocation.json" if kind == "invocation" else "output-manifest-succeeded.json")).read_text())
    assert set(data) - set(base), "the newer-minor fixture must carry an unknown field"
    assert not list(_validator(kind).iter_errors(data))
    wire.validate_wire(kind, data)
    assert _stdlib_parse(kind, data) == _stdlib_parse(kind, base)


def _optional(tp) -> bool:
    return typing.get_origin(tp) in (typing.Union, types.UnionType) and type(None) in typing.get_args(tp)


@pytest.mark.parametrize("pair", [
    (eidos_wire.InputBinding, wire.InputBinding),
    (eidos_wire.Invocation, wire.Invocation),
    (eidos_wire.OutputEntry, wire.OutputEntry),
    (eidos_wire.OutputManifest, wire.OutputManifest),
    (eidos_wire.ComponentManifest, wire.ComponentManifest),
], ids=lambda p: p[0].__name__)
def test_boundary_models_mirror_the_stdlib_wire_types(pair):
    dc, model = pair
    fields = [f for f in dataclasses.fields(dc) if f.name != "raw"]  # raw: the parsed source, not a wire field
    hints = typing.get_type_hints(dc)
    by_alias = {(info.alias or name): info for name, info in model.model_fields.items()}
    assert [f.name for f in fields] == list(by_alias), "field names/order differ"
    for f in fields:
        info = by_alias[f.name]
        dc_required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        if f.name == "wire" and dc is not eidos_wire.ComponentManifest:
            # from_dict requires 'wire' on messages even though the dataclass constructor defaults it
            assert info.is_required()
            continue
        assert info.is_required() == dc_required, f.name
        if f.default is not dataclasses.MISSING:
            assert info.default == f.default, f.name
        assert _optional(hints[f.name]) == _optional(info.annotation), f"{f.name}: optional differs"


def test_component_manifest_enums_match_eidos_wire():
    def literal(alias) -> tuple:
        return typing.get_args(alias)

    assert literal(wire.Kind) == wire_manifest.KINDS
    assert literal(wire.Effect) == wire_manifest.EFFECTS
    assert literal(wire.Phase) == wire_manifest.PHASES
    assert literal(wire.Isolation) == wire_manifest.ISOLATION
    assert f"eidos-wire/{eidos_wire.WIRE_MAJOR}" == eidos_wire.WIRE_SCHEMA


def test_every_shipped_component_manifest_satisfies_the_schema():
    paths = [p for d in ("components", "examples", "tests", "packages") for p in (ROOT / d).rglob("eidos-component.json")]
    assert len(paths) >= 10
    for path in paths:
        data = json.loads(path.read_text())
        try:
            eidos_wire.parse_manifest(data)
        except eidos_wire.ManifestError:
            continue  # deliberately broken manifests used by negative tests
        wire.validate_wire("component-manifest", data)


def test_d_component_registration_refuses_schema_violations_with_problems():
    from eidos.application.handlers.components import parse_or_invalid
    from eidos.domain.errors import Invalid

    good = json.loads((FIXTURES / "component-manifest.json").read_text())
    assert parse_or_invalid(good).id == "fixture.wire-demo"
    with pytest.raises(Invalid) as exc:
        parse_or_invalid({**good, "card": 42})  # eidos_wire.parse_manifest alone accepts this
    assert any(p.startswith("card:") for p in exc.value.details["problems"])
