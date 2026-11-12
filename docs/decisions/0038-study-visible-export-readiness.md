# 0038: Check visible inference before locking a study submission

## Context

A successful training command can write validation predictions using fitted
state still in memory. A later training seed can reproduce that result while
the exact exported checkpoint and source still fail to run their inference
command or omit fitted preprocessing on reload. The independent evaluator
already checks a fresh process on visible validation before scoring held-out
data. Two settled development submissions exposed these distinct failures.

## Decision

Give all four study arms the same explicit instruction to exercise the declared
inference command in a fresh process on the supplied visible validation inputs
before locking or submitting. Use the exact candidate source and checkpoint,
a fresh output path and the declared dependency contract. Verify that required
state, including fitted preprocessing, survives the round trip. Compare fresh
predictions with the training-produced predictions after aligning unique IDs.

The common prompt supplies the instruction across all four entry points. The
operator charter records the same text. Any changes prompted by the check are
authored within the existing assigned budget before lock. An unresolved
limitation can be reported or support an abstention. The intervention is
instructional. It adds no execution tool, validator, automatic relock or
post-submission repair. The study guide assignment, sealed task files,
independent grading, and the previously observed outcomes are preserved.

## Verification and interpretation

Offline fixtures exercise a checkpoint reload exception, a successful reload
with mismatching predictions and a matching round trip after ID alignment. The
private source-bound replay uses the exact settled development submissions on
visible validation. It demonstrates the failure mechanisms without establishing
what a model would do with the instruction. Visible parity remains separate
from independent confirmation. The changed shared controller and prompt
require matched prospective qualification before use in the study.
