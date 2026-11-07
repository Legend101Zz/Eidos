# image-project

A second, unrelated small model: a conv net that classifies the direction of a bar in a 12x12
image, plus the original exposure audit (the same image at two brightness gains). It exists to show
that the pair scorer `lab.pairs.by_group` works unchanged on a second model: `eidos_adapter.py`
emits the same paired-row schema (`pair_id, reference_logit, variant_logit, reference_label,
variant_label, stratum, cluster`).

* `image_reuse.py`, `pair_report.py` - the researcher's scripts (provenance header; `build_model`
  and `audit_rows` were factored out, computations unchanged).
* `eidos_adapter.py` - `ADAPTER` (builder `ConvNet`; the training stream reproduces the original
  `make_checkpoint`: seed 17, stream seed 170, 80 steps of 64 images).

Register like the switch project: environment with torch, `project_root` = this directory,
`adapter = "eidos_adapter:ADAPTER"`. Evaluate with
`{"evaluation": {"split": "audit", "data_seed": 180, "pairs": 400}}`.
