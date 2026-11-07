# macOS owner qualification kit

`scripts/qualify/macos_owner_kit.sh` is run **by the researcher on their own Mac**, when the owner decides. The build agent has not run it on a Mac. Its Linux dry run (`EIDOS_KIT_ALLOW_LINUX=1`, 2026-11-07) passed every step.

## What it does

The kit makes no model calls, needs no API key, sends nothing anywhere, and doesn't touch `~/.eidos`.
1. Builds the wheels from your checkout and installs them into a fresh venv in a temporary directory. It checks that `eidos` is not imported from the checkout.
2. Runs `lab init` (a private PostgreSQL cluster plus the pinned, sha256-verified Temporal server), then `lab start` and `lab doctor --json`.
3. Creates a project. This installs the lab-environment built-in components through validate → publish.
4. Commits an exploration-only charter and runs one deterministic fixture investigation (no LLM).
5. Runs one real component job (`lab.pairs.by_group` on a 3-row CSV) through the Temporal job workflow and the runner. This exercises macOS process identity (`ps` start time, `kern.boottime`).
6. Stops the lab and deletes the temporary directory (keep it with `--keep`).
7. Writes `~/eidos-macos-receipt-<UTC timestamp>.json`. The receipt contains:
   - git describe;
   - macOS version and architecture;
   - Python version;
   - each step's exit code and duration;
   - the doctor checks;
   - the job status;
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
- About 1 GB of disk while the kit runs.
- Network access to PyPI and GitHub release assets.

## Interpreting the receipt

- **Passes** when `passed` is true. Expected doctor warnings on macOS:
  - the sealed profile is unavailable (no bubblewrap);
  - there is no separate candidate user;
  - the Lima VM profile is not qualified.
- **What a pass shows:** the host-native `dev:same-user` profile on that Mac.
- **What a pass does not show:** sealed evaluation (needs a qualified Lima VM profile, not implemented), MPS/CUDA training, or real-LLM investigation.
