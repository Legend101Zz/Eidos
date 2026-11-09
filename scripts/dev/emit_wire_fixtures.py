"""Emit the worker wire golden fixtures through the real worker SDK code paths.

    python scripts/dev/emit_wire_fixtures.py           # (re)write tests/fixtures/wire/v<major>/*.json
    python scripts/dev/emit_wire_fixtures.py --check   # exit 1 if the emitted messages differ

Runs in any environment that has ``eidos-wire`` and ``eidos-worker-sdk`` (Python 3.9+, standard library
only): the server dev environment and the worker-compat matrix (Python 3.9, pydantic<2) both use it.

Valid fixtures come from ``eidos_worker.testing.make_invocation``, ``eidos_worker.run.execute`` and
``eidos_wire.load_manifest`` on ``tests/fixtures/wire/component``. ``*-newer-minor`` fixtures add unknown
fields to an emitted message (what a newer minor writer may send). ``invalid-*`` fixtures are emitted
messages with one deliberate contract violation each.

Volatile values are normalised so the output is byte-for-byte reproducible:
temporary directories become ``/eidos-fixture/attempt`` (and the component directory
``/eidos-fixture/component``), ``receipt.wall_seconds`` becomes ``0.0``, the staged run log (it holds
timestamps) gets digest ``0`` x 64 and size 0, and ``error.traceback_tail`` becomes ``<traceback>``.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from eidos_wire import WIRE_MAJOR, load_manifest
from eidos_worker.run import execute
from eidos_worker.testing import make_invocation

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "tests" / "fixtures" / "wire" / "component"
OUT = ROOT / "tests" / "fixtures" / "wire" / f"v{WIRE_MAJOR}"
ATTEMPT_PLACEHOLDER = "/eidos-fixture/attempt"
COMPONENT_PLACEHOLDER = "/eidos-fixture/component"
ROWS = b"a,b\n1,x\n2,y\n3,x\n"


def _replace_paths(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _replace_paths(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_paths(v, mapping) for v in value]
    if isinstance(value, str):
        for real, placeholder in mapping.items():
            if value == real or value.startswith(real + "/"):
                return placeholder + value[len(real):]
    return value


def _normalise_manifest(data: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    if "wall_seconds" in data.get("receipt", {}):
        data["receipt"]["wall_seconds"] = 0.0
    for entry in data.get("outputs", []):
        if entry.get("role") == "log":
            entry["digest"], entry["size"] = "0" * 64, 0
    if isinstance(data.get("error"), dict) and "traceback_tail" in data["error"]:
        data["error"]["traceback_tail"] = "<traceback>"
    return data


def _attempt(entry: str, base: Path) -> dict[str, Any]:
    inv = make_invocation(
        base,
        entry,
        inputs={"rows": (ROWS, "text/csv")},
        config={"scale": 2},
        source_dir=COMPONENT,
        limits={"max_output_bytes": 65536},
        seed=7,
        component_id="fixture.wire-demo",
    )
    manifest = execute(inv)
    mapping = {str(base): ATTEMPT_PLACEHOLDER, str(COMPONENT): COMPONENT_PLACEHOLDER}
    return {
        "invocation": _replace_paths(inv.to_dict(), mapping),
        "manifest": _normalise_manifest(_replace_paths(manifest.to_dict(), mapping)),
    }


GRANT = {"grant_id": "llmg_attempt-test", "token": "grant-token-0123456789abcdef", "socket": "/run/eidos-llm.sock",
         "models": ["fake:echo"], "max_requests": 4, "max_tokens": 2000, "max_usd": None,
         "expires_at": "2026-11-09T16:00:00+00:00"}


def emit() -> dict[str, Any]:
    """Fixture name (without ``.json``) -> message. Deterministic."""
    work = Path(tempfile.mkdtemp(prefix="eidos-wire-fixtures-")).resolve()
    try:
        ok = _attempt("wire_fixture:run", work / "ok")
        failed = _attempt("wire_fixture:fail", work / "failed")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    component = load_manifest(COMPONENT / "eidos-component.json").to_dict()
    inv, succeeded = ok["invocation"], ok["manifest"]

    def variant(base: dict[str, Any], change: Any) -> dict[str, Any]:
        data = copy.deepcopy(base)
        change(data)
        return data

    def newer_minor_manifest(d: dict[str, Any]) -> None:
        d["provenance_note"] = "optional field added by a newer minor version"
        d["outputs"][0]["checksum_algorithm"] = "sha256"

    def drop_entry(d: dict[str, Any]) -> None:
        del d["component"]["entry"]

    fixtures: dict[str, Any] = {
        "invocation": inv,
        "invocation-newer-minor": variant(inv, lambda d: d.update(deadline_hint_seconds=600)),
        # Since eidos-wire 1.1 (ADR 0013): an attempt with a scoped LLM grant (a grant token, never a provider key).
        "invocation-llm-grant": variant(inv, lambda d: d.update(llm_grant=dict(GRANT))),
        "invalid-invocation-llm-grant-token-missing": variant(
            inv, lambda d: d.update(llm_grant={k: v for k, v in GRANT.items() if k != "token"})),
        "output-manifest-succeeded": succeeded,
        "output-manifest-failed": failed["manifest"],
        "output-manifest-newer-minor": variant(succeeded, newer_minor_manifest),
        "component-manifest": component,
        "invalid-invocation-epoch-zero": variant(inv, lambda d: d.update(epoch=0)),
        "invalid-invocation-entry-missing": variant(inv, drop_entry),
        "invalid-output-manifest-status": variant(succeeded, lambda d: d.update(status="done")),
        "invalid-output-manifest-wire-major": variant(succeeded, lambda d: d.update(wire="eidos-wire/2")),
        "invalid-output-manifest-unsafe-filename": variant(
            succeeded, lambda d: d["outputs"][0].update(filename="../outside.json")
        ),
        "invalid-output-manifest-size-string": variant(succeeded, lambda d: d["outputs"][0].update(size="12")),
        "invalid-output-manifest-receipt-not-object": variant(succeeded, lambda d: d.update(receipt=["input_rows", 3])),
        "invalid-component-manifest-kind": variant(component, lambda d: d.update(kind="plugin")),
        "invalid-component-manifest-card-type": variant(component, lambda d: d.update(card=42)),
    }
    return fixtures


def render(message: Any) -> str:
    return json.dumps(message, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Emit the eidos-wire golden fixtures")
    ap.add_argument("--check", action="store_true", help="compare instead of writing; exit 1 on any difference")
    args = ap.parse_args(argv)
    wanted = {name + ".json": render(msg) for name, msg in emit().items()}
    present = {p.name for p in OUT.glob("*.json")} if OUT.is_dir() else set()
    if args.check:
        stale = sorted(n for n, text in wanted.items() if not (OUT / n).is_file() or (OUT / n).read_text() != text)
        extra = sorted(present - set(wanted))
        for n in stale:
            print(f"differs from the SDK output: {OUT / n}", file=sys.stderr)
        for n in extra:
            print(f"not emitted (remove it): {OUT / n}", file=sys.stderr)
        if stale or extra:
            return 1
        print(f"{len(wanted)} wire fixtures match the worker SDK output")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n in present - set(wanted):
        (OUT / n).unlink()
    for n, text in wanted.items():
        (OUT / n).write_text(text)
    print(f"wrote {len(wanted)} fixtures to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
