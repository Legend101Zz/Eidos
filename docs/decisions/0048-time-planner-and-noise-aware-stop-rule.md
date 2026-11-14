# ADR 0048 — A minimal planner and a noise-aware stop rule inside the unchanged ADR 0042 reserves

Status: Accepted for implementation (step 3)

Implementation scope: step 3 accepts the unchanged C/H reserves and noise-aware policy-3 patience described below. The pure planner, pinned plan, coverage clause and converged-early reporting remain scheduled for steps 5–6; they are not implemented by this slice. Keeping the draft number reserves the sequential ADR namespace without renumbering the accepted design.
Resolves review findings 6, 24c, 31, 32 and orchestrator decision Q2.

## Context

On N04, up to 730 s of 1418 s was authoring, 382 s of the 1800 s envelope was unused, and challenger rounds around one family added +0.000 to +0.015 validation (discovery-gap report 5.1, 3.1, 7.3). Those idle figures predate ADR 0042,
which set the conclude reserve to 15% of wall clamped to 90-600 s and kept the hold-back at 15% clamped to 120-900 s: at W = 1800 s, C = 270 s, H = 270 s, allowance A = 1260 s. ADR 0042 was amended at `86dfe57` (every conclude-only request is armed);
the reserves are unchanged. Rev 1 gave local refinement 45% of the allowance, which contradicted its own evidence (the review, finding 6); it also required the portfolio and the first preparation to fit without saying how (finding 24c), and
proposed lowering H after step 5 (finding 32 notes H is mostly idle because preparation and reproduction run inside exploration and `open-search` is refused in `finalize`).

## Decision

Reserves are unchanged for the first live smoke (Q2): C = 15% (ADR 0042) and H = 15%. The smoke records how much of H is used; a prospective `holdback_fraction 0.10, min 90` through the existing `investigator.exploration` settings is proposed
only with that data, never as a dynamic hold-back (a boundary that moves during a run breaks the prospective character of `exploration.policy`).

Add a small pure `src/eidos/domain/planner.py`: `plan(wall_s, conclude_s, holdback_s, now, measured) -> {exploration_deadline, refine_deadline, prepare_estimate, portfolio_budget_seconds}` with
`refine_deadline = exploration_deadline - prepare_estimate` and `portfolio_budget_seconds = min(350, exploration_remaining - prepare_estimate - margin)`, so round 1 never leaves no time to prepare a candidate and the last re-preparation always
fits before exploration stops. `prepare_estimate` comes from the incumbent's measured `fit_seconds` (a default of 90 s before one exists). There are no phase shares. The plan is shown in a pinned `plan` fact (step 6) and recorded through `exploration.compact`.

Stop rule under `exploration-policy/3` (opt-in): `ceiling-no-improvement` stays at `patience_rounds` rounds with no new incumbent version; with the ADR 0046 margin and the corrected `rounds()`, such a round is one with no gain above the noise floor.
At step 5 the stop is also withheld while the portfolio receipt reports `coverage_complete: false` and allowance remains (the domain reads receipts, not artifact contents). Family stopping is component-side (`family-gain-below-se`). One decision may queue
several refine variants up to the turn's call limit. After convergence the only offered spend is `rescore` on resampled train partitions and extra reproduction seeds; more challengers around a converged family are not offered. Unused time after a covered,
converged search is reported as `converged-early`, not as a defect; the planner does not force use of the wall.

## Rejected alternatives

- Fixed phase shares of the allowance (rev 1): they are advisory numbers with no evidence behind the refine share.
- Lowering H now, or a dynamic H: Q2, and prospectivity.
- Hard lab enforcement of any split of the allowance: it would refuse sound decisions over a heuristic.
- Spending idle time on more challengers: the measured return is +0.000 to +0.015.
- Cutting C because preparation is mechanical: ADR 0042's evidence concerns conclusion latency, which preparation does not change.

## Verification and limits

Tests: planner arithmetic with accelerated clocks (as in ADR 0042) for W = 600, 1800 and 3600, with and without an incumbent; `portfolio_budget_seconds` never starves preparation; `compact` size; the coverage clause (withheld while incomplete, released when complete,
unchanged for investigations without the policy, replay-identical). These establish control flow and arithmetic, not improved outcomes. Step 4 records H utilisation, the time of the first lock and unused wall; the ADR 0042 reserves must be unchanged in receipts.

Limits: the 350 s budget, the 90 s default and the six-neighbour window are initial defaults; the durations in the design are estimates until measured; the model-free gate cannot test the investigator's use of the plan; 30% of the wall remains reserved (C and H) until live data
say otherwise.
