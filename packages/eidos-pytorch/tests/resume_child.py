"""Fresh-process helper: resume from a checkpoint and report the parameter fingerprint."""

import json
import sys

import lab_models

from eidos_pytorch import state_fingerprint, train_loop

ckpt, steps, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
res = train_loop(
    lab_models.DropoutAdapter(),
    {},
    lab_models.dropout_batch_fn,
    steps=steps,
    resume_from=ckpt,
    seed=999,
    deterministic=True,
)
with open(out, "w") as handle:
    json.dump(
        {
            "params": state_fingerprint(res.model),
            "opt": state_fingerprint(res.optimizer.state_dict()),
            "receipt": res.receipt,
        },
        handle,
    )
