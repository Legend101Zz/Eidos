# ADR 0054 — Investigator extension seams and model-free autopilot

Status: Accepted. R-infra: existing defaults remain unchanged.

## Decision

Runtime factories register through `register_runtime(name)` in `investigator.runtime`.
The fixture and lazily imported Pydantic AI factories remain the built-ins; fixture
remains the runtime builder's default. Additional scripted policy modules must be
under `eidos.investigator.`; importing a policy executes code.

`investigator.prompt_packs` owns packs 5 and 6: system text, playbook text/id and tool
guide. `settings.prompt_pack` explicitly selects a pack; without it, the historical
exploration-policy/3 switch still selects prompt 6, otherwise prompt 5. An explicit
pack also selects the ordinary context tool guide; the bounded finalization guide
retains precedence. SHA-256 goldens were computed on the original UTF-8 strings
before refactoring, independently for system, playbook and guide. No bytes change.

New investigations validate context policies 1–4, exploration policies 2–3 and
known prompt-pack identities. The pure identity registry lives in the domain to
preserve the application/adapter import boundary. Pack extensions register their
identity through `register_prompt_pack`. Historical context packing and replay
remain permissive and retain their old revision behavior.

`autopilot-v2` is an explicitly selected fixture policy requiring context policy 4
and exploration policy 3. It runs the unedited supplied project baseline, then one
default-budget auto portfolio and a same-decision preparation macro. Once the jobs
finish it reads the structured candidate projection and chooses a separate lock
only when readiness, second-seed reproduction and the paired positive-gain interval
support it. If the structured task exposes a numeric `practical_bar`, the gain must
also clear it. Current study cards generally expose their bar in prose; the policy
does not parse that prose or infer an unavailable threshold.

The lock note serializes exactly the lab-prepared lock object, including its
selection rule. A successful current-turn note receipt can establish the lock
before the immutable view horizon advances. A supported lock leads to disposition
`candidate`, with the prepared point estimate and structured measurement; arm 4
exports `improvement_claimed`, pending independent evaluation. Without support it
concludes null. Refusals, failed jobs, missing facts, incompatible policies and
conclude-only requests yield a scoped null or abstention. Each step is attempted
once; unfinished work has one additional wait decision. There is no authoring,
model, assessor or hidden-data access. All results remain labelled fixture.

The treatment uses exactly the v2 portfolio flags and the nested investigator
fixture override. Its policy, tests and smoke script belong to the v2 slice removal
inventory; absent optional built-in policy modules remain skippable after removal. Resolution accepts this explicit section form as well as the
historical direct-investigator override form, preserving existing receipts.
Fixture study setup/run records state `provider: "none (fixture runtime)"` and name
the policy. They start with empty credential stdin and skip the provider bridge.

Pipeline C merges `--config-overrides` with RFC 7396 semantics before freezing and
propagates the result to spawned workers. Its candidate digest covers that config.
Every stage accepts `--evaluation-id`, retaining historical defaults when omitted.
The holdout admission order and burn ledger are unchanged.

Shared lab startup distinguishes API health from a live supervisor. With unchanged
provider/credential mode, a live but unhealthy supervisor gets three bounded
health retries, then an error without stopping jobs. Provider/credential changes
still require confirmed stop/start; the start receipt must name exactly the
requested credential environment variable.

## Verification and limits

Unit contracts cover runtime extension, policy namespace restrictions, both prompt
byte sets, selection/validation and each autopilot branch. Fake CLI tests cover the
shared-supervisor race and exact credential-name matching. The treatment default
goldens remain in place.

The integration test drives real PostgreSQL activities, published component bytes,
the real runner, investigator lock/conclusion, arm-4 export and artifact validation.
It fabricates classification/regression signal and noise data with numpy, checks
fresh-process locked inference and two fresh training seeds, then calls the same
pure `study.grade_block.success_rule` used for public development grading. It does
not replace or modify grading rules. It exercises the lab-level alternative, not
the full installed coordinator path; the build host has no Temporal CLI.

`study/exp/smoke_autopilot.sh` is an opt-in full installed-path smoke against a
fresh isolated lab home and a previously prepared public arm-4 run directory with
autopilot treatment receipts. It grades through `grade_run` and stops its own lab.
It is not automatically run against a shared frozen lab.

These are mechanical fixture checks, not scientific generalization evidence or
model-uplift measurements. The build report records exact commands, outcomes and
any execution incidents.
