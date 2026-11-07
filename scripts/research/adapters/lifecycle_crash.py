"""Card adapter (builder-side): run the release's lifecycle worker so that it exits with code 17 right
after committing its checkpoint, verify that, and finish normally so the committed checkpoint is staged.
This is a subprocess death inside one Eidos job, NOT a death of an Eidos worker."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, required=True)
p.add_argument("--steps", type=int, required=True)
p.add_argument("--output", required=True)
a = p.parse_args()
r = subprocess.run([sys.executable, "lifecycle.py", "worker", "--seed", str(a.seed), "--steps", str(a.steps),
                    "--output", a.output, "--crash"], capture_output=True, text=True, timeout=60)
assert r.returncode == 17, (r.returncode, r.stderr[-500:])
assert Path(a.output).is_file(), "no committed checkpoint after the worker exit"
Path(a.output).with_name("receipt.json").write_text(json.dumps({"worker_returncode": r.returncode, "committed_step": a.steps}))
