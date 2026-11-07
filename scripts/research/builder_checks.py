"""Reference-assisted builder checks run with the research dev interpreter (torch). Operates on
downloaded Eidos artifacts; imports the release's lifecycle.py for exact state equality."""
import hashlib
import json
import os
import sys
from pathlib import Path

DEVCOPY = Path(os.environ.get("EIDOS_RESEARCH_ROOT", "/workspace/.eidos-dev/h2/research")) / "devcopy"


def main(argv):
    sys.path.insert(0, str(DEVCOPY))
    import torch
    from lifecycle import equal
    cmd = argv[0]
    if cmd == "ckpt-compare":  # a b -> exact equality of full state; model-state equality
        a, b = (torch.load(x, weights_only=False) for x in argv[1:3])
        print(json.dumps({"state_exact_equal": bool(equal(a, b)), "model_equal": bool(equal(a["model"], b["model"])),
                          "steps": [a["meta"]["step"], b["meta"]["step"]]}))
    elif cmd == "labels":  # project split out -> npz(ids, y): visible development labels for the Eidos-side scorers
        import numpy as np
        from data import dataset
        data, _, _ = dataset(argv[1])
        _, y, ids = data[argv[2]]
        np.savez(argv[3], ids=ids, y=y)
        print(json.dumps({"n": int(len(y))}))
    elif cmd == "sha":
        print(json.dumps({"sha256": hashlib.sha256(Path(argv[1]).read_bytes()).hexdigest()}))
    else:
        raise SystemExit("unknown command")


if __name__ == "__main__":
    main(sys.argv[1:])
