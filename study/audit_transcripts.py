#!/usr/bin/env python3
"""Flag exposure terms for controller review; never silently exclude an outcome."""

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study.common import write_json

PATTERNS = {"github": r"github", "reference_path": r"research/public", "curl": r"\bcurl\b", "pip": r"\bpip\b"}


def audit(path):
    path = Path(path)
    flags = []
    if not path.exists():
        return {"transcript": str(path), "missing": True, "flags": []}
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        matches = [name for name, pattern in PATTERNS.items() if re.search(pattern, line, re.I)]
        if matches:
            flags.append({"line": line_number, "terms": matches})
    return {
        "transcript": str(path),
        "missing": False,
        "flags": flags,
        "interpretation": "term matches require human review; no automatic exclusion",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    records = [audit(path) for path in sorted(args.runs_root.rglob("transcript.jsonl"))]
    write_json(args.out, records)
    print(json.dumps({"transcripts": len(records), "flagged": sum(bool(r["flags"]) for r in records)}))
