# ADR 0033 · Study-private export of visible recipe-sweep evidence

**Status** Proposed for a subsequent development build.

The settled Tier 1 candidate N02-r1 concluded with a genuine abstention. Its measurement cited a recipe-sweep artifact produced by that investigation and visible to the investigator. The answer adapter exported only job receipt identifiers, and the structural validator rejected the unresolved citation. The original answer and grade are immutable.

The arm 4 study adapter may resolve a cited `evidence_table` output of the registered `eidos.search.recipe-sweep@1.1.0` release into a study-private evidence file. It checks the exact artifact and producer identities, successful same-investigation job, registered release digests, publication attempt, output role and schema, project access class, and exact imported train and validation bindings. Only the typed template route is eligible. The downloaded JSON must match the canonical byte size and digest and identify itself as exploratory visible-validation data with no claim. An authored digest, when supplied for the citation, must agree with the canonical digest. Unknown, foreign, restricted, or mismatched provenance fails the export.

The adapter records the original artifact identifier, local path, digest and producer in a run receipt. The immutable ledger retains the investigator-authored conclusion. The answer uses the local path in the measurement and lists the same hashed evidence file. The investigator's claim, measurements, status and abstention remain as authored apart from the path mapping. This policy grants no public release permission and provides no held-out confirmation.
