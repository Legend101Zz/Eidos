# 0023: Incumbent and challengers until the exploration deadline, baseline identity, fresh-state repair and provider resilience

Status: accepted (2026-11-11, round-3 ticket G1). Revises ADR 0020 (playbook, context policy) and refines ADR 0014
(structured-output repair), ADR 0019 (failed-turn policy) and the single retry owner of ADR 0007/0016.

## Evidence (D2 report: recommendations 1, 5 and 6, §D03, §"Round-2 feature audit")

- **Premature commitment.** Both D03 arm-4 runs selected the best member of one small batch, reproduced it and concluded
  at 595 / 498 of 1800 s, leaving 20–22 minutes and about 7.6 M recorded tokens. They followed the playbook's "stop as
  soon as" and "lock early … keep the selection fixed" (`investigator-playbook/1`). The conclusion reserve was ~179 s.
- **Baseline not pinned.** D03-r1 showed "Baseline: none recorded yet" despite a scored baseline and spent a turn
  re-running it. The context heuristic (`context.py` `_baseline_fact`) needed a `baseline` note or a job with purpose
  `reproduction`; the baseline was neither.
- **Malformed-output waits.** Six of eight refused proposals were the runtime's malformed-output fallback, an empty
  `wait_for_jobs` that the lab refuses when nothing is outstanding. D06-r3 spent a 265 s turn on refusals only.
- **Provider failures.** D03-r2 and D05-r1 recovered three 529s each plus a 60 s turn backoff. Four pilot blocks ended
  when the MiniMax quota ran out (HTTP 429, error 2056), which no retry can recover. The study's provider bridge dropped
  every response header except `Content-Type`, so a provider's `Retry-After` never reached the runtime.

## Decision

### 1. A versioned incumbent and budgeted challenger rounds (`exploration-policy/1`)

- **Selection rule, recorded before the rounds it judges.** `{"metric", "split": "validation"|"training-cv",
  "direction": "higher_is_better"|"lower_is_better", "ceiling"?, "max_rounds"?}`, as a `selection_rule` note or as
  `selection` inside the first `candidate_lock`. It is fixed once recorded: a second rule, or a lock carrying a
  different one, is refused. A held-out split cannot be a selection split.
- **Incumbent.** The latest `candidate_lock` naming a job. `record_note` checks every lock (`handlers/exploration.py`):
  the first is version 1 (`first-lock`); with a rule, a lock naming another job must **beat** the incumbent on the rule
  (strictly; a tie keeps the incumbent), else it is refused. Evidence is read from recorded receipts only: the first job
  in the lock job's lineage (jobs that consumed its outputs, directly or through others; e.g. inference then scoring)
  that reports the metric, else the job's own receipt. A number in the note's text is never evidence. A challenger with
  no recorded metric is refused. Each change is stored on its note (`incumbent`: version, evidence, previous incumbent
  and its evidence, rule, basis) and emitted as `incumbent.changed`. Without any rule a re-lock is still recorded, as
  `unverified-no-selection-rule` (existing manual flows lock without metrics), and the context asks for a rule.
- **Freeze before confirmation.** Once an evidence plan of the investigation is frozen (any status other than draft or
  cancelled) or an assessment was requested and not refused, no lock that changes the incumbent is accepted. Assessor
  feedback therefore can never select.
- **Challenger rounds.** A round is a turn after the first lock that admitted an experiment with a challenger purpose
  (`open-search`, `hypothesis-test`, `measurement-development`). Reproduction work is excluded: jobs that re-run the
  recipe of the baseline or of any incumbent version (seed aside), and their lineage.
- **Allowance.** Exploration time is the wall time left minus the conclusion reserve (ADR 0020) minus a **hold-back**
  for reproduction and export (15 % of the wall limit, between 120 and 900 s); exploration tokens are the model-token
  limit left minus 15 % of the limit. Both are configurable at start (`investigator.exploration`: `patience_rounds`,
  `max_rounds`, `holdback_fraction`, `min_holdback_seconds`, `max_holdback_seconds`, `holdback_token_fraction`), so the
  policy is prospective.
- **Stops.** `selection-frozen`, `ceiling-perfect-score` (the rule's `ceiling`, or the perfect value of a bounded metric
  such as accuracy 1.0 or mse 0.0), `ceiling-no-improvement` (`patience_rounds`, default 3, rounds without a new
  incumbent), `search-limit` (`max_rounds`), `wall-holdback`, `token-holdback`. Phases: `baseline` (no incumbent),
  `explore`, `finalize` (a stop applies).
- **Enforcement.** `begin_turn` computes the state from the whole ledger and records its compact form in the turn spec
  (`spec.exploration`, immutable, like `completion`). In `finalize`, `accept_decision` refuses a decision whose purpose is
  `open-search` or `hypothesis-test` and that starts jobs, with a next action: reproduce (purpose `reproduction`),
  export, assess or conclude. A runtime bounded outcome is never refused for this. Purpose is declared by the model, so
  this is a guard against drift, not a security boundary; the incumbent check in `record_note` is the hard rule.
- **Context and playbook.** `context-policy/3` adds pinned exploration facts (rule, incumbent with its evidence and
  reproduction, rounds, allowance, phase and why exploration stopped). `investigator-playbook/2`
  (`investigator-prompt/3`) replaces "stop as soon as / lock early, keep it fixed" with: baseline, diagnose, search,
  lock with the rule, reproduce, challenger rounds while the facts say "keep improving", finalize when they say
  STOPPED, then assess or submit. `context-policy/2` is unchanged and remains for replay.

### 2. Baseline identity as a durable record

When an investigation job succeeds, the lab records the investigation's **first baseline** (`investigation_baseline`,
keyed by investigation): the registered project's own command with no authored `source` input, or a job run for
reproduction. The first later job that reports metrics and consumed the baseline's outputs (directly or through other
jobs) is recorded as its scorer (`baseline.recorded`, `baseline.scored`). Neither changes afterwards. The view returns
the record at the horizon, and `context-policy/3` shows it ("Baseline (recorded): job …; scored by …") unless an explicit
`baseline` note names another job. The heuristic remains only as a fallback before any record exists.

### 3. Bounded fresh-state repair

When a reply stays malformed after the one schema-guided re-ask (text modes), or after the output retries (tool mode),
the runtime asks again from a **fresh state**: the original request alone, without the malformed replies, with a short
note of the error (`FRESH_STATE_NOTE`), no read tools in tool mode. It is bounded (`fresh_state_repairs`, default 1),
counted against the decision's requests and token envelope, and recorded in `usage.structured_output`
(`fresh_state_repairs`, `fresh_state`: the request and the error that led to it). Only when it also fails does the
decision end in the bounded no-effect outcome `structured-output-invalid` (unchanged, and still refused by the lab
when nothing is outstanding). Tool mode used to raise a retryable failure here, which retried the whole decision.

### 4. Provider resilience, with the existing retry owners

- **Classification** (`runtime.provider_failure`): `quota-exhausted` (MiniMax 2056 / "usage limit", `insufficient_quota`,
  "exceeded your current quota", "credit balance is too low"; never retried), `transient` (timeouts, connection errors,
  429/5xx/529) or `permanent`. `Retry-After` (seconds, HTTP date) or `retry-after-ms` is read from the error or the SDK
  error it wraps.
- **Activity retry (Temporal, `MODEL` policy, unchanged attempts).** A transient failure with a Retry-After sets the next
  attempt's delay (`ApplicationError.next_retry_delay`): Retry-After plus up to 20 % jitter, at most 300 s, and never past
  the start of the conclusion's reserve; the turn's schedule-to-close bound (ADR 0020 addendum R10) still ends the
  activity there as a deadline. Quota exhaustion raises the non-retryable `PROVIDER_QUOTA_EXHAUSTED`.
- **Turn retry (coordinator, ADR 0019).** The failed turn's failure carries the provider class and Retry-After
  (`turn_failure`). The retry backoff (patch `turn-retry-after-v1`) is at least the Retry-After, with deterministic
  workflow jitter of up to 20 %, capped as before, and never past the start of the conclusion's reserve (previously the
  wall deadline). A quota failure (patch `provider-unavailable-v1`) gets no retry turn: the investigation is marked
  needs-input with the new code `provider-unavailable` at once.
- **Accounting.** Every attempt's `record_model_usage` carries its attempt number, elapsed seconds and failure class;
  a failed attempt's usage stays uncertain. The ledger keeps them in the usage detail.
- **Study bridge.** `study/eidos_arms/provider_bridge.py` forwards `Retry-After` and `Retry-After-Ms`.

No new retry loop exists anywhere: Temporal remains the only transport retry owner and the coordinator the only turn
retry owner. Retries never relax a success rule or the independent assessment.

## Consequences

- An investigation keeps searching after its first good candidate while the allowance lasts, but a re-lock needs
  better recorded evidence on a rule fixed beforehand, and the final selection is frozen before any confirmation.
- `record_note` can now refuse a lock; such a refusal is an ordinary refused effect (its receipt is in the next
  context). Manual flows without a rule behave as before, flagged as unverified.
- `begin_turn` reads the investigation's jobs and decisions once per turn to compute the state.
- Context and prompt revisions change (`context-policy/3`, `investigator-prompt/3`); recorded turns of earlier revisions
  replay under their own policies.
- Not addressed here: the baseline error-analysis component (recommendation 2), recipe templates (3) and the external
  MCP workflow (4). The study prompt for arm 4 is not changed by this ADR (the running pilot keeps its prompt).

## Addendum R14: selection is prospective and evidence-bound

Review R14 found seven ways the first implementation let selection depend on what was seen after the fact, or let
exploration spend the reproduction hold-back. Each is closed as follows.

1. **The rule comes before the challengers.** A `candidate_lock` that names a job is refused without a recorded rule
   (a `selection_rule` note, or the first lock's `selection`); a rule first given with a re-lock is refused. A re-lock
   needs recorded selection evidence for **both** the incumbent and the challenger under the rule, and the challenger
   must have been admitted after the rule was recorded. The bases `unverified-no-selection-rule` and
   `incumbent-unscored` no longer exist.
2. **Evidence is bound to prospective provenance.** A rule now names its `scorer` (a component ref; without a release it
   matches any release) and its selection `data` (`inputs` {port: artifact} and/or `config` {key: value}). Only a
   receipt of that scorer, run on exactly that data, reading no charter-attested held-out artifact, is evidence; the
   first such job of the candidate's lineage (else the job itself) supplies it. A rule naming attested held-out data is
   refused, and a lock cannot nominate `scored_by` (a nomination that differs from the rule's evidence is refused).
3. **Reproduced means evaluated.** The rule may declare `reproduce_within` (else the policy's
   `reproduction_tolerance`, default 0.02, fixed when the investigation starts). A later run of the incumbent's recipe
   under another seed reproduces it only when its own selection evidence is no worse than the incumbent's by more than
   that; unscored or short attempts are recorded as such. A lock records the evaluation and is refused if it claims
   `reproduced` without it; the conclusion records the incumbent with `reproduced` true only when it passed.
4. **The hold-back cannot be spent by an open turn.** An exploratory turn's deadline and its activities end at the
   exploration deadline (`completion.exploration_deadline_unix`, the start of the hold-back; `turn_boundary` honours
   it). Between decisions, `accept_decision` recomputes the boundary (wall and model tokens) and refuses new
   challengers once it is passed; `reserve_model_budget` refuses an exploratory reservation larger than the tokens left
   before the hold-back; exploration stops (`token-holdback`) when one decision's envelope no longer fits.
5. **Retries are bounded by the exploration deadline.** Activity and turn retries bound their delay by the exploration
   deadline (never past the conclusion reserve). When the provider's Retry-After cannot fit, the activity stops
   retrying (`retry_stopped`), and the turn retry waits at most until the exploration deadline, so the next turn begins
   in the finalize phase rather than asking an operator.
6. **Patience counts evaluated rounds only.** A challenger round counts as unsuccessful only once every challenger it
   admitted is terminal and, if it succeeded, has selection evidence. Pending rounds may finish: in the finalize phase a
   decision that scores a pending challenger's outputs is still accepted. A succeeded challenger that is never scored under the
   rule stays pending, so patience cannot end exploration on it; the hold-back, the search limit and the turn limit
   still do. Before the first lock (the baseline phase) there is no incumbent to reproduce, so no hold-back applies.
7. **The baseline is explicit.** The baseline is the project command without authored source, or a job a `baseline`
   note designates (recorded at once if it already succeeded). A `reproduction` purpose never makes a job the baseline,
   and `context-policy/3` no longer guesses one.

`exploration-policy/2` identifies these rules in the turn spec.

## Addendum H2 (review R17 findings 1–3): one round history, every challenger purpose, a bounded pre-lock phase

**1. One round history for both paths.** The challenger rounds and the turn of the first lock were built only from
accepted investigator decisions, so a lock recorded by MCP `submit_candidate` had no turn and `run_batch` jobs belonged
to no round: an MCP investigation never reached `max_rounds` or patience. The lab now derives one **shared round
history** from the ledger, in ledger order (`exploration.turn_log`): an investigator turn (the jobs and notes its
accepted decisions recorded); an MCP batch (opened by the `mcp-batch` record `run_batch` writes before any call, holding
the challengers its calls admitted, however many calls or resumptions it takes); a challenger admitted by a direct call
outside a batch (always its own round: reading an earlier round's outputs never hides a new challenger in it); a
`candidate_lock` or `selection_rule` note recorded outside a decision (its own entry, so rounds count from an MCP lock).
Every challenger admitted outside a decision is recorded at admission (`round_admission`: the job, its purpose and its
batch's command key); other jobs outside a decision join the round of what they read, else the entry before them. The domain rules (`rounds`, `state`) are unchanged and see
entries numbered from 1. A call of an MCP batch is checked against the state **as its batch began** (its own jobs not
yet counted), as a decision's effects are checked against the state when it was accepted: one batch is one round, and
`max_rounds`, patience and the ceiling stop the next round on either path. Scoring the batch's own pending challengers
remains admitted.

**2. `measurement-development` is gated.** `REFUSED_WHEN_FINALIZING` is now every challenger purpose
(`open-search`, `hypothesis-test`, `measurement-development`), on the decision path and at admission. The only allowance
is unchanged and narrow: a job that reads an output of a pending challenger (or of what already read one) finishes an
already admitted round (R14 #6).

**3. The hold-back is protected before the first lock.** With no incumbent the state used to return `baseline` before
any hold-back check, so a long baseline, diagnosis or sweep could spend the time and tokens kept for reproduction,
export and the conclusion. The wall and token hold-back now apply in the pre-lock phase too: the exploration deadline
caps a `baseline` turn and its admissions (`EXPLORING_PHASES`), the live check between decisions and the token
reservation check apply to it, and when the allowance is spent the phase is `finalize` with `candidate: "none"`. The
finalization path reports honestly: new exploratory work is refused with a message that no candidate was locked; the
investigator may still lock an already scored candidate (no compute) or conclude stating that no candidate was found
within the allowance. Nothing is invented to submit.
