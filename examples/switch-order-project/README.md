# switch-order-project

Ordinary custom PyTorch code (a model that must decide which of two switches fired first) from the
Eidos design handoff, plus a thin Eidos adapter. Nothing here depends on Eidos except
`eidos_adapter.py`, which only follows the adapter protocol (`build`, `predict`, ...) by duck typing
and does not import Eidos.

* `switch_order.py`, `pair_report.py` - the researcher's original scripts (unmodified apart from a
  provenance header). `SwitchNet` averages embeddings and cannot see order; `OrderedSwitchNet`
  weights positions. The recorder input agrees with the label 98% of the time.
* `eidos_adapter.py` - `ADAPTER`: `build(config)` (`config["builder"]` = `SwitchNet`,
  `OrderedSwitchNet` or `module:Class`), `predict`, `examples`, `loss`/`loss_terms`
  (`balance_groups`), `make_optimizer`, `train_batches` (the original sequential stream as a pure
  function of the batch index), `evaluation_rows` and `phases`.

## Register it with Eidos

1. Create an environment with Python >= 3.9 and torch, then `pip install -e examples/switch-order-project`
   (or `pip install torch` only: the lab puts the project root on `sys.path`).
2. Register that interpreter and this directory as the environment's `project_root`.
3. Run the built-in components with `adapter = "eidos_adapter:ADAPTER"`, for example
   `eidos.pytorch.train` with `{"adapter": "eidos_adapter:ADAPTER", "builder": "OrderedSwitchNet",
   "steps": 45, "seed": 41}`, then `eidos.pytorch.evaluate` with
   `{"evaluation": {"split": "test", "data_seed": 76102, "pairs": 4000}}`.

Standalone use: `python switch_order.py` reproduces the original experiments.
