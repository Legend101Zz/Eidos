# Mac-side qualification status (local Mac session)

Machine: Apple M4, 24 GiB, macOS 26.3.1 (arm64). All environments, caches, labs and run outputs on the external SSD
(`/Volumes/Mrigesh SSD/eidos-mac`, via `env.sh`). Branch `mac/local-qualification` (from `origin/build/eidos-local-v1`
at 80c1e2d; merged 48d4338 at start). Internal disk free at start: 24.9 GiB.

## Item 1: macOS researcher-machine qualification (A1)

**Evidence class: macOS-researcher-machine runtime-verified** (same OS user; no `separate-identity`; no Lima VM).

What ran:

| Run | Command | SHA | Result |
|---|---|---|---|
| kit-1 | `PG_BIN=/opt/homebrew/opt/postgresql@16/bin EIDOS_KIT_RECEIPT=... bash scripts/qualify/macos_owner_kit.sh --keep` | 48d4338 + TMPDIR fix | passed (13/13 steps); job under `macos:seatbelt-sealed` |
| job-path home probe | kit-1 lab restarted with a fake `ANTHROPIC_API_KEY`; `lab run macq eidos.project.command@1.0.6` with a probe script | 48d4338 wheels | **FAILED containment**: key hidden, network denied, but the planted `~/.eidos-planted-homeprobe` and `~/.claude.json` were readable (`receipts/a1-job-path-home-read-before-fix.json`) |
| seatbelt probes | `OUT=... .venv/bin/python scripts/ci/seatbelt_probes.py` | 48d4338, then b7d9ed1 | passed both times (7 sealed controls passed; `separate-identity` failed as expected; negative control detected every access; permitted computation ok) |
| assessor probe | `scripts/ci/assessor_sandbox_probe.py` | 48d4338 | `protocol-passed`, all 7 controls passed under `macos:seatbelt-sealed` |
| kit-2 | kit at 1cf2728+edits | — | `init` failed: GitHub release asset HTTP 504 after 5 attempts (and 6 more `curl` tries); not a code failure |
| kit-3 | kit with `EIDOS_TEMPORAL_ARCHIVE_CACHE` (archive fetched via `gh release download`, sha256 = pinned `6c34c8ac…b2f0`) | 1cf2728 + kit edits (dirty) | passed 15/15 |
| **kit-4** | same, clean tree | **36aaffa** | **passed 15/15** (`receipts/a1-macos-owner-kit.json`): fresh wheels (`--no-cache`), init 13 s, start, doctor (only warnings: `separate-identity`, `lima-vm`), project, charter, fixture investigation (turn 1 → `input-requested`), real job `lab.pairs.by_group` under `macos:seatbelt-sealed`, `sealed_probe` (key hidden, `$HOME` file and listing refused, external network and lab API refused), stop, uninstall (no process/port left, venv+lab removed) |
| contained commands | `EIDOS_RESEARCH_PYTHON=$S/venvs/research/bin/python pytest -q tests/integration/test_contained_commands.py` | 48d4338 | 4 passed (incl. the D02 baseline contained) |

Fixes:

- **b7d9ed1 (core, security)**: sealed lab jobs and the assessor could read the user's home directory on macOS.
  The runner's sealed spec denied only the lab root + a fixed credential-dir list (`run_contained` already denied
  `$HOME`). Now `launcher.sealed_deny_read()` (lab roots + `$HOME` + credential dirs) is used by the runner, the
  assessor sandbox, `run_contained` and the CI probes; `python_prefixes()` also re-allows the interpreter's
  `sys.path`; the `no-credential-read` probe also fails if `$HOME` can be listed. Regression test
  `tests/integration/test_sealed_home_read.py` (4 tests, fail without the fix; project roots inside `$HOME` stay readable).
  Linux `bwrap` was not affected (home is not mounted).
- 1cf2728, 36aaffa (kit): work dir under `$TMPDIR` (was hard-coded `/tmp`), `EIDOS_KIT_RECEIPT`; new `sealed_probe`
  and `uninstall` steps; the receipt recorded the receipt-writer's Python (3.14.5) instead of the lab's (3.12.8); job
  isolation profile in the receipt; logs kept next to a failed receipt; doc's expected doctor warnings were wrong for
  macOS (it said "sealed profile unavailable (no bubblewrap)"; doctor reports seatbelt ok).

What was wrong in the cloud-written kit/runbook (for the cloud session):

- `mktemp -d /tmp/...` ignores `TMPDIR` (writes to the internal disk).
- No uninstall step and no containment probe of the job path, although the brief/A1 needs both.
- `doctor` step can never fail (`...; true`); only the receipt's `passed` reflects doctor `fail` checks (left as is).
- `lab init`'s "next" hint prints `EIDOS_HOME=/Volumes/Mrigesh SSD/... lab start` unquoted (breaks on paths with spaces).

Open: GitHub release-asset downloads (browser URL) returned 504 for the whole session; the API asset route worked. See REQUESTS.md.
