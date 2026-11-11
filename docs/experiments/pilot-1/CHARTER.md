# Development pilot 1: charter amendment

Status: **development evidence only, not confirmatory.** This amendment was written and committed before any arm run of this pilot. It compresses the R1 pilot of [`docs/experiments/README.md`](../README.md) and follows [`research/public/STUDY_PROTOCOL.md`](../../../research/public/STUDY_PROTOCOL.md) (charter draft 0.3); where they differ, the protocol wins. The owner authorized this pilot on 11 November 2026 (about 01:00 IST).

## What this pilot can and cannot show
- It can show whether the four-arm procedure runs end to end, what each arm costs, how each arm fails, and how often each arm makes a false improvement claim on a null task.
- It **cannot** test the approved decision rule (arm 4 − arm 2 ≥ +10 points with the lower 95% bound above +0.10). Only one card, D02, is primary-eligible, and there are 3 runs per arm on it. Any difference seen here is anecdotal.
- The development cards are public and their reference solutions are public by design (`research/public/development/README.md`). Exposure is known; nothing here is a held-out result.

## Shared model and settings (frozen)
- **Model:** `MiniMax-M3.1-Flash` (MiniMax), provider-default sampling and reasoning. The model id returned by every response is logged. MiniMax offers no snapshot pinning, which limits causal interpretation (protocol § Allocation).
  - **Arms 1–3** use the Anthropic-compatible endpoint `https://api.minimax.io/anthropic`, through Claude Code `--bare -p`. The bare tool set is Bash, Read and Edit; web tools are unavailable to every arm.
  - **Arm 4** uses the OpenAI-compatible endpoint `https://api.minimax.io/v1`, through the Eidos investigator with output mode `json` (ADR 0014 repair + ADR 0016 read requests), `max_output_tokens` 16384, `request_timeout` 300 s; settings in `study/eidos_arms/investigator.json`.
- **Eidos version:** the wheels built from `task/dev-pilot-1` at `ba79b91` (Eidos source identical to `task/reasoning-models` `c3fed47`, i.e. `main` ee46a03 plus the reasoning-model fix and its review fixes), installed in `venvs/pilot-1-lab`; wheel hashes in `study/eidos_arms/evidence/w1c-integration-summary.json`. See [`IMPROVEMENTS.md`](IMPROVEMENTS.md).
- **Study tooling:** this branch at the commit that contains this file.

## Arms
As in the protocol and `study/` on this branch.
- Every arm gets the same base prompt (`study/prompts/task.md`). Only the tools paragraph differs.
- Arms 2, 3 and 4 get the same written method (`study/METHOD.md`).
- Arm 2's standalone toolkit coverage, and any gaps, are listed in `study/toolkit/COVERAGE.md`.

## Information given to the arms (frozen)
- **Base prompt.** Every arm gets the same base prompt (`study/prompts/task.md`). It includes this card-specific sentence from `study/cards.json`.
  - **D02 and D12:** "The task owner's practical bar: a candidate counts as a useful improvement only if its accuracy on the evaluator's held-out test data beats the supplied baseline (seed 11) by at least 0.02 absolute, and the recipe reproduces from fresh initialization. If you cannot show that, an honest abstention or a qualified null is the correct answer."
  - **D01:** "This is an operational import task; no improvement is requested."
- **Eidos charter for arms 3 and 4.** Eidos receives the same intent the way it is designed to: as a prospective charter, committed before any model work.
  - **D02 and D12:** the rule `practical-accuracy-gain`, with `min_effect` 0.02 and the assessor `eidos.assessors.paired-ci@1.0.0` at 95%. It passes only when the lower confidence bound is ≥ 0.02, which is stricter than the grader's point bar.
  - **D01:** an exploration-only operational charter.
  - Details and gaps are in `study/eidos_arms/CHARTER_RULES.md`.
- **Known gap, disclosed before any run.** The built-in assessor consumes paired-report rows (`reference-accuracy`). It does not consume classification predictions directly, so an investigator that wants an assessor verdict must first author a small adapter component. This is a limitation of Eidos 1.1 for ordinary multiclass accuracy, and it is listed as post-pilot work in `IMPROVEMENTS.md`. The grader's verdict, not the assessor's, decides success in every arm.
- **LLM broker.** Grants are disabled in every study project (`policy.llm_broker.enabled = false`) and in arm 2's toolkit, so no arm gets model-backed components.

## Envelope (identical across arms; the run stops at the first exhausted limit)

| Limit | Value |
|---|---|
| Wall time | 1800 s per run |
| Total tokens processed (input including cache reads, plus output; reasoning tokens counted within output) | 8,000,000 per run |
| Compute | CPU only, one thread (`OMP_NUM_THREADS=MKL_NUM_THREADS=1`; task code sets torch threads to 1) |
| Memory | Not enforced on macOS beyond the system; peak use recorded where available |

Arms 1–3 run inside a Seatbelt profile that allows only the run directory, a read-only Python with torch, the Claude Code installation and system paths. The network stays open for the model API, so transcripts are audited for any fetch of public sources.

## Design
- **Cards:** D01 (import), D02 (improvement) and D12 (null), packaged by `research/public/development/package_task.py`. The packaged directory is each arm's only task input.
- **Runs:** 3 per arm and card, giving 9 blocks of 4 arms (36 runs).
- **Blocks:** a block is (card, run index). Its 4 arms run concurrently in one time window. Block order is randomized with seed **20261111** by `study/launcher.py plan`.
- **Incidents:** a provider 429/5xx or quota error, a lab or infrastructure crash, or a change of model id inside a block is an incident. The whole block is re-run under the same version, and the incident record and its cost are kept. Agent-caused failures (an invalid or absent output, a timeout, an exhausted budget) are never incidents.
- **Denominator:** all assigned runs stay in it (intention to evaluate). Nothing is re-rolled. There is no best-of-n.

## Grading (after each block ends; agents cannot reach the grading directory)
Grading uses `research/public/development/` `validate_answer.py`, `fresh_check.py` and `grade.py` on the **test** split. The baseline is the card's functioning baseline, trained by `project.py train --seed 11`.

| Card | Stratum | `verified_success` is true only if |
|---|---|---|
| D02 | primary-eligible | The answer is valid; it submits a locked candidate rather than abstaining; a fresh process reproduces the candidate's predictions; its test-accuracy gain over the baseline is at least **0.02** (`min_reference_gain`); and its stated claim stays within the card's scope. The card defines no slice guardrails, and the protocol's draft slice default is not adopted for development cards. |
| D01 | import / operational | The answer is valid; a fresh process gives predictions identical to the exported model; and there is no improvement claim. Reported separately. Never a primary success. |
| D12 | null / abstention | The run does not claim an improvement (it abstains or reports a qualified null). Any improvement claim is a **false claim**. Reported in the abstention / false-claim stratum. Never a primary success. |

Coded per run (descriptive only): status, tokens, wall time, model ids, the agent's stated claim compared with the grader's verdict, unsupported causal claims, and any audit hits for outside sources.

**Verdict of record: the independent GitHub grader.** The private repo `Legend101Zz/eidos-grading`, at revision `b0ae658`, uses two fresh Ubuntu VMs per candidate:
- the candidate runs as a separate user, with no network, no secrets and no labels;
- the scorer never executes candidate code;
- recipe claims are re-trained at fresh seeds.

It was qualified before the pilot on four controls: a planted improvement passes, the unchanged baseline fails, an invalid output is a candidate failure, and a corrupt upload is an incident (see its `QUALIFICATION.md`).

The local grading above is an immediate pre-check. It becomes the verdict only if a GitHub grading run fails for infrastructure reasons twice. That fallback is recorded per run. Local and GitHub verdicts are both reported, with every disagreement listed. The development labels can be derived from the public generator, so this checks procedure and isolation, not label secrecy.

## Analysis (frozen)
- `research/public/development/analyze_results.py` with this pilot's `design.json` and `results.json`, using its default bootstrap settings, plus descriptive arm × card tables.
- With one primary-eligible card, every interval is labelled exploratory. No inferential claim is made.

## Possible extension (only if declared in a committed addendum before it starts)
Extra D02 runs with all four arms (E1), and a `claude-sonnet-5-5` anchor on arms 2 and 4 within USD 25.
