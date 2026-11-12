# Development pilot 3: charter

Status: **development evidence only, not confirmatory.** Written and committed before any arm run of this pilot. It follows [`research/public/STUDY_PROTOCOL.md`](../../../research/public/STUDY_PROTOCOL.md); where they differ, the protocol wins. The owner authorized continued iteration on 11 November 2026 ("keep going").

## Why this pilot exists
In pilot 2, the round-2 Eidos investigator verified an improvement in 12/12 blocks against 11/12 for the coding agents, with about 5× fewer tokens. **But its verified improvements on D03 were smaller** (+0.225 against about +0.29). The pilot-2 diagnosis (D2) attributed this to premature commitment, a misdiagnosed mechanism, and unused search tools. Round 3 (ADRs 0023–0025) changes how Eidos explores:
- incumbent and challenger rounds until the exploration deadline;
- a baseline error-analysis diagnosis;
- typed sweep templates;
- a compact MCP workflow for external agents;
- provider resilience.

This pilot measures the round-3 system on **four new cards built by a fixed, prospective procedure**, so that card choice cannot favour any arm.

## Cards (fixed before any arm run)
- **The procedure** was committed in `879c094` before any reference execution. The cards, bars and bundles were frozen in `5bedf94` before any stub arm. Details: `study/packaging/PROSPECTIVE_P3.md` and `QUALIFICATION_P3.md`.
- **Datasets:** scikit-learn data only, built offline. The split is train 60 / validation 20 / test 20 with seed 20261111. Test data is never packaged.
- **Baseline:** raw features, a single linear layer, Adam lr 0.01, 80 full-batch epochs, seed 11.
- **Qualification:** for every card, a reference recipe stated in advance clears the bar at seeds 11, 23 and 37. No card was dropped or tuned.

| Card | Data | Metric | Seed-11 baseline | Practical bar |
|---|---|---|---|---|
| N01 | `load_breast_cancer` | accuracy | 0.912 | +0.02 |
| N02 | `load_diabetes` | RMSE | 178.01 | −8.900 (5% relative) |
| N03 | `make_friedman1` (n=2000, noise 1.0) | RMSE | 11.04 | −0.552 (5% relative) |
| N04 | `make_classification` (20 features, 8 informative, 3 clusters per class) | accuracy | 0.688 | +0.02 |

## What may and may not be concluded
- It **cannot** confirm the +10-point decision rule. With 12 paired blocks, every interval is exploratory.
- The cards are new to all arms. Unlike the development cards, they have no public reference solutions, though they use public datasets.
- Pilots 1–3 used different cards and system versions; cross-pilot comparisons are descriptive.

## Systems under test (frozen)
- **Eidos:** wheels built from `257792a` on `task/pilot-3-tooling`, installed in `venvs/pilot-3-lab` (provenance in `pilot-build.json`). The Eidos source is identical to `main` 7e6641d (v1.1.0 + I1–I5 + round 2) merged with `task/r3-integrated` 42371dc (round 3: ADRs 0023–0025, reviewed in R14–R17, all findings fixed, CI green).
- **Study tooling:** this branch, at the commit containing this file.
- **Grader:** `Legend101Zz/eidos-grading`, qualified on N01 and N03 controls (6/6).

## Shared model and envelope (unchanged from pilots 1 and 2)
- **Model:** MiniMax-M3.1-Flash, provider defaults; the served model id is logged.
- **Limits per run:** 1800 s wall time; 8,000,000 processed tokens; CPU, one thread; stop at the first exhausted limit.
- **Arm 4 settings:** json output mode, `max_output_tokens` 16384, `request_timeout` 300 s. Setup is timed outside the envelope.

## Arms (as in pilot 2)
1. Claude Code `--bare`.
2. Arm 1 plus the standalone toolkit (now including the round-3 diagnosis component and sweep templates) and `METHOD.md`.
3. Claude Code plus `METHOD.md` plus Eidos over MCP. The tools paragraph describes the round-3 MCP workflow factually.
4. The Eidos internal investigator, with `METHOD.md` embedded.

Every selectable environment uses the research interpreter. The LLM broker is disabled. The common prompts are unchanged.

## Design
- **Runs:** 3 per arm per card, giving **48 runs** in 12 blocks of 4 concurrent arms, two blocks at a time. Block order is randomized with seed **20261111** (`study/launcher.py plan --cards N01,N02,N03,N04 --runs 3`).
- **Incidents (pilot-2 rules, as amended there):**
  - an incident is a provider 429/5xx or quota failure that ends a run without a valid final result, a lab or infrastructure crash, or a change of served model id (`<synthetic>` excluded);
  - transient retries never stop a run;
  - incidents are re-run whole, the record is kept, and the shared model is never substituted.
- **Run status:** final outcome, symmetric across arms.
- **Denominator:** all assigned runs stay in it. No re-rolls and no best-of-n.

## Outcomes
- **Primary:** `verified_success` per card, exactly as in pilot 2:
  - a valid improvement claim;
  - a run status that is not a timeout, budget stop, crash or invalid output;
  - a test-split gain at or above the card's bar;
  - reproduction at fresh seeds;
  - guardrails and scope.
- **Primary contrast:** arm 4 − arm 2 (`analyze_results.py`, family-balanced, exploratory).
- **Pre-registered secondary (quality):** the mean test gain among verified runs, per arm and card, and the per-card difference arm 4 − arm 2 in mean verified gain, in the metric's natural direction. Descriptive only, no inferential claim. This is the outcome round 3 is designed to move.
- **Also reported:** tokens, wall time, status counts, failure modes, the transcript audit, and agreement between local and GitHub grading.

## Grading
- **Verdict of record:** the private GitHub two-VM grader, together with the run-status gate. Local grading is a pre-check.
- **When:** grading happens only after a whole block ends.

## Addenda
Any defect found during the run is recorded in a committed addendum before the next affected block. No rule changes after the first run except to correct a demonstrable defect, and any such change applies to all arms.
