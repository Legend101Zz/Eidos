# ADR 0049 — Investigator playbook 3, context policy 4 and prompt revision 6, with a claim rule and an evidence packet

Status: Accepted for implementation (step 3)
(`investigator-prompt/5`). Resolves review findings 10 (packet wording), 12, 28 (claim audit). The minimal revision ships in step 3; the packet and `plan` fact in step 6.

## Context

`investigator-playbook/2` orders baseline, diagnosis, one broad typed sweep, incumbent and challenger rounds, reproduction, assessment or submission, conclusion (`src/eidos/investigator/playbook.py`). With the sweep as the only typed search every new
idea became an authoring turn and the lock, readiness and reproduction mechanics were left to the model: the Pilot 3 build averaged 6.6 refused decisions per run (0-1 in later gates) and orchestration cost about 131 s per N04 run (discovery-gap
report 5.1, 4(d)). The finalisation turn is bounded by ADR 0036, 0040 and 0042 and stays as it is. `PROMPT_REVISION` is `investigator-prompt/5` and `PLAYBOOK_ID` is `investigator-playbook/2` at `86dfe57`.

The context is packed by `context-policy/3` into 60,000 characters with fixed pinned budgets (`search` 2,400, `exploration` 1,600); ADR 0041 requires the guide to stay small; the finalisation projection is capped at 8,000 characters. A changed pack output
needs a new policy id so recorded turns replay as they were. Two review findings bear on wording: nothing compared the locked candidate with the baseline before a claim could be made (12), and the packet would have passed an "optimism bound" that is not a bound (10).

## Decision

A revision set is selected by an `investigator` setting (opt-in): `investigator-playbook/3`, `context-policy/4`, `investigator-prompt/6`. Earlier revisions replay unchanged. Each build that goes to live evidence freezes its revisions; later
additions (the `profile` section, the `plan` fact, the packet) bump to `context-policy/5` and `investigator-prompt/7`.

**Playbook 3 (minimal, step 3).** (1) Baseline without authoring, unchanged. (2) One portfolio call (`include: "auto"`); add typed recipes for hypotheses the template lacks; author source only for what the library cannot express, using
`include: "eidos_recipes"`. (3) `prepare_candidate` prepares; read the pinned `candidate` fact and decide whether to lock the prepared candidate with the prepared text; the lock and the selection rule are your act (ADR 0023, 0038). (4) Order hypotheses by
the table's `class_contrasts`; refine what leads; once gains are within the margin use `rescore` instead of more challengers; re-prepare only if the table's choice would clear the lab margin (the lab pre-checks it). (5) Finalise under ADR 0023 and 0042
semantics. **Claim rule:** an improvement claim needs the paired interval against the baseline to exclude 0 in the right direction on the candidate's own validation predictions; otherwise conclude null or abstain. Report the scheme, the number of recipes screened,
the validation score with its SE, the paired interval, and what was not confirmed. Words reserved for assessor evidence ("confirmed") are not used for same-data re-scoring, and the winner's-curse scale is described as an approximation, never a bound. A null or
within-noise result is a result. Playbook items on success rules, assessors, steering and failures are unchanged.

**Tool guide.** The diagnose, sweep and challenger paragraphs become a shorter flow description; `prepare_candidate` and `author_source include` are one line each; the operator vocabulary and portfolio config live in the manifests' `config_schema` and method cards
and are read with `describe_component`. The guide must end smaller than today's.

**`context-policy/4`** adds a pinned `candidate` section (up to 4,000 characters, 8% of context, at most 4,000 per line) carrying the prepared-candidate facts and the prepared lock text from `domain/promotion.suggested_lock`, and extends `_sweep_facts` so the latest portfolio table and diagnoses
are pinned beside it and the 25-job window cannot drop them. **Step 5** adds a `profile` section; **step 6** adds a `plan` fact and a `conclusion-evidence/1` projection inside `_finalization_projection` (8,000-character cap): `class_contrasts` with intervals; the scheme; recipes
screened; the chosen recipe with selection-time CV +/- SE, the rescored CV (labelled "re-scored on resampled train partitions, same data"), the validation score +/- SE (primary), the paired baseline interval, readiness, reproduction, `fit_seconds`, `checkpoint_bytes`; the
winner's-curse scale labelled an approximation; the count of candidates scored on validation; the macro stamp and `basis`; time per phase. It is a ledger projection of values, not a model-authored object, not a new read authority and never lab-written prose claims; the
tables stay readable through the two read rounds because `finalization_evidence.allowlist` already admits job outputs of safe jobs. The conclusion, scope and claim stay the investigator's under ADR 0040.

**Mechanical claim audit** (`study/practice/claim_audit.py`, used from step 4): every number in a conclusion appears in the ledger or the packet; the words "confirmed" and "bound" are absent; every improvement claim carries the paired interval. A violation is reported
per run and is a go/no-go input for steps 5-6.

## Rejected alternatives

- Keeping playbook 2 and adding the portfolio as another tool paragraph: the guide grows against ADR 0041 and the old "diagnose, sweep, challenge" order keeps competing.
- A controller-written conclusion or claim over the evidence: ADR 0040.
- Operator vocabulary in the prompt: it belongs where the lab can validate it.
- A lab refusal of positive dispositions when the paired interval includes 0: a new validator that needs its own ADR; the playbook rule plus the audit come first.

## Verification and limits

Tests: updated guide-order and wording tests (`tests/unit/test_diagnose_sweep_guide.py`, `test_investigator_tool_guide.py`, `test_context_policy_v2.py`); pinned-section budget tests; a guide-size test (smaller than today's); replay of recorded v1 transcripts under the
new policy; a lint that model-facing text contains no practice-family or card identifier (ADR 0043); a fixture-policy end-to-end run through the lab. Live evidence is step 4 (descriptive: authoring seconds, decisions per run, time to first lock, refused decisions including
lock-copy errors, H utilisation, claim audit); the packet and plan are measured again in step 6.

Limits: tests establish structure and budgets, not that a model follows the playbook; live runs use one seed per card; the claim rule is instruction plus audit, not enforcement; no live run has used any of this.

## Review fix round (r2)

Guide 4 and playbook 3 retain the claim-object dispositions and point-estimate conventions, submission without a held-out verdict, manual selection-rule JSON, explicit purpose defaults, pending-round finalization restriction, assessor request shape, charter authority, environment checks, steering and failure handling. They remain general instructions; paired-interval wording is not a new claim validator. The guide remains smaller than guide 3. `study/prompts/tools_arm4.v2.md`, selected by the v2 treatment and embedded in the investigation question, now directs copying both `job_id` and `selection` from prepared lock text and removes the stale classification-scorer-only route. This is part of the model-facing revision set, not an unchanged study prompt.

Portfolio pins contain compact, bounded, complete JSON with chosen validation/SE/fit metrics. On smaller budgets complete fields are omitted instead of clipping tokens. Diagnosis `next` remains first. Candidate lock offers are suppressed during finalization and when that job is already the incumbent. `context-policy/4` is registered in the offline replay table; historical authorized source manifests can be repacked with it without execution. Claim/rule examples are tested against the live validators, guide ordering and size are tested, and the practice-identifier source lint covers both new constants and the study prompt.

## Lock-copy fix (r3)

The pinned candidate section labels the prepared lock text and emits its exact JSON on the next line, with top-level `job_id` and `selection`. Evidence appears separately and contains no nested lock. If the copyable entry cannot fit its budget, the entire entry is omitted instead of truncating the JSON. New `candidate_lock` notes that identify no job are refused with a correction hint; historical nameless notes remain readable. Coordinator tests copy the emitted JSON line verbatim and accept it, refuse the former whole nested candidate object without recording a note, then accept the correction in the same turn. The model-free 60-instance fixture also copies the emitted line.
