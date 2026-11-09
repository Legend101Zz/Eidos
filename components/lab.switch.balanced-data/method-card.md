# lab.switch.balanced-data 1.0.0 (generate-data)

**Question.** Produce switch-order sequences with a known share of "exception" examples (recorder
disagrees with the label) and report exactly what was realized, not only what was requested.

**Why two modes.** `rounded-per-batch` makes each batch carry `round(batch_size * rate)` exceptions, so
0.5% with batches of 100 becomes 0 (`round(0.5) == 0`), whereas `bernoulli` makes each example an
exception independently with probability `rate`. A balancing or reweighting rule that assumes "exactly
the requested rate" is wrong in the first case; the receipt's `requested_exceptions` vs
`realized_exceptions` and per-batch min/max show it.

**Output.** JSONL rows `{id, events, pos_a, pos_b, target, recorder, exception, batch}` (schema
`switch-sequences/1`), deterministic given `seed`. Standard library only (`random.Random`).

**Limits.** Synthetic; the generator is a re-implementation in the spirit of the handoff's
`draw_batch` (it does not reproduce its torch random stream). It does not validate that a consumer
interprets `exception` correctly.

**Tests.** Fixtures are author tests, not independent qualification.
