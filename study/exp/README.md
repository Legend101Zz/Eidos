# Study treatments and feature flags

The default is `pilot4-v1`. It preserves the public Pilot 4 configuration, prompt,
component bindings and toolkit from main at `806f939`. Select the experimental
portfolio explicitly before launching a block:

```sh
export EIDOS_STUDY_TREATMENT=v2-portfolio
python -m study.exp.flags show v2-portfolio
python -m study.exp.flags check
```

Selection passes through launcher and lab worker environments. `arm.json`, arm-4
setup and run records, lab import records, and expected/delivered prompt receipts
carry the treatment id, ordered flag ids and canonical resolved SHA-256. A worker
refuses a different selection from its prepared arm record. The hash covers the
resolved investigator, selected prompt bytes, component settings and toolkit
mapping; component publications and the toolkit manifest separately bind source
bytes. Lifecycle annotations do not change the resolved hash, so rejection does
not break historical identity. `for_run` accepts old metadata without a treatment
field for compatibility; new launcher runs always record it.

To add a flag, add an entry to `flags.json` and name it in a new treatment under
`treatments/`. Keep the immutable investigator base and Pilot 4 goldens unchanged.
Component `install` and toolkit `add` operations accumulate across flags; binding
and replacement maps merge by destination. Investigator patches and treatment
overrides follow RFC 7396: objects merge,
`null` deletes, arrays and scalars replace. Overrides apply to the investigator
configuration after flag patches. Requirements must be explicitly present in the
treatment; dependencies apply before dependents. Unknown selections, missing
requirements, cycles and invalid lifecycle records refuse resolution.

States are `experimental`, `adopted`, and `rejected`. Experimental flags require
explicit treatment selection. Only a kept experiment can change the default.
After adoption, fold its patch into the default and keep the adopted flag one
cycle as an empty-patch alias. No experimental or rejected flag can be in the
default treatment.

To reject a flag, set its state to `rejected` and fill `removal` with `decided`,
`experiment`, and `reason`. Keep its patch for replay. New resolutions refuse it;
`resolve(id, allow_rejected=True)` is only for historical replay. Update active
treatments to omit it before `check` will pass. `removal-list` lists rejected
flags, their exclusive code paths, and treatments still referencing them.

To remove rejected code, review that inventory, all remaining flag dependencies,
and inline policy branches, then delete exclusive files and obsolete wiring,
remove the flag from active treatments, and remove its registry entry. Some
features share modules: empty `code` lists for the selection-margin and toolkit
flags mean their changes live in shared files, not that rejection removes their
inline branches automatically. The portfolio source is used by both v2 lab and
toolkit flags; retain it while either treatment use remains. Re-run the goldens,
registry check, replay tests and the full relevant validation suite.

`toolkit-base.json` is the directory/file inventory main copied. The default maps
its diagnosis destination to the byte-identical pinned 1.0.0 source. Exported
`COVERAGE.md` uses frozen main documentation for the unchanged default tree;
selected toolkit changes use the current coverage document. The diagnosis fix
also retains its pinned 1.0.0 directory in experimental toolkits for explicit replay. The treatment receipt
is outside the toolkit, preserving default tree bytes. Reusing a toolkit directory
under a different treatment is refused.
