# eidos.pytorch.behavior-check 1.0.0 (observe; project environment)

**Question.** Will these weights behave the same when restored in a new process through the named
builder? Strict `load_state_dict` succeeding says nothing about behaviour (two architectures can share
parameter names and shapes).

**Config.** `adapter`, `builder` (the builder being checked), optional `reference_builder` (default:
the builder saved in train-meta), `adapter_config`, `python_path`, `seed`, `atol`, `rtol`.

**Method.** Record outputs on the adapter's reference batch (`adapter.examples(config)`) using the
reference builder in-process, then load the checkpoint's model entry with the checked builder in a fresh
Python process and compare (eidos_pytorch `behavioral_restore_check`).

**Receipt.** `behavior_match`, `strict_load_ok`, `max_abs_diff`, `builder`, `reference_builder`, `stage`,
`problems`. A mismatch is a *successful* observation (`behavior_match: false`), not a failed job.

**Limits.** Only the reference batch is compared; agreement on it is evidence, not proof, of equivalence.

**Tests.** Author fixtures only (OrderedSwitchNet weights through SwitchNet -> false).
