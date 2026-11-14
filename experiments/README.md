# Experiment record

This area is the public development record for experiments on Eidos.
Each attempt owns a directory, a registered plan and a separate hash chain.
Kept, reverted and abandoned attempts all remain visible.
The main branch owns `INDEX.jsonl`, appended once when an attempt closes.
Separate chains let two experiments run on separate branches without a shared log fork.
Use a distinct GitHub issue for each attempt; `new --issue N` is required.
The id is the issue number padded to at least three digits (issue 123 → EXP-123).
Parallel branches use separate issues, so their ids do not collide.

## The loop

1. Observe a concrete failure. Collect sources and record which pools were exposed.
2. Register a hypothesis, evaluation plan and keep/revert rule. Commit and push
   the registration before changing code. The ledger prints the Git commands.
3. Build the smallest change that tests the mechanism, behind a feature flag.
4. Evaluate cheapest first. Stop when a pre-declared falsifier or guardrail fires.
5. Decide by script using the registered rule and scorecards. Record its output;
   the ledger stores evidence and does not itself compute or adjudicate statistics.
6. Obtain independent code review and, except for infrastructure, result review.
   Reviews identify the exact commit reviewed and preserve the review file digest.
7. Merge only after CI and review pass, or revert/abandon and preserve the learning.
   Close the record and append its index entry in the main-branch merge sequence.

## Evaluation tiers

T0 is tests and CI, including a golden check of the default treatment.
T1 is model-free evaluation: a scripted "autopilot" investigator on real cards,
generated practice cards, and replays. It is the cheapest mechanism check.
T2 is a small paired live comparison on four cards, used as a smoke test.
T3 is the paired live comparison on eight cards with two runs per card.
One extension may be pre-declared; it is never chosen after looking at results.
Pair incumbent and candidate runs and declare the pool, cards, replicates and provider.
Declare stop rules, budgets and any second provider in the plan.
Keep search and confirmation separate, and record every exposure.

## Scorecard and guardrails

The primary measure is verified reference-normalised gain (VRG).
Secondary measures are verified-success rate, measured gain (MRG), error ratio
against the reference, uplift over autopilot, and novelty that wins.
A mechanism metric explains why the primary should move; name it before building.
Scorecards are evidence inputs. Their aggregate blocks are copied without recomputation.
Use exact paired sign-flip tests where a rule calls for a sign-flip p-value.

Guardrails are vetoes, not compensating terms in a weighted score.
Require zero false claims and zero held-out identifier matches.
Reject a new failure class, and keep cost and time within the declared plan.
Cost above 1.5 times incumbent requires a pre-declared exception.
All five rules inherit `rules/VETOES.json`.

## Default rules

- `R-infra`: tests and CI green, default treatment unchanged in a golden test,
  and independent code review ACCEPT.
- `R-mech`: evaluation-rotation autopilot mean delta VRG at least 0.03 with exact
  paired sign-flip p at most 0.10. No card loses more than one reference SE unless
  named in the plan. Practice generators show no harm. T2 smoke has no guardrail
  breach and mean delta VRG at least -0.25.
- `R-behav`: T2 clean; T3 mean delta VRG reaches the declared delta (default 0.15),
  with sign-flip p at most 0.10, or at most 0.06 at each look with an extension.
- `R-disc`: as R-behav on MRG (default delta 0.08), with delta VRG at least -0.10
  and verified-success rate no lower by more than one run.
- `R-enable`: mean delta VRG at least -0.10, or autopilot delta VRG at least -0.01
  when model-free evaluation suffices, plus the declared target improvement.

Rule JSON uses metric/stat/op/value/tier clauses and named boolean checks.
`when` selects conditional clauses; every selected clause must pass.
`override` names a pre-registered choice override; `plan` values refer to plan fields.
`each_look` requires the threshold at every declared look, not just the last.
`except_plan_named_cards` and `unless` permit only exceptions declared before building.
The `target` metric refers to the target named in the plan, including its direction.
Additional target fields or a model-free sufficiency declaration may be added to the plan.
Freeze rule choice, version, overrides and extension policy at registration.

## Flags and treatments

See [the experiment runtime guide](../study/exp/README.md) for flags and treatments.
The default treatment must remain unchanged unless a registered change says otherwise.
Infrastructure can use T0 evidence; behavioral claims require the appropriate live tiers.
Everything here is development evidence. Only a registered pilot on sealed cards
confirms anything. Development evidence must never be presented as confirmation.

## Ledger commands

Run from the repository root with the study Python interpreter.
Create an attempt, then fill every hypothesis section and the plan/rule choices:

```sh
python -m study.exp.ledger new --title 'Test a mechanism' --slug mechanism --kind performance --issue 123
python -m study.exp.ledger register experiments/EXP-123-mechanism --trunk origin/main
```

Registration refuses unfilled hypotheses, invalid plans/rules and any code changes
outside this area in the branch or working tree. Commit and push the registration.
Nothing outside `experiments/` may be touched by any non-merge commit before the
registration commit, including code committed and then removed, or documentation.
Before registration, only two-parent trunk merges are allowed: the second parent
must be an ancestor of the named trunk. Other merges, including octopus merges,
are refused; merge resolutions must not introduce changes outside this area.
The base is the merge-base of HEAD and the named trunk (default origin/main), which must exist.
The three registered files, rule definition and vetoes are frozen; record later findings as events or decisions.
`verify --against` also checks that the first committed registration preceded code.
Use `python -m study.exp.ledger rule DIR` to obtain the effective registered rule;
live definition changes produce a warning for open experiments and cannot change their rule.
See [the precise clause grammar](rules/README.md) for fields, checks and fail-closed evaluation.

```sh
python -m study.exp.ledger record experiments/EXP-123-mechanism --tier T1 --scorecard scorecard.json --cost '{"usd":0}'
python -m study.exp.ledger review experiments/EXP-123-mechanism --kind code --verdict ACCEPT --commit HEAD_SHA --file REVIEW.md
python -m study.exp.ledger review experiments/EXP-123-mechanism --kind result --verdict ACCEPT --commit HEAD_SHA --file RESULT_REVIEW.md
python -m study.exp.ledger decide experiments/EXP-123-mechanism --decision keep --decision-file experiments/EXP-123-mechanism/DECISION.md
python -m study.exp.ledger event experiments/EXP-123-mechanism --type exposure --data '{"pool":"practice"}'
python -m study.exp.ledger close experiments/EXP-123-mechanism --merged-commit MERGED_SHA --issue 123
python -m study.exp.ledger verify --against origin/main
python -m study.exp.ledger status
```

`record` accepts repeated `--scorecard` arguments. It copies scorecards into `results/<tier>/`, hashes them and copies aggregates.
Reviews are copied into `reviews/`; all ledger paths are repository-relative.
At append time review commits must exist and be HEAD or an ancestor of HEAD.
Later verification checks commit existence and re-hashes copied evidence.
Evidence text is scanned before copying for absolute local paths and restricted
source names. Redact the source yourself, or explicitly use `--allow-paths` on
`record`, `review` or `decide`; the entry records that flag and copies verbatim.
Use `incident`, `exposure`, `budget` or `note` events for other evidence.
Keep requires registration, a result and ACCEPT reviews; infra needs only code review.
A later FIX-FIRST or REJECT supersedes an earlier ACCEPT of the same review kind.
Use `revert`, `extend` or `abandon` to record other outcomes, with a decision note.
An extension follows the registered policy; it does not rewrite the plan.
`close` needs a final keep, revert or abandon decision; extend keeps the experiment open.
It rechecks the latest required reviews for keep and binds the latest decision digest.
It refuses duplicate closure and records `none` for an unmerged attempt.
The issue must match the plan. Keep requires an existing merged commit SHA;
revert and abandon require `--merged-commit none`.
Close runs only on the trunk’s local branch (`main` by default); the index is appended at merge time only.
The hidden `--allow-branch` bypass exists solely for disposable test repositories.

After experiment CI and review pass, merge the experiment branch into local `main`,
run `close` there, and commit the chain and index append. Push that commit to a
`task/merge-…` branch for CI, then fast-forward `main` to the passing commit.
For reverted/abandoned attempts, merge the public record with the code reverted.
Merge with a merge commit or fast-forward only: no squash and no rebase of an
experiment branch after registration. Do not amend its published history.
No force-push: CI fails if the previous push is missing or is not an ancestor
while the branch carries a registered experiment directory absent from origin/main.
Serialize these merges so each index append starts with the preceding main index.
Each chain and the index use canonical JSON, SHA-256 links and one-based sequence numbers.
The actor is the configured Git user name; UTC comes from the system clock. Actor and time are self-reported;
verification checks timestamp syntax/order, not real-world identity or clock accuracy.
`verify` checks current files and links, including the registered file hashes.
`--against` checks HEAD and the working tree against the merge-base with REF for the index and chains as byte-for-byte prefixes,
and preserves the reference experiment's three registered files.
CI runs pytest ledger tests, verification against origin/main and the previous push,
and per-commit Kernel-Change/Measurement-Change body trailer guards.
Every guarded non-merge commit needs its own nonempty matching trailer in the body,
separated from the subject by a blank line.
Branch protection is an external precondition: require `merge-gate`, disable
force-push on main and experiment branches, and allow merge commits or fast-forwards
only. These settings are managed outside this repository. The aggregate
gate runs on pushes; fork PRs rely on their individual job checks.

The [experiment runner](../study/exp/README.md#one-command-development-runs) turns a
treatment/build/provider and a pool into independently graded scorecards with
`python -m study.exp.run`; `--pair` interleaves variants and writes a descriptive
paired summary. Start with `bash study/exp/smoke.sh`, inspect plans with `--dry-run`,
and provide budget/quota evidence for live runs. `python -m study.exp.heldout`
performs a limited blind H consultation and exports only a coarse receipt.
