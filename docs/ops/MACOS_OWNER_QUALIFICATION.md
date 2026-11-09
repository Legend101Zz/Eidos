# macOS owner qualification kit

`scripts/qualify/macos_owner_kit.sh` is run **by the researcher on their own Mac**, when the owner decides. The build agent has not run it on a Mac. Its Linux dry run (`EIDOS_KIT_ALLOW_LINUX=1`, 2026-11-07) passed every step.

## What it does

The kit makes no model calls, needs no API key, sends nothing anywhere, and doesn't touch `~/.eidos`.
1. Builds the wheels from your checkout and installs them into a fresh venv in a temporary directory. It checks that `eidos` is not imported from the checkout.
2. Runs `lab init` (a private PostgreSQL cluster plus the pinned, sha256-verified Temporal server), then `lab start` and `lab doctor --json`.
3. Creates a project. This installs the lab-environment built-in components through validate → publish.
4. Commits an exploration-only charter and runs one deterministic fixture investigation (no LLM).
5. Runs one real component job (`lab.pairs.by_group` on a 3-row CSV) through the Temporal job workflow and the runner. This exercises macOS process identity (`ps` start time, `kern.boottime`) and runs under `macos:seatbelt-sealed`.
6. Sealed-job containment probe (`sealed_probe`): the lab is started with a fake `ANTHROPIC_API_KEY` in its environment and a uniquely named file is planted in `$HOME` (removed afterwards). A project command run through the lab's job path must not see the key, read the planted file, list `$HOME`, or connect to the network or the lab API.
7. Stops the lab, then `uninstall` checks that no process of the lab is left and its ports are free, and removes the venv and the lab directory. `--keep` skips the uninstall and keeps the stopped lab, its venv and the logs.
8. Writes `~/eidos-macos-receipt-<UTC timestamp>.json` (or `$EIDOS_KIT_RECEIPT`). On failure the step logs are copied next to it (`<receipt>.logs/`). The receipt contains:
   - git describe;
   - macOS version and architecture;
   - Python version;
   - each step's exit code and duration;
   - the doctor checks;
   - the job status and its isolation profile;
   - the sealed-probe result;
   - the investigation's turns.

   It records no credentials and no file contents.

## Run

```bash
brew install postgresql@16          # binaries only; the kit runs its own cluster
curl -LsSf https://astral.sh/uv/install.sh | sh    # if uv is not installed
git clone https://github.com/Legend101Zz/Eidos && cd Eidos && git checkout <release tag>
bash scripts/qualify/macos_owner_kit.sh
```

Requirements:
- Free ports 18765, 15544 and 17633–17641.
- About 1 GB of disk while the kit runs. The work directory is created under `$TMPDIR` (set `TMPDIR`, `UV_CACHE_DIR` and `UV_PYTHON_INSTALL_DIR` to keep everything on an external volume; see `STORAGE_PATHS.md`).
- If GitHub release downloads fail (HTTP 504 was seen on 2026-11-08), point `EIDOS_TEMPORAL_ARCHIVE_CACHE` at a directory holding `temporal-server-<pinned sha256>.tgz`; `lab init` re-verifies the digest. `gh release download v1.32.2 -R temporalio/temporal -p 'temporal_1.32.2_darwin_arm64.tar.gz'` (GitHub API asset route) worked while the browser-download URL returned 504; check its sha256 against `TEMPORAL_RELEASES` in `src/eidos/supervisor/services.py` before using it.
- Network access to PyPI and GitHub release assets.

## Interpreting the receipt

- **Passes** when `passed` is true. Expected doctor warnings on macOS (observed on macOS 26.3.1, M4, 2026-11-08):
  - `sealed-profile:separate-identity`: there is no separate candidate user;
  - `sealed-profile:lima-vm`: the Lima VM profile is not qualified.
  Doctor reports `sealed-profile:seatbelt` ok when `sandbox-exec` works.
- **What a pass shows:** install, services, a fixture investigation and real jobs under `macos:seatbelt-sealed` on that Mac, with the containment probe holding (same OS user; no `separate-identity`).
- **What a pass does not show:** isolation from a hostile same-user program (needs a Lima VM profile, not implemented), MPS/CUDA training, or real-LLM investigation.
