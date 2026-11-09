# lab.pairs.by_group 1.0.0 (paired-report/1)

**Question.** For prediction pairs (an original example and its twin), how often is the model
right on the original, and how often is it right on *both*? Reported overall and per group.

**Metric identities.** `both_correct_rate` = pairs with reference decoded correctly AND variant decoded
correctly / all pairs. `reference_accuracy` = reference decoded correctly / all pairs. Decoding:
score > 0 is class 1, score <= 0 (including ties) is class 0. Changing the numerator (for example to
"either correct") is a different metric identity even if the output shape is unchanged.

**Validation.** Rejects (failed run, category `contract`): duplicate pair ids, non-numeric or
non-finite scores, labels other than 0/1, missing columns, empty group values, zero rows.

**Assumptions.** Scores are binary-decision logits from one model; the grouping column is chosen by the
caller (`config.group_by`, default `stratum`). Pairs are the unit of analysis; they are *not*
independent replications of a training procedure (pairs from one model share its randomness) - use the
`cluster` column and a cluster-robust assessor for inference.

**Cannot support.** Any claim about causes, other populations, or uncertainty by itself.

**Tests.** Fixtures under `fixtures/` are author tests, not independent qualification.
