Card-set profiles are selected from the card ID. `EIDOS_CARD_PROFILE` can pin a
profile; a card/profile mismatch refuses use. With no selection, `configured_seal()`
retains Pilot 4 and `EIDOS_PRIVATE_SEAL_ROOT`.

| Profile | Cards | Commitment file | Seal-root variable |
| --- | --- | --- | --- |
| pilot-4 | N05–N08 | pilot4_commitment.txt | EIDOS_PRIVATE_SEAL_ROOT |
| pilot-5 | N09–N12 | pilot5_commitment.txt | EIDOS_PILOT5_SEAL_ROOT |
| pilot-6 | N13–N16 | pilot6_commitment.txt | EIDOS_PILOT6_SEAL_ROOT |

Pilot 5's digest is copied from the published Reserve commitment in
`prospective_commitments.txt`. Pilot 6's digest is copied from the already public Pilot 6 line in the same file.
Its commitment loads, while configuration still refuses missing public metadata.

N09–N16 metric kinds and per-card relative bar rules are **unknown — to be
supplied by the trusted plane**. No alternating pattern is assumed. The trusted
plane can publish these in the registry or provide `EIDOS_CARD_PROFILE_METADATA`
as a JSON object mapping each card in the selected profile to `[metric, bar]`.
Allowed rules are `["accuracy", 0.02]` (absolute gain) and `["rmse", 0.05]`
(fraction of the evaluator baseline). All four cards must be supplied before use.
This input must contain public metadata only, never absolute RMSE thresholds.
Supplying metadata cannot override the published digest or Pilot 4 metadata.

`PrivateSeal(root, commitment, cards)` takes the profile's validated card-to-rule
mapping or a roster resolved through the public registry; omission retains the
historical Pilot 4 mapping. It verifies the whole
roster, then stages only the seven model-facing files. New profiles check the
RMSE bar against the sealed baseline at scoring. `python -m study.reconcile_seal
--profile pilot-5` additionally checks all manifests, fixed confirmation seeds,
and relative bar rules and returns only booleans and a card count. Private
reconcilers should call `verify_profile(profile)` before accepting run receipts.

All seal-root variables are stripped before workers run. A new pilot also needs
its own registered charter and pinned grader revision supplied through the
existing controller configuration. This plumbing supplies neither registration
nor evidence of builder blindness.

Scientific treatments are independent of card profiles. `EIDOS_STUDY_TREATMENT`
defaults to `pilot4-v1`; the v2 portfolio requires explicit `v2-portfolio` selection.
See [treatment instructions](exp/README.md). No profile selects v2 implicitly.
