# eidos.qualification-runner 1.0.0 (artifact-op, observe, LAB environment)

**Question.** For a committed control suite, what does a target template/assessor release actually
output on each planted-effect and null control, repeated as the suite requires?

**How.** Config `{suite, target?, result_slot?, outcome_key?}`; the target's source directory arrives at
`invocation.ports["target.source/1"]["path"]`. For every control and repetition the runner executes the
target's manifest entry in this process through the worker runner and records
`{detected: outcome == "protocol-passed", outcome, valid}`; a run that does not produce a valid outcome is
`valid: false` (the gate then reports the control incomplete, never passed). A control input is
`{"inputs": {port: json}, "config": {...}}`; any value may carry `"$generate": {"name":
"paired-rows/1", "params": {...}}` for seeded stochastic evidence (seed + repetition index).

**Does not.** Decide approval (the lab's qualification gate does), choose controls, or make a suite
independent: independence is a property of who wrote and who could see the controls.

**Limits.** Runs target code in the lab interpreter, so it is meant for assessors/templates whose
authority is being checked inside a restricted worker, not for untrusted code.

**Tests.** The fixture runs the shipped assessor's own controls: an author test. Independent
qualification needs suites the assessor's author did not write.
