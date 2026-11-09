# lab.learning.group-balanced-ema 1.0.0 (training extension, change-learning)

**Question.** Does giving rare-but-important training groups a persistent, equally weighted share of the
update (and remembering their gradient between updates) change what a model learns, compared with the
ordinary mean loss, the same extra group-gradient computation with memory disabled (`decay = 0`), and
the full rule? Compare those three arms with matched examples and report the added runtime.

**Mechanism.** At `before-optimizer-step`, per update: recompute per-group losses through the adapter's
`loss_terms(model, batch)`, take each group's gradient, update `ema[g] = decay * ema[g] + (1 - decay) *
grad[g]` (first sight: `ema[g] = grad[g]`), then set `param.grad = mean_g ema[g]`. The mean over groups
is unweighted, so group frequency does not matter.

**Owned state.** `group-ema/1`: `{decay, updates, extra_backward, groups: {group id: {param name: tensor}}}`,
saved as `component.<name>` in the checkpoint. Resuming restores it exactly. Branching from a checkpoint
that lacks it needs an explicit `init` for that entry.

**Requirements and failure modes.** Needs `adapter.loss_terms`. Batches must be re-evaluable (no
dropout noise unless you accept that the recomputed gradient differs from the one that was just
back-propagated). Gradients of a group that appears rarely are dominated by stale memory when `decay` is
large. Grad scaling and gradient clipping are not handled (the rule replaces unscaled gradients).

**Cannot support.** That group weighting is the cause of any improvement: the extra computation, the
weighting and the memory are separate ingredients, hence the matched ablation arms above.

**Tests.** `fixtures/test_group_ema.py` is an author test (run by the repository's component tests); it
is not independent qualification.
