#!/usr/bin/env python3
"""Licence inventory of an installed Eidos environment, as Markdown tables (standard library + packaging).

    <venv>/bin/python scripts/dev/license_inventory.py [--out FILE]

Reads `importlib.metadata` of the interpreter that runs it (use the repo's `.venv`, which has the dev group, so dev-only packages
show up as "dev"). A distribution is `runtime` when it is reachable from `eidos-lab` through `Requires-Dist` entries that are not
behind an `extra ==` marker (extras named in a requirement, e.g. `psycopg[binary]`, are followed); everything else installed is
`dev`. The licence comes from `License-Expression`, else the `License` field (only if short), else `License ::` classifiers.
Flags: `UNKNOWN` (nothing declared), `COPYLEFT?` (GPL/LGPL/AGPL/MPL/EPL/CDDL/EUPL/SSPL in any declared text), `OWNER` (the project's own packages: licence not chosen). The flags are a screen, not legal advice.
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT = {"eidos-lab", "eidos-wire", "eidos-worker-sdk", "eidos-pytorch"}
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|GNU (Lesser |Affero )?General Public|MPL|Mozilla Public|EPL|Eclipse Public|CDDL|EUPL|SSPL)", re.I)


def licence(dist: metadata.Distribution) -> tuple[str, list[str]]:
    md = dist.metadata
    found: list[str] = []
    expr = md.get("License-Expression")
    if expr:
        found.append(expr.strip())
    lic = (md.get("License") or "").strip()
    if lic and lic.upper() != "UNKNOWN" and len(lic) <= 80 and "\n" not in lic:
        found.append(lic)
    if found and expr:
        return found[0], found[:1]  # an SPDX expression is authoritative; classifiers repeat it
    cls = [c.split("::")[-1].strip() for c in md.get_all("Classifier", []) if c.startswith("License ::")]
    found += [c for c in cls if c not in found]
    if not found and lic:
        found.append("(full licence text in metadata; see the package)")
    return (" / ".join(found) if found else "UNKNOWN"), found


def closure(dists: dict[str, metadata.Distribution], root: str) -> set[str]:
    seen: set[str] = set()
    todo = [(root, frozenset())]
    done: set[tuple[str, frozenset]] = set()
    while todo:
        name, extras = todo.pop()
        if (name, extras) in done or name not in dists:
            continue
        done.add((name, extras))
        seen.add(name)
        for raw in dists[name].requires or []:
            r = Requirement(raw)
            marker = r.marker
            if marker is not None:
                if any(e in str(marker) for e in ("extra",)):
                    if not any(marker.evaluate({"extra": e}) for e in extras):
                        continue
                elif not marker.evaluate({"extra": ""}):
                    continue
            todo.append((canonicalize_name(r.name), frozenset(r.extras)))
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    ap.add_argument("--root", default="eidos-lab")
    a = ap.parse_args()
    dists = {canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions() if d.metadata["Name"]}
    runtime = closure(dists, canonicalize_name(a.root))
    rows = []
    for name, d in sorted(dists.items()):
        text, found = licence(d)
        flags = []
        if text == "UNKNOWN":
            flags.append("UNKNOWN")
        if COPYLEFT.search(text) or any(COPYLEFT.search(f) for f in found):
            flags.append("COPYLEFT?")
        if name in PROJECT:
            text, flags = "(this project's own package; licence is an owner decision, not yet made)", ["OWNER"]
        rows.append((d.metadata["Name"], d.version, text.replace("|", "/"), "runtime" if name in runtime else "dev", ", ".join(flags)))
    lines = ["| name | version | licence (declared) | scope | flag |", "|---|---|---|---|---|"]
    lines += [f"| {n} | {v} | {t} | {s} | {f} |" for n, v, t, s, f in rows]
    lines += ["", f"{len(rows)} distributions ({sum(1 for r in rows if r[3] == 'runtime')} runtime, "
                  f"{sum(1 for r in rows if r[3] == 'dev')} dev) from Python {sys.version.split()[0]}."]
    text = "\n".join(lines) + "\n"
    if a.out:
        open(a.out, "w").write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
