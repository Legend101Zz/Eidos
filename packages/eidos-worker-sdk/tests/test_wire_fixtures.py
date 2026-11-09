"""The worker SDK emits exactly the golden wire fixtures (tests/fixtures/wire/v<major>).

Runs in the worker environments (Python 3.9 + pydantic<2 in the worker-compat matrix): only the standard
library, eidos-wire and eidos-worker-sdk. Schema validation of the same fixtures happens in the server
test environment (tests/unit/test_wire_schemas.py), so this environment never needs jsonschema.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EMITTER = REPO / "scripts" / "dev" / "emit_wire_fixtures.py"

if not EMITTER.is_file():  # pragma: no cover - only outside a repository checkout
    pytest.skip("wire fixture emitter not available outside the repository", allow_module_level=True)


def _emitter():
    spec = importlib.util.spec_from_file_location("emit_wire_fixtures", EMITTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sdk_emits_the_golden_fixtures():
    emitter = _emitter()
    emitted = emitter.emit()
    on_disk = {p.stem: p for p in emitter.OUT.glob("*.json")}
    assert sorted(emitted) == sorted(on_disk), "fixture set differs; rerun scripts/dev/emit_wire_fixtures.py"
    for name, message in emitted.items():
        assert emitter.render(message) == on_disk[name].read_text(), f"{name}: the SDK output changed"


def test_worker_reads_newer_minor_messages():
    """The stdlib wire types accept unknown fields from a newer minor version and ignore them."""
    from eidos_wire import Invocation, OutputManifest

    out = Path(_emitter().OUT)
    man = OutputManifest.from_dict(json.loads((out / "output-manifest-newer-minor.json").read_text()))
    assert man.status == "succeeded" and not hasattr(man, "provenance_note")
    inv = Invocation.from_dict(json.loads((out / "invocation-newer-minor.json").read_text()))
    assert inv.epoch == 1 and inv.component["entry"] == "wire_fixture:run"
