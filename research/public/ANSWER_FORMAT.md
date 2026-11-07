# Common externally graded answer 0.2

All arms submit the same small JSON record plus loadable artifacts. They may keep any internal journal format. An Eidos claim ledger is optional supporting evidence, never a baseline requirement. Narrative confidence cannot override independent execution.

```json
{
  "schema_version": "eidos-answer/0.2",
  "task_id": "D02",
  "run_id": "example-only",
  "status": "inconclusive",
  "claim_type": "recipe_improvement",
  "claim": "The candidate may improve validation accuracy; independent confirmation is pending.",
  "scope": "This project, budget and training recipe only.",
  "candidate": {
    "code_path": "candidate/project.py",
    "code_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "model_path": "candidate/model.pt",
    "model_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "predictions_path": "candidate/test_predictions.npz",
    "predictions_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "reproduce_command": ["python", "candidate/project.py", "--config", "recipe.json"],
    "environment_lock": "requirements.lock",
    "environment_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "parent_checkpoint": null,
    "restore_class": "fresh_initialization"
  },
  "measurements": [],
  "evidence": [
    {"path": "validation_receipt.json", "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "role": "validation", "supports": "candidate selection", "limitations": "not independent confirmation"}
  ],
  "alternative_explanations": ["training variation"],
  "abstention": {"abstained": false, "reason": null, "unresolved": ["independent confirmation"], "next_discriminating_test": "paired fresh training runs"},
  "cost": {"elapsed_seconds": null, "cpu_core_seconds": null, "gpu_device_seconds": null, "gpu_type": null, "model_input_tokens": null, "model_output_tokens": null, "model_reasoning_tokens": null, "model_calls": null, "billed_usd": null, "unknown_fields": ["elapsed_seconds", "cpu_core_seconds", "gpu_device_seconds", "gpu_type", "model_input_tokens", "model_output_tokens", "model_reasoning_tokens", "model_calls", "billed_usd"], "receipt_paths": []},
  "exposures": ["public development task"],
  "deviations": [],
  "confidence": null
}
```

The all-zero hashes above are illustrative, not artifact-integrity evidence. This example is structurally valid and remains scientifically inconclusive.

Allowed status values: `improvement_claimed`, `no_improvement_found`, `inconclusive`, `invalid`, `abstained`. Only the independent grader can issue `verified_improvement`. Allowed claim types: `checkpoint_improvement`, `recipe_improvement`, `architecture_improvement`, `planted_diagnosis`, `operational_reliability`, `transfer`, `no_positive_claim`.

`measurements` entries require `metric`, `direction` (`higher_is_better` or `lower_is_better`), `split`, a positive-integer `denominator`, `units`, finite numeric `baseline` and `candidate` values, `uncertainty_method`, and `evidence_receipt`. The receipt is a relative path also listed in `evidence`. An explicit statement that uncertainty is not yet estimated is structurally allowed; it does not satisfy a scientific confirmation requirement. Empty measurements are allowed for non-positive statuses; fabricated zero values are not. `evidence` identifies concrete hashed files, their provenance, role and limitations. Causal explanations need competing hypotheses and discriminating executed controls. A planted-cause label does not turn every plausible explanation into causal evidence.

For abstention, use `status: "abstained"`, `claim_type: "no_positive_claim"`, and `abstention.abstained: true`; set `candidate` to null when none exists. Supply a nonempty reason, at least one unresolved obligation, and the next discriminating test. Other statuses require `abstention.abstained: false`. An `improvement_claimed` record requires a non-null complete candidate, nonempty evidence and measurements, and a positive claim type. An unsuccessful search is not an impossibility proof. Null/inconclusive responses can earn evidence-discipline credit while still yielding zero primary improvement successes. `confidence`, if used, is a finite probability in [0,1] and requires a nonempty top-level `confidence_outcome` naming the prospectively checkable outcome; calibration is summarized only over enough eligible cases.

Artifact paths must be relative to the submission root, resolve inside it, and point to ordinary files with matching hashes. A qualified evaluator must reject traversal, escaping symlinks, missing artifacts and oversized inputs before processing. Those filesystem and resource checks are not implemented by the structural validator. Exact byte/size limits are a final task-manifest field; not silently invented by a baseline wrapper. Commands are argument arrays; never evaluate arbitrary strings through a shell. Execution belongs in the candidate sandbox, not the scoring process.

Every non-null candidate requires the fields shown above, including hashes for code, model, predictions and the environment lock. `reproduce_command` must be a nonempty list of strings. `parent_checkpoint` is null or a relative path; `restore_class` is a nonempty description whose truth must be checked independently. Each evidence entry requires `path`, lowercase SHA-256 `sha256`, `role`, `supports`, and `limitations`.

`cost` requires every field shown above. Measurements are finite nonnegative numbers or null; token and call counts must be integers when known. `gpu_type` is null or a nonempty string. `unknown_fields` names exactly the null numeric/device fields, without duplicates; unknown is never represented as zero. `receipt_paths` is a list of relative paths. A structurally complete record may still have all costs unknown, which cannot establish budget compliance.

The included validator checks required fields, scalar types, enums, abstention coherence, lexical relative-path syntax, digest format and measurement-to-evidence references. It reads no artifact, resolves no symlink, matches no file hash, executes no command and verifies no scientific or cost claim. A pass is only structural acceptance. Filesystem confinement, size limits, independent execution, budgets, uncertainty and scientific qualification remain separate unqualified integration gates.

Migration from unpublished 0.1: update `schema_version` to `eidos-answer/0.2`; add `candidate.predictions_sha256` and `candidate.environment_lock_sha256` for every non-null candidate; name `confidence_outcome` when confidence is non-null; and populate the complete typed measurement, evidence, abstention and cost structures described above. The validator rejects 0.1 records instead of silently treating their missing fields as valid. Historical 0.1 records remain historical artifacts.
