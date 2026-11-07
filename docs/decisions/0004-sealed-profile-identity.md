# 0004 — Sealed profile: namespaces first, distinct identity where supported

Status: accepted (2026-11-07)

**Decision.** `linux:bwrap-sealed` requires six access controls (no label read, no credential read, no host write, no network egress, no control socket, no scorer capability), each established by launcher-run hostile probes in the same sandbox configuration, with host writes judged from the host side. `separate-identity` is always measured and reported, and required only if a plan lists it. A distinct candidate user is used only when it can actually run the project interpreter.

**Why.** In the build sandbox the candidate user could not read a root-owned interpreter; forcing it broke execution, while namespace isolation alone passed all access probes. Reporting the identity control truthfully keeps plans that require it unsatisfiable rather than silently weakened.

**Reversal.** Make separate-identity default-required once installers place project environments where the candidate identity can read them.
