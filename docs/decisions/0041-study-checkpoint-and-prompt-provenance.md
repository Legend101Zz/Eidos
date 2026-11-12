# ADR 0041: Bind study checkpoint names and delivered prompts

## Decision

Study arms 3 and 4 install the controller-owned project-command 1.0.8 release explicitly into fresh study projects. The controller checks the canonical bundle digest, published artifact bytes and binding. Both runtime builds receive the same release. Other selected built-ins still come from the installed runtime. The older project-command release stays available outside these projects.

The new producer receipt records original safe basename, normalized slot, staged SHA-256 and byte size. The arm-4 exporter requires one matching model output and the pinned producing job, checks receipt and checkpoint bytes, and preserves the basename in the existing answer path and run record. Missing provenance, unsafe names and ambiguous exports refuse explicitly. Normalized slots, staged filenames and training argv do not establish an original checkpoint name. Fresh confirmation and the answer schema are unchanged.

The launcher rejects smoke prompt and lane-root overrides on live dispatch and removes them from worker environments. The expected common template and arm render hashes are recorded outside the agent-writable arm directory. Each adapter separately hashes the exact initial Claude prompt or investigation question at dispatch. For arm 4, the question includes the embedded task text and method. Receipt absence remains explicit when dispatch did not occur. Offline smoke entry points remain available.

Visible inference readiness precedes the first and each replacement lock. Selection, assessment freezes and the ordinary search budget retain their authority. The common Arm 4 guidance identifies both decision and effect purpose for direct reproduction or confirmation. A launch uses the purpose frozen on its plan. Pending challenger work stays tied to its admitted outputs.

## Consequences

The old runtime with this shared controller and producer is a matched development instrument with updated study inputs. It does not recreate the original historical runtime experience. The new producer release and receipt fields are inspectable in both study projects. Exact installed-runtime compatibility requires no-model installation, production and export evidence for both frozen builds before live qualification.
