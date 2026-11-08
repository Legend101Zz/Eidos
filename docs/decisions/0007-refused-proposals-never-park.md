# 0007: Refused proposals never park an investigation

Status: accepted (2026-11-08).

**Context.**
- `64ad288` started refusing `wait_for_jobs` when nothing is outstanding. On CI (`8301799`), the switch journey's scripted investigation then ended turn 5 on the invalid-proposal limit. Nothing woke it afterwards, and it idled until the journey timed out.
- A refused proposal ending a turn was itself a parking path. It was in `WAIT_REASONS` and woke only on news or the 6 h max-idle timer.
- A second contributor: `investigation_view` derived job visibility from the first 5,000 events from seq 0 (the investigation's events plus all project-level ones). In a long project, finished jobs could therefore look "running-or-queued" indefinitely to a policy that decides from the view.
- **Root cause, reproduced locally at `08f9e01`:** the scripted policy's "publish the validated diagnostic" decision combines a `publish_component` call with `wait_for_jobs`. A publish creates no job, so the rule refused the **whole decision**. That dropped the publish, so every later turn proposed the same thing. The local run hit it on turns 5–7, and with the retry mechanism it surfaced as "needs operator input".
- **Fix:** only a **pure** wait (no calls) with nothing outstanding is refused. A decision with calls is accepted. Its effects' events wake the loop, because `claim.filed`, `model.exported`, `component.retired` and `evidence_plan.*` were added to the idle-poll news types; `component.published` and job events already were.

**Decision.**
1. **Bounded immediate retry.** A turn that ends `invalid-proposals` gets up to 2 immediate retry turns, each with a freshly built context. The refusals are ledger records, and the view is rebuilt at a new horizon. Each retry is an ordinary turn: accounted, bounded by turn and model budgets, and counted toward `turns_per_run`.
2. **Then surface.** After 2 retries the coordinator stops investigating on its own. Its status becomes "needs operator input". Job/evidence wakes and quiet polls do not start turns; only operator news (steer, resume, charter changes, stop) does. There is no max-idle auto-turn.
3. **Versioned.** The change is guarded by `workflow.patched("invalid-turn-retry-v1")`, and recorded histories replay unchanged.
4. **View fix.** `investigation_view` takes job terminal visibility from typed `job.*` terminal events up to the horizon, with no window. The record cutoff comes from `event_time(horizon)`.
5. **Implicit waits.** A decision with no calls and no control also ends a turn "waiting", so the nothing-to-wait-for rule applies to it too. Decisions with calls are never refused by this rule.

**Alternatives rejected.**
- Exempting the refusal, i.e. accepting the wait as before: this keeps the original idle trap.
- Unbounded retry: a model that keeps proposing the same refused action would spend budget in a loop.
- Stopping the investigation outright: this loses the operator's chance to steer. `request_input` semantics are the design's way to surface.

**Tests.**
- `test_workflows.py::test_refused_proposal_turns_retry_boundedly_then_surface_for_input`
- `test_turn_recovery.py::test_wait_refused_when_launched_jobs_finished_before_the_decision_and_next_view_sees_them`
- `test_turn_recovery.py::test_wait_for_jobs_with_nothing_outstanding_is_refused_within_the_turn`
- The switch journey, run locally twice.
