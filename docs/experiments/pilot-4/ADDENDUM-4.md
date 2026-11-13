# Pilot 4 Addendum 4 — final-capture structural validation

R6 is a direct child of R5 `70fbcb691ee5e2ec3bcabb88fc065ca7ad12b5d1`.
This defect was discovered after all 48 assignments (24 paired blocks) had
resolved. The v5 parent halted when its complete capture refused the
controller-produced `prompt-receipts/` directory. The previous strict layout
expected only `block_record.json`, `arm2/` and `arm4/`; its synthetic fixtures
omitted the controller's ADR 0041 prompt-delivery artefacts.

Only structural capture validation and its private binding change:

1. v10.6 requires the final block layout with `prompt-receipts/` and verifies
   exactly `arm2-expected.json`, `arm2-delivered.json`, `arm4-expected.json`,
   `arm4-delivered.json`. Per-run receipt and arm/run identities, exact JSON
   schemas, SHA-256 fields and delivery surfaces must agree. The delivered
   hash for arm 2 must equal the expected-render hash. For arm 4, ADR 0041 and
   the adapter at registered controller C `86dfe5791ac75e76998912ea19efd6461b93444d`
   define the delivered text as the render plus two newlines, `Packaged task
   files:` and a newline, then `json.dumps(files, ensure_ascii=False)`. Files
   are sorted task-relative POSIX paths with suffix .py/.md/.json/.lock/.txt,
   read with the adapter's text I/O; METHOD.md is inserted from registered C.
   v10.6 reconstructs the registered render, verifies its hash and common
   template against the expected receipt, verifies every staged bundle file
   and exact roster, reconstructs the exact question and requires its hash
   to equal delivered_sha256 on the investigation-question surface. Missing,
   changed or ambiguous bundle inputs refuse. Receipts are never normalized
   or rewritten. Evidence tracking rejects links
   and changes during capture. The run's controller delivery receipt is the
   external delivered JSON; the historical run_record.json has no embedded
   prompt hash, and none is fabricated.
2. Capture-only FREEZE/ACTIVATION v6 bind R6 and the v10.6 closure while pinning
   the exact R5 admission freeze as `capture_source`. The reconciler verifies
   unchanged admission/scientific fields and uses that original freeze and
   chain for all historical assignments. The captured manifest hashes the
   original freeze and the separate capture-validation freeze. This introduces
   no admission generation, retrospective switch or parent state alteration.
3. Fixtures now reproduce the controller's directory and receipt schema;
   tests cover receipt and binding refusals, full 48-assignment synthetic
   capture, exact agreement with construction statements extracted from C on
   fabricated bundles, and missing/changed/extra/linked task and staging inputs.
   Hash manifests and the
   smoke's repair-manifest pin are refreshed. No other executable helper changes.

No estimator, decision rule, status gate, verdict-of-record selection,
denominator, incident rule, admission policy, model, budget, window or order
changes. Existing scientific function source and policy helpers are byte
identical. Development did not inspect grade/verdict/gain values to choose a
change. The real-evidence structural diagnostic disables both estimator
invocations and all aggregate summary computation. It never emits a complete
capture or report-ready receipt. Private sealed-source/numeric checks and
private candidate repacking are deferred within this read scope; production
capture retains them unchanged and must pass them before scientific reporting.

R2 replaces the earlier unpushed R6, retaining R5 as its sole parent. The
orchestrator's round-2 structural decision resolved the render/delivery
contract distinction using ADR 0041 and the registered arm-4 adapter. This
fix was made before any real primary estimate, interval, aggregate gain or
outcome summary was computed. Changes were selected from controller code,
not grades, verdicts or gains. The read-only structural dry-run passed all
200 independent gates, with zero remaining refusals, including all 24 arm-4
question hashes and all 24 arm-2 render/delivery pairs. No class-(a) tooling
gap or class-(b) evidence refusal remains in the exercised structural gates.
The full result and source hashes are retained privately in
`registration/ADDENDUM4_STRUCTURAL_VALIDATION.json`. This is not a successful
complete capture or scientific-source qualification; deferred checks above
remain mandatory before reporting.

The reported eight arm-4 local precheck PermissionErrors cause no additional
refusal in the existing structural local-grade validation: all 48 local
identity/resource/status/inventory gates pass in the read-only diagnostic.
No precheck is rerun or relabelled. The charter's Design and scope states:
"Local grading is a pre-check. The verdict of record requires the hosted
private grade plus the recorded run-status gate." Its Outcomes section
requires exact 48-assignment reconciliation and resolved incident membership
before estimation. Missing or inconsistent local evidence remains blocking in
the unchanged capture implementation; no missing grade becomes a scientific
failure and no hosted grade or status gate is replaced by a local result.

No parent switch is needed to invoke the new guarded capture entry directly.
The v5 parent's exact NEEDS identity is
`b5574ac7d6d5af53a1624b74c00463bd4a5b6fcf7feb4a2cf1d20698f831d4d9`.
After separately authorized resolution, a successful exact guarded capture
and independent audit, the orchestrator may use the existing v5 resume/ack
entry with that exact SHA. Only the lock-owning parent creates its genuine
acknowledgement receipt. This acknowledges the halt occurrence; it does not
adopt an external capture, change the parent's pins or replace its failed job.
Do not fabricate an ack/switch/capture receipt or edit the parent's state.

Preparation only. No push, application, real capture, acknowledgement, live
process inspection/signalling, lock acquisition or live state mutation occurs.
