# 0008: Versioned wire JSON Schemas generated from server boundary models

Status: accepted (2026-11-09, release 1.0.1).

**Context.**
- The design's worker compatibility promise (§ "The worker compatibility promise") asks for four things: server boundary models generate versioned JSON Schema; worker fixtures are checked against those schemas in a separate test environment; the server validates received messages; and the schema major and supported Python range are explicit.
- v1.0.0 had the wire version tag (`eidos-wire/<major>` in `packages/eidos-wire/src/eidos_wire/messages.py`), the stdlib dataclasses with their `from_dict` checks, and the worker-compat environment matrix. It had no schema files, no fixtures, and no structural validation on receipt. `from_dict` checks presence and a few rules, not field types. For example, a manifest whose `receipt` is a list passed `from_dict` and could fail later with an untyped error.
- The worker packages (`eidos-wire`, `eidos-worker-sdk`, `eidos-pytorch`) must stay standard-library only and run on Python 3.9 with Pydantic v1 projects.

**Decision.**
1. **Source of truth.** Pydantic v2 boundary models in `src/eidos/boundary/wire.py` (server side only) mirror the stdlib wire types. They cover `Invocation` (with `InputBinding`), `OutputManifest` (with `OutputEntry`) and `ComponentManifest`. The models are strict: no type coercion. Unknown fields are accepted and ignored. A test (`tests/unit/test_wire_schemas.py::test_boundary_models_mirror_the_stdlib_wire_types`) keeps field names, order, required-ness, defaults and optionality in sync with the dataclasses. A second test keeps the manifest enums in sync with `eidos_wire.manifest`.
2. **Published schemas.** `scripts/dev/gen_wire_schemas.py` writes `schemas/eidos-wire/v1/{invocation,output-manifest,component-manifest}.schema.json` (JSON Schema 2020-12). Each file carries an `x-eidos` header (`generator`, `source` model, `wire`, `wire_major`) and a `urn:eidos:schema:eidos-wire:v1:<message>` `$id`. Output is deterministic: sorted keys, fixed indent, no timestamps or tool versions. CI fails if regeneration changes anything (`--check`, then `git diff --exit-code`).
3. **Golden fixtures from the real SDK.** `scripts/dev/emit_wire_fixtures.py` (Python 3.9+, stdlib, eidos-wire and eidos-worker-sdk only) produces `tests/fixtures/wire/v1/*.json`:
   - valid messages from `make_invocation`, `eidos_worker.run.execute` (one succeeded and one failed run) and `load_manifest`;
   - `*-newer-minor` variants carrying unknown fields;
   - `invalid-*` variants, one violation each.

   Volatile values are normalised, and each is listed in the script docstring: temporary paths, `wall_seconds`, the timestamped run log's digest and size, and traceback text.
4. **Where each check runs.**
   - Server environment (`tests/unit/test_wire_schemas.py`, `jsonschema` as a dev dependency only): schemas are current; valid fixtures pass and invalid ones fail, both under `jsonschema` and under the server validator; newer-minor fixtures are accepted.
   - Worker environment (`packages/eidos-worker-sdk/tests/test_wire_fixtures.py`, run by the worker-compat matrix on Python 3.9 with `pydantic<2`, and by the macOS SDK step): the SDK emits messages byte-identical to the golden fixtures, and the stdlib types accept newer-minor messages. This environment never needs `jsonschema` or Pydantic v2.
5. **Validation on receipt.** `eidos.boundary.wire.validate_wire` runs before the existing semantic checks at every server point that receives a worker message:
   - the command service's `publish_outputs` (refused as `INVALID`, with field-level `problems`);
   - the runner's and the assessor activity's manifest read (the attempt fails with the schema message);
   - component registration (`parse_or_invalid`, after `parse_manifest`'s richer contract messages).

   Semantic rules that JSON Schema cannot express stay in `eidos_wire`: duplicate output slots, kind and phase combinations, and stateful learning-rule state.
6. **Versions.** Schema major = `WIRE_MAJOR` = 1, directory `v1`. Additive optional fields are a minor change: same directory, regenerated files, old readers ignore the new field. Removing or renaming a field, or changing its type, is a major change: new `v2` directory, new `WIRE_MAJOR`, and the server refuses other majors. Worker packages support Python 3.9 and later. The lab server supports Python 3.11–3.13.

**Alternatives rejected.**
- Generating the schemas from the stdlib dataclasses (`eidos_wire.messages.json_schema`, kept as a descriptive helper). The design names server boundary models as the source, and the dataclasses cannot express patterns, enums or bounds without adding a schema library to the worker packages.
- Validating with `jsonschema` at runtime. That adds a production dependency for what the Pydantic models already enforce, and the fixture tests show the two agree.
- `additionalProperties: false`. It would break the promise that a newer minor writer's optional fields are accepted.

**Consequences.** A wire change now needs a schema regeneration and a fixture re-emission in the same commit, and CI enforces both. Messages that are well-formed but mistyped (a list `receipt`, a numeric `card`) are refused at the boundary with a field-level message instead of failing later.
