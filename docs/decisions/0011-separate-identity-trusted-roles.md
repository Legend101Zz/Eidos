# 0011: Separate identity: candidate code as `eidos-cand`, assessor and scorer as the lab identity

Status: accepted (2026-11-09, release 1.1.0). Extends ADR 0004.

**Context.** Since ADR 0004, `linux:bwrap-sealed` drops candidate code to the `eidos-cand` user when the lab runs
as root and that user can execute the project interpreter. `launch()` applied that rule to **every** process
under the profile, including the assessor (`AssessorActivities._run_assessor`). The assessor then ran as
`eidos-cand` and could not read its evidence file, which is deliberately readable only by the lab identity
(0600): with a distinct candidate user the assessor failed closed (v1.0.0 release notes, "Known limitations").
No end-to-end run had covered a sealed job plus its assessment with the distinct identity.

**Decision.**
1. A launch spec names its identity: `candidate` (default; untrusted candidate code) or `lab` (trusted roles).
   Only `candidate` launches under `linux:bwrap-sealed` drop to `eidos-cand` (`launcher.launch_identity`).
2. The assessor, which also computes the scores of the assessment from raw predictions (the trusted scorer of
   the evaluation path), runs with identity `lab`, inside the same bubblewrap confinement: read-only release,
   evidence and interpreter; writable staging and scratch only; no network, credentials or control socket.
3. Component jobs launched by the runner are candidate code, including scoring components a project runs as
   ordinary jobs: under the distinct identity they run as `eidos-cand` and receive only their bound inputs as
   private read-only copies.
4. Receipts say which identity ran: `identity` (`candidate` or `lab`), `lab_uid`, `candidate_uid` and
   `probe_uid_inside`. `separate-identity` is `passed` for a candidate receipt when the probe ran as the
   candidate user, and for a lab-identity (assessor) receipt when it ran as the lab identity **and** a distinct
   candidate user exists, i.e. trusted roles never share an OS identity with candidate code.
5. Root setup is scripted (`scripts/ci/separate_identity_setup.sh`) and documented
   (`docs/ops/INSTALL_AND_OPERATE.md` § "Separate candidate identity (root)"). A CI job `separate-identity` on
   `ubuntu-24.04` runs it with sudo and then, as root, a sealed job through the real runner and command service
   plus its assessment (`tests/integration/test_separate_identity_e2e.py`, which fails instead of skipping in
   that job).

**Consequences.**
- The v1.0.0 limitation "with a distinct identity the assessor fails closed" is removed.
- Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor; bwrap run by `eidos-cand` needs
  `kernel.apparmor_restrict_unprivileged_userns=0` (or an AppArmor profile that allows bwrap). The setup script
  sets the sysctl; operators who keep the restriction must provide such a profile.
- The lab home must be traversable (mode 0711) by `eidos-cand` so it can reach its own attempt directory, and the
  worker SDK sources the runner binds into the sandbox must be readable by it. Everything else under the lab home
  stays private.
- macOS is unchanged: Seatbelt is a same-OS-user profile, and there is no candidate user.
