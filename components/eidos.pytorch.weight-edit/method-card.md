# eidos.pytorch.weight-edit 1.0.0 (change-weights; project environment)

**Question.** What happens if named parameter entries are changed directly (zeroed, scaled, shifted),
with no further training?

**Config.** `edits`: list of `{entry, index?, op: set|scale|add, value}` (`entry` is a model state-dict
name, `index` an integer list into that tensor, e.g. `readout.weight` `[0, -1]`), optional `reason`.

**What it does.** Loads the parent checkpoint, builds the child through `apply_migration` (every other
state entry copied, so optimizer, RNG, data cursor and component state are preserved), edits only the
child's cloned model tensors, checks the child shares no storage with the parent, and verifies the
parent file's digest and model fingerprint are unchanged afterwards.

**Receipt.** `edits` (entry, index, op, before/after for small tensors, norms), `parent_digest`,
`parent_unchanged`, `storage_independent`, `restore_class: warm-start`, `migration_actions`,
`checkpoints`, fingerprints.

**Cannot support.** That the edit is the *cause* of any change in behaviour (same-size random or
reverted edits are separate controls) or that it generalises beyond the evaluated population.

**Tests.** Author fixtures only.
