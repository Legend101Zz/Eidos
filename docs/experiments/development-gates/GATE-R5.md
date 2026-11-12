# Development gate G-R5: registration

**Status:** registered before any model call of this gate. The registration is the commit that adds this file, pushed to
`task/gate-r5-registration` before launch.

## Why this gate exists

The a7c5d19 development gate (12 November 2026) failed its candidate prerequisite. The candidate concluded 6 of 7 runs; the
seventh, D02, crashed during the conclude-only wrap-up because its final request was still in flight at the decision
deadline. That gate stays failed. It is not re-run, reinterpreted or waived.

R5 ([ADR 0042](../../decisions/0042-split-conclude-only-reserve.md), PR #27) makes two changes:
- it enlarges the conclusion reserve to 15% of the wall;
- it splits that reserve into a read slot, an armed request timeout, and one decision-only fallback request.

This gate is the fresh, prospective check of the R5 build.

## Builds

- **Candidate (new):** `86dfe5791ac75e76998912ea19efd6461b93444d`. This is the head of PR #27: R5, its review fixes, and
  two-arm support in the study controller. It must have green exact-head CI before launch.
- **Baseline (old):** the frozen runtime `257792ab56b6b984e807fe284a22fc5d31c4dd8b`, the same baseline as every previous paired
  development gate.
- **Controller:** the candidate commit's `study/` tree drives both phases, with the same producer
  (`eidos.project.command@1.0.9`, bundle `d6ea9fdd…a6a7`) and the same arm-4 configuration. Only the installed runtime differs
  between the two phases.
- **Fresh labs:** each phase gets its own fresh lab, built from exact committed archives. The no-model producer
  qualification passed for both labs, and the role-parity check confirmed that all seven rendered arm-4 prompts and charters
  are identical across the two phases.

## Fixed design (unchanged from previous gates)

- **Cards:** seven practice/development cards: N01, N02, N03, N04, D02, D03, D04.
- **Arm and seed:** arm 4 only, seed index 1, one run per card per build.
- **Per-run envelope:** 1,800 s wall, 8,000,000 processed tokens, 1 CPU thread.
- **Scheduling:** concurrency 2 per phase, with both phases running in parallel.
- **Model:** MiniMax-M3.1-Flash.
- **Reference gains:** the frozen Pilot 3 reference file, SHA-256
  `7536b73adddf94859b79ce76d049ec33375bf560f7d376770147b5c7b41d022e`.
- **Sleep prevention:** the gate controller owns a `caffeinate -dimsu -w <controller pid>` assertion. It is created before the
  first quota read or admission and held until both phases settle. Every phase verifies the assertion before it is admitted.

## Pass rule (fixed now; not changeable after launch)

The gate **passes** only if all of the following hold:

1. **Strict candidate completion:** 7/7 investigator conclusions, i.e. the candidate scorecard's `completion_pass` is true.
   Controller abstentions count as failures.
2. **Zero dead ends**, together with the existing evidence prerequisites, all true for the candidate:
   - `run_evidence_pass` and `invariant_pass`: zero dead ends, zero fabricated empty waits, zero held-out identifier matches;
   - `full_development_set`;
   - `eligible`.
3. **Comparator:** `study/evals/compare.py` exits 0 when run as `compare(baseline_scorecard, candidate_scorecard)`. Both the
   comparator (SHA-256 `171490bb…fa1e`) and its policy (`4dd33f9d…b5ac`) are byte-identical to those pinned for the a7c gate.
   The comparator has zero tolerance.

If any condition fails, the gate **fails**. A failed gate is recorded and diagnosed, then followed by a fix and a new gate under
a new registration. It is never re-run, reinterpreted or waived.

A provider or infrastructure incident is handled under the existing whole-attempt incident rules and recorded. Incidents
include terminal provider failure, host sleep, and quota exhaustion in the middle of a run.

## Admission

Each phase is admitted only on a quota observation that is:
- fresh (no more than 60 seconds old);
- valid;
- showing strictly positive active interval and weekly allowance.

Quota is always read, never inferred.

## Pinned tooling (private operations; hashes for identification)

| Item | SHA-256 |
| --- | --- |
| Gate wrapper | `f4d9c70eb2ac817711868108842590e864bf514be12aa55ea771332ee527e7ca` |
| Preflight | `856a88ec4d22e63231808a31dd0ca807475bcb7ae83eadb71c1dcd497d383ddc` |
| Quota helper | `7c2bb9f95e7f9cc6e870a664cd91be9146ac49432aed1923e89e6a611b631cc0` |
| Producer qualification | `7954a9db8d74fbf75f9fb7fec4076cdf91c045cc613771f231a87a70ac8b8aa7` |
| Candidate build receipt | `886ea651e2991e56aa96ab17d03f4b11282e1d4f351db45c2ceb0e0f09f8b448` |
| Baseline build receipt | `0c5f7ca246ea23c82d254be0008dc11a6d049e8ba4f4102b1ec10428f944b8f0` |

## Reporting

The following are retained privately and summarized in the repository README and paper:
- both scorecards;
- the comparison output;
- the terminal resource receipts;
- the sleep-prevention and cleanup receipts.

No row is selected away. Results from a single seed on seven exposed development cards are development evidence. They do not
estimate general performance.
