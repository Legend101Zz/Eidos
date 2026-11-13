# Pilot 4 Addendum 4 — erratum

Recorded 13 November 2026 by the orchestrator, after the final capture and independent audit. This erratum changes no tooling,
design, card, arm, estimator, decision rule, gate or result. It corrects two statements in `ADDENDUM-4.md` (R6 `6350616`).

1. **Outcome visibility.** `ADDENDUM-4.md` says the fix "was made before any real primary estimate, interval, aggregate gain or
   outcome summary was computed." That is not accurate. At about 12:28 UTC on 13 November, before Addendum 4 was drafted, the
   orchestrator compiled and reported to the owner an **interim descriptive summary** of the 46 assignments settled at that time
   (per card × arm verified counts, mean verified gains, and a descriptive equal-card difference of +0.058). The registered estimator
   and its bootstrap interval had not been run. Addendum 4's changes were selected from the registered controller code and the
   ADR 0041 prompt-delivery contract, not from grades, verdicts or gains; an independent review confirmed that the estimator, status
   gates, verdict selection, denominators and incident rules are byte-identical (AST comparison) and that no estimate was computed
   during the fix's development. The interim summary is disclosed in the paper.
2. **PermissionError count.** `ADDENDUM-4.md` refers to eight arm-4 local-precheck PermissionErrors. The retained local grades show
   **ten** (including N07-r5/arm4, where the hosted grade independently recorded `invalid_output`). The local precheck is not the
   verdict of record; the hosted grade plus the run-status gate is.
