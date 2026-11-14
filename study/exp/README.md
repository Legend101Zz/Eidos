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
resolved investigator, selected prompt bytes, ordered component plans for N01,
N02 and D03 (bundle digests, bindings and upload names), and the complete toolkit
manifest digest. Component publications also bind source bytes. Lifecycle
annotations do not change the resolved hash, so rejection does not break
historical identity. `for_run` refuses missing treatment metadata during live
dispatch; offline replay can still accept legacy metadata. Arm 4 also records the canonical effective
investigator SHA-256 before per-run mutations. Its legacy configuration hash
retains the base-file hash only when the effective dictionary equals the base.

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
cycle as an empty-patch alias. The default refuses every flag (including adopted
aliases) and every override.
An adoption updates the default implementation through a kept experiment, rather
than selecting an alias in its definition.

To reject a flag, set its state to `rejected` and fill `removal` with `decided`,
`experiment`, and `reason`. Keep its patch for replay. New resolutions refuse it;
`resolve(id, allow_rejected=True)` is only for historical replay. Update active
treatments to omit it before `check` will pass. `removal-list` is a dry run: it
reports per-path deletion eligibility, procedures,
blocking flags/treatments/core imports, and treatments that would be retired.
`--assume-rejected floor.v2-slice,toolkit.portfolio` previews a decision without
changing the registry. A nonzero exit means a blocker remains. In that preview,
`floor.selection-margin` still requires the slice and must also be retired.

To remove rejected code, review that inventory, all remaining flag dependencies,
and inline policy branches, then delete only paths marked `removable` and obsolete
wiring,
remove the flag from active treatments, and remove its registry entry. Some
features share modules: the selection-margin flag has no standalone files, so
its inline branches still require review. Entries with `kind: procedure` describe
restoration instead of deletion: rejecting the diagnosis fix restores the pinned
1.0.0 bytes as the unpinned release. Portfolio entries declare `shared_with` in
both owning flags and cannot be deleted while either owner remains. The dry run
resolves every surviving treatment, builds component/toolkit plans for N01, N02
and D03, and repeats resolution and artifact hashing in a disposable tree after
virtual deletions/restoration. Treatment definitions selecting removed flags are
reported for retirement rather than silently rewritten. Dependent flags must be
retired as well before removing registry entries. The portfolio source is used by
both v2 lab and toolkit flags; retain it while either treatment use remains. Re-run the goldens,
registry check, replay tests and the full relevant validation suite.

`toolkit-base.json` is the directory/file inventory main copied. The default maps
its diagnosis destination to the byte-identical pinned 1.0.0 source. Exported
`COVERAGE.md` uses frozen main documentation for the unchanged default tree;
selected toolkit changes use the current coverage document. The diagnosis fix
also retains its pinned 1.0.0 directory in experimental toolkits for explicit
replay. The treatment receipt is outside the toolkit, preserving default tree bytes. Reusing a toolkit directory
under a different treatment is refused.

`flags check` validates inventory paths and builds every treatment's component and
toolkit plans for N01, N02 and D03. Runtime resolution does not require code
inventory paths (such as tests) to be shipped. Unknown treatment and patch keys
are refused. `floor.v2-slice` recommends `floor.selection-margin`: the v2 prompts
advertise a one-SE margin, and `check` warns if a treatment selects the slice
without that flag. The recommendation is not a dependency; omission still means
strict improvement with no margin.
