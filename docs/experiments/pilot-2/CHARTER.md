# Development pilot 2: charter

Status: **development evidence only, not confirmatory.** Written and committed before any arm run of this pilot. It follows [`research/public/STUDY_PROTOCOL.md`](../../../research/public/STUDY_PROTOCOL.md); where they differ, the protocol wins. The owner authorized this pilot on 11 November 2026.

## Why this pilot exists
Development pilot 1 (`docs/experiments/pilot-1/`) found that the Eidos internal investigator did not outperform a coding agent given the same toolkit and method on the one improvement card. A diagnosis of its transcripts attributed the loss mainly to execution friction, low search density and an under-claim. Part of it was a study-setup confound: a selectable environment without NumPy. Eidos was then changed, and the setup was corrected. This pilot measures the changed system on improvement cards that **played no part in that diagnosis or in the design of those changes** (the cards are public; "unseen" means unseen by that work, not secret).

## What may and may not be concluded
- It can show how each arm performs on four improvement cards from four project families, with 12 paired runs per arm, and whether the changes closed the gap seen in pilot 1.
- It **cannot** confirm or refute the approved decision rule (arm 4 − arm 2 ≥ +10 points with the lower 95% bound above +0.10). The protocol's power calculation needs about 280 independent pairs. Any interval here is exploratory.
- Pilot 1 used a different card and an earlier system. Comparing pilot 1 with pilot 2 is descriptive only.

## Systems under test (frozen)
- **Eidos:** source at `6258978` on branch `task/pilot-2-tooling`. That is `main` b28a06b (v1.1.0 plus I1–I5, the pilot-1 improvements) merged with `task/r2-integrated` 434ee67 (round-2 changes, all CI-green and independently reviewed in R10–R13):
  - ADR 0020, investigator reliability and completion;
  - ADR 0021, assessment by default and evidence-derived claim state;
  - ADR 0022, dependent batches and a recipe-sweep component.

  Wheels are built once from that commit into `venvs/pilot-2-lab` (provenance in `pilot-build.json`). Arms 3 and 4 use them. Round 2 is not yet merged into `main`.
- **Study tooling:** this branch at the commit containing this file. It differs from `6258978` only in `study/` and `docs/`, including the assessor and template release references (1.2.0) fixed by a live smoke before this charter.
- **Grader:** `Legend101Zz/eidos-grading` at `56a8889`, qualified on D03 and D04 controls.

## Shared model and envelope (unchanged from pilot 1)
- **Model:** MiniMax-M3.1-Flash, provider-default sampling and reasoning; the served model id is logged.
- **Limits per run:** 1800 s wall time; 8,000,000 total processed tokens (input including cache reads, plus output); CPU only, one thread; stop at the first exhausted limit.
- **Arm 4 settings:** json output mode, `max_output_tokens` 16384, `request_timeout` 300 s. Lab and project setup are timed separately (`setup_wall_s`) and are outside the 1800 s.

## Arms (as in pilot 1, with parity corrections)
1. Claude Code `--bare` with ordinary tools.
2. Arm 1, plus the standalone component toolkit (including the new sweep component) and `METHOD.md`.
3. Claude Code plus `METHOD.md` plus Eidos over loopback MCP.
4. The Eidos internal investigator, with `METHOD.md` embedded as in arms 2–3.

**Corrected before this pilot:**
- every selectable lab environment uses the same research interpreter as arms 1–3;
- arm 4 receives the same factual note as arm 3 about baseline execution;
- the LLM broker is disabled in every arm.

## Design
- **Cards:** D03 (shifted-image accuracy), D04 (poorly conditioned regression), D05 (seasonal forecasting), D06 (positional information). Four project families. Each is packaged by `study/packaging/package_card.py`; the packaged directory is each arm's only task input.
- **Runs:** 3 per arm per card, giving **48 runs** in 12 blocks of 4 arms.
- **Blocks:** a block is (card, run index). The 4 arms of a block run concurrently, with up to 2 blocks at a time. Block order is randomized with seed **20261111** (`study/launcher.py plan --cards D03,D04,D05,D06 --runs 3`).
- **Incidents:** a provider 429/5xx or quota error, a lab or infrastructure crash, or a model-id change inside a block is an incident. The whole block is re-run and the record kept.
- **Run status (prospective change, symmetric across arms):** a run is `crash` only if it ends without a valid final result. Recovered intermediate errors do not make a crash.
- **Denominator:** all assigned runs stay in it. No re-rolls and no best-of-n.

## Success (all four cards are primary-eligible)
`verified_success` requires all of these:
- a valid answer that claims an improvement;
- a run status that is not a timeout, budget stop, crash or invalid output;
- a test-split gain over the seed-11 baseline of at least the card's practical bar;
- reproduction at fresh initialization seeds;
- guardrails and claim scope.

| Card | Practical bar |
|---|---|
| D03 | +0.01 accuracy on shifted test images |
| D04 | RMSE reduced by at least 0.5 |
| D05 | series-held-out RMSE reduced by at least 0.5 |
| D06 | +0.30 accuracy, fresh initializations required |

Every arm receives the same bar sentence (`study/cards.json`). For arms 3 and 4 the same bar is also a prospective charter rule; where no built-in assessor exists (RMSE), the gap is documented in `study/eidos_arms/CHARTER_RULES.md`.

**Arm-4 answer mapping (fixed now):** a "candidate submitted for confirmation" with a positive point estimate is exported as `improvement_claimed` with a scope that says confirmation is pending. The evaluator decides success for every arm.

## Grading
- **Verdict of record:** the private GitHub grader (two fresh VMs per candidate), combined with the run-status gate. Local grading is a pre-check and the fallback for repeated grader infrastructure failure. Both are reported, and disagreements are listed.
- **When:** grading happens only after a whole block ends.

## Analysis (frozen)
- `research/public/development/analyze_results.py` (family-balanced, draft bootstrap, exploratory) and descriptive tables per arm and card: successes, gains, status counts, tokens, wall time, and transcript audits.
- Secondary: the contrasts 2−1, 3−2 and 4−3, descriptively. No inferential claims.
- The pilot-1 comparison is reported separately as descriptive context.

## Addenda
Any defect found during the run is recorded in a committed addendum before the next block it affects. No rule here changes after the first run except through such an addendum, and no change may be made in response to an arm's outcome unless it applies to all arms and corrects a demonstrable defect.
