# 0052: Prospective treatments and feature flags

Date: 2026-11-14. Status: accepted. Owner decision D1.

The v2 merge changed the study configuration, prompts, catalog snapshot, toolkit,
and two policy-2 behaviors. Merging dormant experimental code must preserve the
registered default. `study.exp` now resolves a standard-library-only registry of
flags and named prospective treatments. `EIDOS_STUDY_TREATMENT` defaults to
`pilot4-v1`; unknown treatments or flags are errors. `v2-portfolio` explicitly
selects corrected diagnosis, v2 policies/portfolio, a selection margin and toolkit
parity. Dependencies are explicit, validated and ordered before dependents.
Investigator patches and final overrides use RFC 7396.

The immutable investigator and arm-4 prompt retain main's bytes. Resolution drives
component publication/binding and toolkit source mapping as well as investigator
configuration and prompt selection. The default publishes only main's components,
binds diagnosis 1.0.0, and exports main's toolkit bytes. The v2 prompt lives in a
separate file. Nameless-lock rejection, the preparation tool vocabulary and
incumbent evidence supplementation require exploration-policy/3. Legacy proposal
validation and corrective feedback also exclude preparation; replay uses the
recorded vocabulary. Special prepared-lock/portfolio truncation requires
context-policy/4. An absent
selection-margin setting means strict improvement. Existing context-policy/3,
playbook 2 and prompt revision 5 remain unchanged.

A flag starts `experimental`, usable only by explicit selection. The default
changes only through a kept experiment. A kept patch is folded into the default;
its `adopted` flag remains one cycle as an empty-patch alias. `rejected` means
**marked for removal**, with a dated experiment/reason decision. New runs refuse
rejected flags. Historical replay can explicitly allow them. Removal tooling lists
per-path removable/blocked results, restoration procedures, and treatment
references. It checks shared owners and core imports, then virtually removes
proposed paths and re-resolves every surviving treatment (including the default),
building component/toolkit plans and hashing their artifacts. Nonzero exit means
blockers remain; shared inline branches still require review. Diagnosis rejection
restores the pinned 1.0.0 content as the unpinned release. Portfolio deletion
requires rejection of both owners. `--assume-rejected` previews this without any
registry change. Dependent flags and treatments selecting removed flags must be
retired before deleting registry entries. Strict inventory checks belong to
`flags check` and the removal dry run, not runtime resolution. Unknown treatment
and patch keys are errors. The default accepts no flags or overrides.

Arm metadata, lab setup/import, arm-4 run records and ADR 0041 prompt receipts
carry id, flags and SHA-256 of the canonical effective treatment. Workers refuse
selection drift and missing live-dispatch treatment records. The treatment hash
covers ordered N01/N02/D03 plans with bundle digests, bindings and upload names,
and the complete toolkit manifest digest; it excludes lifecycle annotations.
Arm 4 records `investigator_effective_sha256` from canonical resolved JSON before
per-run mutations. `investigator_config_sha` keeps the registered base-file hash
only when the resolved dictionary equals the base, otherwise it is the effective
hash. Default diagnosis uploads retain main's unpinned artifact name. For every
installed component, other pinned releases are published unbound; this includes
diagnosis 1.0.0 under v2 and preserves step 3's catalog. Live
prompt-override refusals still apply. No registry data contains credentials.

Golden checks in `study/tests/test_default_treatment_golden.py` and
`tests/unit/test_default_policy_golden.py` pin main 806f939's investigator bytes,
arm-4 tools bytes, full public N01 and public-metadata Pilot 4 N05 renders,
ordered component refs/bundle digests/bindings for N01/N02/D03, full exported
toolkit tree, policy-2 turn vocabulary and nameless-lock semantics, prompt
revision/system/playbook/tool-guide hashes. They also pin step 3's parsed v2
configuration and test registry rejection, dependency/default rules, worker drift,
merge patches and CLI validation. Recorded-turn replay tests remain unchanged.
These values were computed from `git show origin/main:<path>` and a public-source
`git archive origin/main` imported in an isolated subprocess, never sealed data.

Portfolio manifests declare the pinned numpy/scikit-learn/joblib dependencies.
Dependency preflight runs for every component at validation, before fixture
execution, and never changes a build lock or release digest. The portfolio pins
exact numpy/scikit-learn/joblib versions on purpose: a different measured
environment fails closed with a dependency refusal.
CI integration and journey project environments provide these packages and run
the real fixtures. Study tests run on main and all task branches without secrets.

`floor.v2-slice` recommends `floor.selection-margin`; `flags check` warns when
the slice is selected without the margin advertised by the v2 prompts. Without
it, lock replacement still requires strict improvement. Permanent consumer tests
exercise prepare, arm 4, launcher/lab forwarding and their records. Additional
main-derived differential goldens pin ten context-policy/3 packs and 400
policy-2 exploration scenarios, and step-3 goldens pin N01/N02/D03 publications.
