# Title
Iteration 0: the loop works (retrospective)

## Retrospective

**Retrospective registration.** The infrastructure this experiment validates (treatments and flags, provider
profiles, card pools, ledger and merge gate, seams and autopilot, scorecard and comparison, runner) was built in
work packages WP2–WP6b2 before this ledger existed, and this registration was written on 15 Nov 2026 while
iteration 0 was running (model-free steps and the first live checks had already run). It records what was measured;
it cannot claim to have been registered before the evidence was seen. Tracking issue: #42.

## Mechanism and evidence
The improvement programme needs one command that turns a variant and a pool into a scorecard, a ledger that records
every attempt, honest model-free and live tiers, three providers through the study run path, and a measured noise
floor before any small difference is trusted. Sources: `claude-orch/improvement-programme/v2/RUNBOOK.md` section 9;
`v2/HYPOTHESES.md` H0.

## Change
No change to Eidos behaviour. The default treatment `pilot4-v1` is byte-identical to Pilot 4's configuration (golden
tests). Everything new is dormant behind treatments and provider profiles.

## Prediction
0a stub and autopilot on the open D pool produce scorecards; the autopilot verifies where the reference clears the
bar and makes no claim on the null card. 0b the default treatment equals Pilot 4's configuration. 0c the three
provider profiles name live models. 0d one short run per provider concludes with no false claim. 0e main-vs-main
paired runs: the comparison of identical arms decides no difference; the spread of VRG and MRG is reported.

## Falsifier
Any false claim; a default-treatment byte difference; a provider that cannot conclude; a rule that would keep a
difference between identical arms.

## Risks to the two invariants
Never a false claim: every run is graded by the independent sealed grader; false claims are a veto. Search separate
from confirmation: only the open pool D is used; E and the held-out pool are not consulted.

## Exposure
Pool D (open). E and H: not run, not consulted. Legacy development cards: not run in this experiment.
