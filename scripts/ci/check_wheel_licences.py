"""Check that every built Eidos wheel declares Apache-2.0 and ships LICENSE and NOTICE.

Usage: python scripts/ci/check_wheel_licences.py DIST_DIR [--expect-version V]
Standard library only. Exit 1 on any missing or wrong item.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import sys
import zipfile
from pathlib import Path

PACKAGES = ("eidos_lab", "eidos_wire", "eidos_worker_sdk", "eidos_pytorch")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dist")
    ap.add_argument("--expect-version", help="required version of eidos-lab (e.g. 1.0.0)")
    a = ap.parse_args()
    repo = Path(__file__).resolve().parents[2]
    licence_sha = hashlib.sha256((repo / "LICENSE").read_bytes()).hexdigest()
    problems: list[str] = []
    seen = set()
    for whl in sorted(Path(a.dist).glob("*.whl")):
        pkg = whl.name.split("-")[0]
        if pkg not in PACKAGES:
            continue
        seen.add(pkg)
        with zipfile.ZipFile(whl) as z:
            names = z.namelist()
            meta_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
            meta = email.parser.Parser().parsestr(z.read(meta_name).decode())
            if meta.get("License-Expression") != "Apache-2.0":
                problems.append(f"{whl.name}: License-Expression is {meta.get('License-Expression')!r}")
            for f in ("LICENSE", "NOTICE"):
                hits = [n for n in names if n.endswith(f".dist-info/licenses/{f}")]
                if not hits:
                    problems.append(f"{whl.name}: no {f} in dist-info/licenses")
                elif f == "LICENSE" and hashlib.sha256(z.read(hits[0])).hexdigest() != licence_sha:
                    problems.append(f"{whl.name}: LICENSE differs from the repository LICENSE")
            if pkg == "eidos_lab" and a.expect_version and meta.get("Version") != a.expect_version:
                problems.append(f"{whl.name}: version {meta.get('Version')} != {a.expect_version}")
        print(f"{whl.name}: License-Expression={meta.get('License-Expression')} Version={meta.get('Version')}")
    missing = set(PACKAGES) - seen
    if missing:
        problems.append(f"wheels missing for {sorted(missing)}")
    for p in problems:
        print("PROBLEM:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
