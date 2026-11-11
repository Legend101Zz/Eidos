"""Versioned investigator playbook ``investigator-playbook/1`` (ADR 0020): how to spend a bounded investigation.

Part of the system prompt, so a change is a new prompt revision and is recorded with every decision. General
guidance only: it says nothing about any particular task, and it does not relax a success rule or an assessor.
"""

from __future__ import annotations

PLAYBOOK_ID = "investigator-playbook/1"

PLAYBOOK = f"""Playbook ({PLAYBOOK_ID}). Work in this order and stop as soon as the recorded evidence settles the question:
1. Baseline first, without authoring source: when the task names a project command, run eidos.project.command with no source input; it copies the registered project into a private working directory, so the baseline needs no author_source. Check the environment's capabilities first and pick one that has what the project imports.
2. Search next, in batches: cheap quick wins, or eidos.search.recipe-sweep over a parameter space on visible validation (bounded by max_trials or max_seconds). Chain authoring, training, inference and scoring in one decision with {{"$ref": "call[i].field"}} references instead of spending a turn on each step. Jobs run one at a time unless a resource policy allows more. Sweep results are exploratory: they make no claim.
3. Lock early: once a candidate beats the baseline on evidence you may look at, record it (record_note with kind "candidate_lock", or the kind your task asks for) and keep the selection fixed. Do not keep relocking.
4. Reproduce the locked candidate with a fresh seed, and the baseline under the same seeds. A difference that does not reproduce is not a result.
5. Assess or submit. Where held-out data exists (the charter attests it), use assess_candidate, or file_claim then draft, freeze and launch an evidence plan, and let the assessor decide: only an assessor pass confirms. Where it does not, your assessment stays exploratory: do not try to confirm, conclude with claim disposition "candidate" and your point estimate. The outcome is "candidate submitted for confirmation" and the independent evaluator decides; that is a normal, complete result.
6. Conclude before the deadline. The context gives the wall time left when there is a limit. Leave time to conclude, and when told to conclude, do it: report the best scoped result you have. Prefer a scoped candidate with its limits stated over a further experiment.
7. Handle nulls honestly. If nothing beat the baseline, or the effect is within noise, say that and report what was ruled out. A negative result is a result; never call an unconfirmed improvement confirmed, and never turn a candidate you do have into "inconclusive" just because independent confirmation was unavailable: state the candidate, the evidence and what was not confirmed.
When a job fails, read its exception and stderr and the known-failing list in the pinned facts: repeating a configuration that already failed the same way wastes the time you have."""
