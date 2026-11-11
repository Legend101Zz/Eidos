# 0020: Investigator reliability and completion

Status: accepted (2026-11-11, round-2 improvement E1). Extends ADR 0007 and ADR 0019 (bounded retries, no silent
stalls) and the context policy of the design. The numbers come from the diagnosis of development pilot 1
(`tickets/D1-REPORT.md`, §1, §3 and recommendations 1, 2, 7, 8), which are expected effects to test in the next
pilot, not measured gains from this change.

## Evidence

- Pilot D02-r2: 18 of 22 failed jobs were `ModuleNotFoundError: numpy` in environment `lab`, which was registered
  without its packages. D12-r2 made seven such baseline attempts. The investigator kept asserting that `default` had
  failed, because the job views did not show the environment.
- Job failure context kept the first 1200 characters of the message and dropped the exception type and any stderr.
- Run r2's context reached the 60,000-character limit by turns 17 to 18, and baseline facts fell out of the 25-job
  window, so baseline work recurred.
- r2 had a 0.9833-validation candidate but ran out of wall time repeating work; r3 under-claimed. Nothing told the
  investigator how much time was left or reserved time for its conclusion.

## Decision 1: capability metadata and admission preflight

`register_environment` records `capabilities` (`environment-capabilities/1`, replaced by `/2` in addendum R10 below): the interpreter digest and, for each
key module (numpy, torch, sklearn, joblib, scipy) and each declared requirement, whether it imports and its version.
It also records the project's declared `requirements`.

The probe (`eidos.runtime.env_probe`) runs in the process that registers the environment, on the host that holds the
interpreter, in the job allowlist environment with `-I`. It is not run by the API handler: the application layer may
not depend on the runtime, and the handler must not execute a caller-supplied path. CLI registration
(`project register-env`, the lab environment), task-bundle import and the pilot's study setup all use it.

Admission (`resolve_lock`, so every run, trial, branch and evidence arm) refuses, before the job row exists, a job
whose component `dependencies` or project requirements the environment measurably lacks
(`ENVIRONMENT_PREFLIGHT_REFUSED`, HTTP 422) with a message that names the environment and packages. Refusals are
recorded twice: the investigator's decision receipt, and an `admission.refused` event written after the command's
transaction rolls back, so they survive the rollback and appear in later contexts.

Refusal needs positive evidence. An environment whose probe failed or never ran is reported as unprobed and is never
refused. Only the key modules of a lock file are checked, and without their versions: a lock file pins transitive
packages exactly, and another patch release is not an incompatibility. A `requirements.txt` is checked in full.
Components that run the project's code (`project-command/1`) also carry the project's declared requirements; other
components do not, so assessor infrastructure is unaffected. `eidos-*` packages are provided by the launcher.

`eidos.task.score-classification` and `eidos.task.score-regression` now declare numpy (releases 1.0.2).
`eidos.task.paired-classification-report` still does not: its release is pinned by the template, and a failure there
is reported through the actionable-failure path until the template release is next revised.

Limits: capabilities are a snapshot taken at registration. If an interpreter changes afterwards, the environment must
be registered again.

## Decision 2: failures the investigator can act on

Job failure context now includes the exception type (the innermost one a traceback or message reports, because a
project subprocess's error is wrapped by the component) and a bounded stderr tail. The runner stores up to 3000
characters of the process's stderr with a failed job; the context shows the last 800, passed through the same
secret-shaped redaction as the rest of the policy.

Repeated failures are grouped across the whole run by component, environment, exception and detail, and shown as
"X failed 3× in environment lab with ModuleNotFoundError numpy". Preflight refusals are grouped the same way.

## Decision 3: context policy 2, a pinned facts block

`context-policy/2` has a pinned block that survives truncation and the 25-job window: the baseline job and metric, the
current lock, the charter's success rule, environments and capabilities, known failures and the wall time. Sections
are then filled in order of value (recent jobs, plans, results, created artifacts, claims, approved protocols, catalog)
until the budget is spent, so catalog boilerplate goes first. Everything left out is listed in `omissions`.

`context-policy/1` stays available for replay and for investigations started before this change; a turn records and
uses the policy in its spec. The baseline comes from a note of kind `baseline` when one exists, otherwise the first
successful reproduction job. The lock comes from the latest note of kind `candidate_lock` or `lock`.

## Decision 4: versioned playbook

`investigator-playbook/1` is part of the system prompt (now `investigator-prompt/2`, recorded with each decision):
baseline without authoring source, quick wins, early lock, fresh-seed reproduction, assessment or submission,
deadline-aware stopping, and null handling. It has no task-specific remedies and does not weaken a success rule.

## Decision 5: deadline-aware completion

An investigation may set `investigator.wall_seconds` and `investigator.conclude_threshold_seconds` (default 10% of
the wall, within 60 to 600 s). The wall deadline is fixed at start, on the lab's clock, and recorded in the
investigation body and each turn spec (`completion`).

- Remaining wall time is in the pinned facts every turn.
- Once the reserve begins, the context says CONCLUDE NOW, the coordinator stops waiting and starts the next turn at
  once, a decision that neither concludes nor stops is refused (a runtime's bounded no-effect outcome is exempt), and
  no new compute is admitted (`CONCLUDE_REQUIRED`). The independent assessment of a frozen plan is exempt.
- Earlier turns stop where the reserve begins, and the concluding turn may use what is left, so the conclusion keeps
  its time. A job's `wall_seconds` is reduced to fit before the reserve; the lock records what it ran with.
- A failed-turn backoff and the decide activity's retries are bounded by the remaining wall time. At the deadline
  `begin_turn` refuses (`wall-deadline-reached`) and the coordinator surfaces that as needs-input.

Scope: without `wall_seconds` nothing changes. Infrastructure retries of a job (lost workers) are not bounded by the
wall; the outer stop at the deadline covers those.

## Consequences

Assessment rules, evidence obligations, freezing and the independent assessor are untouched. A preflight refusal
cannot admit anything, and a wall-time refusal only removes work; neither can turn a failed result into a passed one.

## Addendum R10: review of the first implementation

Review R10 found eight problems; each is corrected here and has a test.

1. **The study passes the wall deadline.** `study/eidos_arms/arm4.py` sets `investigator.wall_seconds` from the watchdog's
   own deadline, measured right before `start_investigation` and 5 s short of it, so the lab's deadline is never the later
   of the two.
2. **The probe runs nothing from the environment** (schema `environment-capabilities/2`). It starts the interpreter with
   `-I -S`, so no `.pth` import line or `sitecustomize` runs, computes the site directories itself and reads only
   `importlib.metadata`. It has its own process group, is killed as a group at the timeout, and writes to files, so a
   descendant cannot hold it open. Consequently it no longer measures whether a module imports, or GPU availability
   (`devices` is `{"cpu": true}`; `eidos_pytorch.devices` reports devices inside a job). A distribution that is installed but
   broken is found when a job runs it and appears in the known failures.
3. **Import names come from distribution metadata** (`top_level.txt`, else `RECORD`). The inventory decides whether a
   distribution is installed; a distribution whose import names cannot be read is `provided: null`, shown as `unknown`. A
   distribution absent from the inventory is refused only if nothing suggests another distribution provides it and no
   installed distribution has an unknown mapping; otherwise the requirement is inconclusive.
4. **Requirements are parsed with `packaging.requirements`.** Markers are evaluated for the probed interpreter (its marker
   environment is recorded), compound constraints use the specifier set, and extras use the distribution's recorded
   `Requires-Dist`. Includes inside the project or source bundle are followed. URLs, unresolved includes, `-e`, constraints
   files and unparsable lines are inconclusive: never refused and never dropped. `assess` returns them, and they are recorded
   in the capability record and in the job's lock (`preflight_inconclusive`, only when non-empty, so other locks keep
   their digests).
5. **Redaction before truncation, stored and shown** (`eidos.domain.redaction`). Quoted and escaped JSON keys, Python
   dictionaries and credential-shaped strings are covered. The runner redacts a log before it cuts its tail and starts its
   scan at a line boundary; the launcher handler redacts every failure the ledger stores, whichever path reported it; the
   context and the `get_job` read tool redact what the model sees, including records stored before this change.
   Residual: `eidos.project.command` cuts its child's stderr to 1500 characters inside its own message; a secret split by
   exactly that cut could lose its name. It is not changed here because that would release a new component version.
6. **Every pinned section has a budget** (`PINNED_BUDGETS`: an absolute amount, a share of the limit, and a per-entry cap).
   Cuts and dropped entries are recorded in `omissions`. Refusal groups are capped like failure groups. If the pinned part
   still does not fit a small limit, the question shrinks, then the least valuable sections go whole, and a final guard
   keeps the text within the limit, with the tool guide when it fits.
7. **A turn ends at its boundary** (`turn_boundary`: where the reserve begins, or for the concluding turn the deadline).
   `build_context`, every `decide` and every `execute_decision` carry a schedule-to-close timeout to that instant, so
   retries are inside it, and no new slot starts past it. A boundary stop is the turn's `deadline` outcome, not a failed turn:
   the coordinator starts a fresh turn there, whose completion state is computed anew and says conclude now.
8. **The registration key digests the whole payload**, so a re-probe with a different interpreter, inventory, capabilities
   or declaration registers a new record, and an unchanged one replays.

The review's principle is unchanged: admission refuses only on positive evidence, and nothing here weakens assessment.

## Integration with ADR 0021 and ADR 0022 (E4)

`context-policy/2` carries three more budgeted pinned sections: where the candidate stands (`assessment`: state, plan
confirmation, `assess_candidate` requests, and the route the charter leaves), recipe sweeps (`search`) and batches
(`batches`, including calls that did not run). The body also lists `assessment_requests`, which only `context-policy/1`
rendered. A waiting batch (`_drive_batch`) stops at the reserve's start like the rest of the turn: its calls that cannot
start are recorded `BATCH_DEADLINE`, and every slice is bounded. `investigator-playbook/1` and `investigator-prompt/2` now
describe the one integrated workflow; neither id was released with the earlier text, so they are not bumped.
