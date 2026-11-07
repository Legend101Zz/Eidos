"""Card adapter (builder-side): private child of an immutable parent checkpoint with an explicit weight
edit, using the release's own lifecycle.restore/advance (the child's training continuation is the
release's fixture). The parent file is only read; its hash is recorded before and after."""
import argparse
import hashlib
import json
from pathlib import Path

import torch
from lifecycle import advance, capture, restore

p = argparse.ArgumentParser()
p.add_argument("--parent", required=True)
p.add_argument("--steps", type=int, required=True)
p.add_argument("--edit", type=float, default=0.2)
p.add_argument("--output", required=True)
a = p.parse_args()
before = hashlib.sha256(Path(a.parent).read_bytes()).hexdigest()
m, o, g, meta = restore(torch.load(a.parent, weights_only=False))
with torch.no_grad():
    next(m.parameters()).add_(a.edit)
advance(m, o, g, meta, a.steps)
torch.save(capture(m, o, g, meta), a.output)
after = hashlib.sha256(Path(a.parent).read_bytes()).hexdigest()
assert before == after
Path(a.output).with_name("receipt.json").write_text(json.dumps({"parent_sha256": before, "parent_unchanged": 1, "edit": a.edit, "step": meta["step"]}))
