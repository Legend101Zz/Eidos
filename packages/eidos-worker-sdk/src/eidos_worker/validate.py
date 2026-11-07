"""``python -m eidos_worker.validate <component-dir>`` — manifest check plus declared fixtures.

The component directory holds ``eidos-component.json`` (or ``.toml`` on Python 3.11+). Every entry of
the manifest's ``controls`` list that ends in ``.json`` and exists relative to the directory is a
fixture. Missing fixture files are reported as skipped, never as passes.

Fixture format::

    {"name": "...",
     "inputs": {"predictions": {"file": "data/p.csv", "media_type": "text/csv"}
                | {"text": "a,b\\n1,2\\n", "media_type": "text/csv"} | {"json": {...}}},
     "config": {...},
     "expect": {"status": "succeeded" | "failed",
                "error_category": "contract",            # optional
                "receipt": {"input_rows": 2},            # subset match, optional
                "outputs": {"report": {"json": {...}}}}} # exact JSON match, optional

Each fixture runs through the real subprocess protocol. Succeeded runs must stage only slots that
the manifest declares under ``output`` (plus the log) and, when a schema is given, with that schema.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from eidos_wire import ManifestError, OutputManifest, load_manifest
from eidos_worker.testing import make_invocation, run_subprocess

MANIFEST_NAMES = ("eidos-component.json", "eidos-component.toml")


@dataclass
class FixtureResult:
    path: str
    passed: bool
    skipped: bool = False
    problems: List[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    component: Optional[str]
    manifest_problems: List[str] = field(default_factory=list)
    fixtures: List[FixtureResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.manifest_problems and all(f.passed or f.skipped for f in self.fixtures)


def validate_component(directory: Any, timeout: float = 120.0) -> ValidationReport:
    root = Path(directory).resolve()
    manifest_path = next((root / n for n in MANIFEST_NAMES if (root / n).is_file()), None)
    if manifest_path is None:
        return ValidationReport(None, [f"no {' or '.join(MANIFEST_NAMES)} in {root}"])
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return ValidationReport(None, list(exc.problems))
    except Exception as exc:  # noqa: BLE001
        return ValidationReport(None, [f"cannot load manifest: {exc}"])
    report = ValidationReport(manifest.ref)
    for control in manifest.controls:
        if not control.endswith(".json"):
            continue
        fixture_path = root / control
        if not fixture_path.is_file():
            report.fixtures.append(
                FixtureResult(control, False, skipped=True, problems=["fixture file not present"])
            )
            continue
        report.fixtures.append(_run_fixture(root, manifest, fixture_path, control, timeout))
    return report


def _run_fixture(root: Path, manifest, fixture_path: Path, control: str, timeout: float) -> FixtureResult:
    problems: List[str] = []
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="eidos-fixture-") as tmp:
            inputs: Dict[str, Any] = {}
            for name, spec in (fixture.get("inputs") or {}).items():
                media = spec.get("media_type")
                if "file" in spec:
                    inputs[name] = (root / spec["file"], media or "application/octet-stream")
                elif "text" in spec:
                    inputs[name] = (spec["text"], media or "text/plain")
                else:
                    inputs[name] = (spec["json"], media or "application/json")
            inv = make_invocation(
                tmp,
                manifest.entry,
                inputs=inputs,
                config=fixture.get("config"),
                source_dir=root,
                limits=fixture.get("limits"),
                component_id=manifest.id,
            )
            proc = run_subprocess(inv, timeout=timeout)
            if proc.returncode != 0:
                return FixtureResult(
                    control, False, problems=[f"runner exit {proc.returncode}: {proc.stderr[-500:]}"]
                )
            out = OutputManifest.read(inv.staging_dir)
            expect = fixture.get("expect") or {"status": "succeeded"}
            if out.status != expect.get("status", "succeeded"):
                problems.append(
                    f"status {out.status!r} != expected {expect.get('status', 'succeeded')!r}: {out.error}"
                )
            if "error_category" in expect and (out.error or {}).get("category") != expect["error_category"]:
                problems.append(
                    f"error category {(out.error or {}).get('category')!r} != {expect['error_category']!r}"
                )
            for key, want in (expect.get("receipt") or {}).items():
                if out.receipt.get(key) != want:
                    problems.append(f"receipt[{key!r}] = {out.receipt.get(key)!r}, expected {want!r}")
            for slot, want in (expect.get("outputs") or {}).items():
                entry = next((e for e in out.outputs if e.slot == slot), None)
                if entry is None:
                    problems.append(f"expected output slot {slot!r} missing")
                elif "json" in want:
                    got = json.loads((Path(inv.staging_dir) / entry.filename).read_text(encoding="utf-8"))
                    if got != want["json"]:
                        problems.append(f"output {slot!r} = {got!r}, expected {want['json']!r}")
            if out.status == "succeeded":
                for entry in out.outputs:
                    if entry.role == "log":
                        continue
                    declared = manifest.output.get(entry.slot)
                    if declared is None:
                        problems.append(f"staged slot {entry.slot!r} is not a declared output port")
                    elif entry.schema is not None and entry.schema != declared:
                        problems.append(
                            f"slot {entry.slot!r} schema {entry.schema!r} != declared {declared!r}"
                        )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"fixture error: {type(exc).__name__}: {exc}")
    return FixtureResult(control, not problems, problems=problems)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m eidos_worker.validate <component-dir>", file=sys.stderr)
        return 2
    report = validate_component(argv[0])
    for p in report.manifest_problems:
        print(f"MANIFEST: {p}")
    for f in report.fixtures:
        print(f"{'SKIP' if f.skipped else 'PASS' if f.passed else 'FAIL'} {f.path} {'; '.join(f.problems)}")
    print("ok" if report.ok else "FAILED")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
