"""Generate the versioned worker wire JSON Schemas from the server boundary models.

    uv run python scripts/dev/gen_wire_schemas.py           # (re)write schemas/eidos-wire/v<major>/
    uv run python scripts/dev/gen_wire_schemas.py --check   # exit 1 if the files differ (CI)

Source of truth: ``eidos.boundary.wire`` (Pydantic v2). Output is deterministic: sorted keys, two-space
indent, trailing newline, no timestamps or tool versions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eidos.boundary.wire import PUBLISHED, schema_document
from eidos_wire import WIRE_MAJOR

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "schemas" / "eidos-wire" / f"v{WIRE_MAJOR}"


def render() -> dict[str, str]:
    """File name -> exact file text for every published schema."""
    return {
        f"{name}.schema.json": json.dumps(schema_document(name), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        for name in PUBLISHED
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare instead of writing; exit 1 on any difference")
    args = ap.parse_args(argv)
    wanted = render()
    present = {p.name for p in OUT.glob("*.schema.json")} if OUT.is_dir() else set()
    if args.check:
        stale = sorted(n for n, text in wanted.items() if not (OUT / n).is_file() or (OUT / n).read_text() != text)
        extra = sorted(present - set(wanted))
        for n in stale:
            print(f"out of date: {OUT.relative_to(ROOT) / n}", file=sys.stderr)
        for n in extra:
            print(f"not generated (remove it): {OUT.relative_to(ROOT) / n}", file=sys.stderr)
        if stale or extra:
            print("run: uv run python scripts/dev/gen_wire_schemas.py", file=sys.stderr)
            return 1
        print(f"{len(wanted)} wire schemas up to date in {OUT.relative_to(ROOT)}")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n in present - set(wanted):
        (OUT / n).unlink()
    for n, text in wanted.items():
        (OUT / n).write_text(text)
    print(f"wrote {len(wanted)} schemas to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
