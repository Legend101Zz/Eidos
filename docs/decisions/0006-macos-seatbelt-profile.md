# 0006 — macOS candidate sandbox via Seatbelt (`macos:seatbelt-sealed`)

Status: accepted for development (2026-11-08). Qualification: GitHub-runner evidence only.

**Decision.** On darwin the trusted launcher runs candidate code under `/usr/bin/sandbox-exec` with a profile generated per attempt (`src/eidos/runtime/launcher.py::seatbelt_policy`).

- The base is `(allow default)` with targeted denies:
  - network;
  - reads of file contents and xattrs under the lab home and common credential directories, except the attempt's own files inside them;
  - writes everywhere except staging/scratch/resume, a private tmp, /dev and the attempt's logs.
- The environment is cleared.
- Paths are `-D` parameters, never spliced into profile text.
- `lab init` selects it as the strongest available profile when `sandbox-exec` works. Each run's receipt holds the executed probe results, so nothing is assumed.

**Why.**
- It needs no VM, no extra install and no root.
- It blocks the accesses the design names for candidate code: labels, lab credentials, host writes, egress, the control socket and scorer capability.
- A deny-default profile was rejected because Python and PyTorch need many undocumented mach and sysctl permissions, and it could not be iterated on a macOS host in this build.

**Limits (recorded, not hidden).** Seatbelt does **not** isolate candidate code from a hostile same-user program. After review 2 it also denies inspecting other processes (probed as `no-process-inspection`), exec of launchctl/osascript/open and the securityd mach services. Other launchd/XPC routes and signals stay open (`docs/dev-runs/adversarial-review-2-2026-11-08.md`). The per-limit details follow.
- Same OS user, so `separate-identity` fails. Plans that require it cannot pass on macOS.
- Mach IPC is unfiltered; the Keychain is reachable subject to its own ACLs.
- Apple deprecates `sandbox-exec`.
- Metadata (stat) of denied paths stays visible.
- The assessor process on macOS still runs same-user.
- A Lima VM profile is not implemented.

**Reversal.** If a future macOS release breaks `sandbox-exec`, `detect_sealed_profile()` returns none. The lab then falls back to `dev:same-user` with recorded waivers, and plans that require sealed execution are refused.

## Amendment (v1.0.0, 2026-11-08): Seatbelt is not hostile-code isolation

Owner decision for v1: macOS isolation is Seatbelt with documented limits, and sealed (hostile-code) evaluation is advertised on Linux (`linux:bwrap-sealed`) only. No Lima VM in v1.

Before this amendment, `evaluate_receipt` treated `macos:seatbelt-sealed` like any `*-sealed` profile. A Seatbelt receipt that passed the six sealed controls therefore satisfied a plan with `sealed: true`, although the candidate ran as the lab's own OS user. That contradicted A11 ("a same-user receipt cannot satisfy a plan requiring sealed execution").

Now:
- Each profile declares `same_os_user` and `hostile_code`. Only hostile-code profiles satisfy `requires_sealed` (`sealed: true`, or a named hostile-code profile).
- A lab whose strongest profile is not hostile-code refuses such plans at freeze and at launch.
- Receipts carry `same_os_user` and `hostile_code_isolation`, derived by the service from the profile name.
- Seatbelt still satisfies control-level requirements, and it stays the default for candidate code and model-chosen commands on macOS.

The limits listed above stay as they are and are restated in `docs/release/SUPPORTED.md`: launchd/XPC by other routes, mach IPC, signals, possible hard links, the deprecated `sandbox-exec`, and the same-user assessor.
