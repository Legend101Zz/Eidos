# Mac qualification runbook (controller)

For the controller running on the owner's Mac (Apple Silicon, macOS 26.3.1, storage on an external SSD). Parts a, c, d of the plan. The lead's real-LLM steps go in section 5. Nothing in the scripts below edits `~/.claude*`, `~/.codex*` or the Claude Desktop config; every run writes receipts you can attach.
Not run on a Mac by the author: the Linux checks are listed at the end.

## 0. Prerequisites
* Checkout of Eidos, `uv`, PostgreSQL 16 binaries (Homebrew `postgresql@16`), the SSD mounted.
* Claude Code 2.1.318 and Codex CLI 0.157.0 installed and logged in (they use their own existing credentials).
* Read `docs/ops/STORAGE_PATHS.md`; set the preamble there so everything lands on the SSD.

## 1. Storage on the external SSD (d)
1. Export the preamble from `STORAGE_PATHS.md` (`EIDOS_HOME`, `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `TMPDIR`).
2. Build the lab venv and the research venv on the SSD (`uv venv $S/venvs/lab`, `uv venv $S/venvs/research`); keep the research venv **outside** `$EIDOS_HOME`.
3. After the lab has run something, confirm nothing landed on the internal disk: `du -sh ~/.eidos ~/Library/Caches/uv ~/.local/share/uv 2>/dev/null` (expect missing or unchanged) and `ls $TMPDIR`.
Record: the output of those commands in the receipt notes.

## 2. Lab with data to read
Use the existing flow (`scripts/qualify/macos_owner_kit.sh --keep`, or `lab init`/`start`, `lab project create`, a fixture investigation with `--policy conclude-immediately --runtime fixture --environment lab`). Note the **project id** and an **investigation id**, and keep the lab running.

## 3. MCP clients (a)
What `lab mcp` exposes (read from `src/eidos/mcp/`, not guessed): server name `eidos-lab`, 34 tools (for example `get_investigation`, `list_events`, `list_components`, `get_run`, `get_claim`, `get_assessment`, `start_investigation`, ...), seven resource templates `lab://projects/{project}/{investigations|runs|claims|evidence-plans|assessments|charters}/...` and `artifacts/{id}/manifest`. There is **no list-projects tool, no resources/list entries, and no report resource**: the HTML report is `lab report`. The prompt therefore gives the project id and reads the investigation resource. Under the client-side key `eidos`, Claude Code names tools `mcp__eidos__<tool>`.

```bash
python3 scripts/qualify/mcp_clients/gen_configs.py --home "$EIDOS_HOME" --lab "$S/venvs/lab/bin/lab" --out "$S/mcp-configs"
bash scripts/qualify/mcp_clients/run_clients.sh --home "$EIDOS_HOME" --lab "$S/venvs/lab/bin/lab" \
     --project <project> --investigation <investigation> --out "$S/mcp-run"          # print mode: shows both commands
bash scripts/qualify/mcp_clients/run_clients.sh ... --run                           # executes Claude Code then Codex
```
`--run` writes `claude.receipt.json` and `codex.receipt.json`; exit status 1 if any check fails. Each receipt checks: client exit 0; real `get_investigation` and `list_components` tool calls in the client's event stream; the final text names the investigation id and the status the lab reports now (read via `lab query`); the lab has components. Claude's receipt also requires reading the investigation resource; for Codex that is recorded as advisory only (assumption: `codex exec` may not offer MCP resource reads to the model).

Flag assumptions (documentation was not reachable from the build sandbox; confirm with `claude --help` / `codex exec --help` on the Mac and adjust `run_clients.sh` if a flag differs):
* Claude Code: `-p`, `--mcp-config <file>`, `--strict-mcp-config`, `--allowedTools 'mcp__eidos__*'`, `--max-turns`, `--output-format stream-json --verbose` (stream-json needs `--verbose` with `-p`).
* Codex: `codex exec --json --skip-git-repo-check -c key=value ...` with TOML values: `mcp_servers.eidos.command="..."`, `mcp_servers.eidos.args=["mcp"]`, `mcp_servers.eidos.env={EIDOS_HOME="..."}`; JSONL events of type `mcp_tool_call`. Codex may ask for approval of MCP tool calls; in `exec` this normally auto-declines, so if the Codex receipt shows zero tool calls, add `-c approval_policy="never"` and re-run, and say so in the report.
* The server's only environment is `EIDOS_HOME`; the credential is the lab's admin token file. On macOS the clients spawn `lab` without your shell PATH: the configs use absolute paths.

**Claude Desktop (manual, later):** merge `$S/mcp-configs/claude-desktop-snippet.json` into `~/Library/Application Support/Claude/claude_desktop_config.json` yourself (the scripts never touch it), restart Claude Desktop, ask it to list components in project `<project>`, then `bash scripts/qualify/mcp_clients/check_desktop_log.sh`. PASS needs a successful `initialize` result (with `serverInfo`) and a `tools/list` result naming `start_investigation` in `~/Library/Logs/Claude/mcp-server-eidos.log`. The log wording is assumed from the MCP JSON-RPC traffic.

## 4. MPS vs CPU (c)
In a plain venv with torch and numpy only (the research venv will do):
```bash
$S/venvs/research/bin/python scripts/qualify/mps_smoke.py --require-mps --out $S/mps-receipt.json
```
It trains the switch-order `OrderedSwitchNet` (45 steps, seed 41) through `eidos_pytorch.training.train_loop` on cpu and on mps in separate processes with `PYTORCH_ENABLE_MPS_FALLBACK=1`, and records per device: torch version, `mps.is_available()`, the device the parameters lived on, wall time, final loss, evaluation accuracy on a fixed 4000-pair draw, a parameter checksum, warnings and any CPU-fallback messages. Pass criterion (printed in the receipt): |Δ final loss| ≤ 5e-3 and |Δ accuracy| ≤ 0.02; exact equality is not expected. `--require-mps` makes an unavailable MPS a failure. Linux CPU reference (this build sandbox): final loss 0.27577, accuracy 0.9840 (the reference number for this model at step 45). Unqualified until the Mac receipt shows `match_within_tolerance: true`; the lab itself never claims exact restore across devices.
Note: `train_loop(device=...)` moves the model only; the script moves batches because the example adapter builds them on the CPU.

## 5. Real-LLM steps (lead)

Keep the key only in the shell that runs these commands. Never put it in `lab.json`, a file, or `lab start` from another shell. The supervisor passes provider credentials only to the investigator worker. Jobs and model-chosen commands run with an allowlisted environment, and on macOS under `macos:seatbelt-sealed`.

**5.0 Security check.** Run it first, with no key needed:

```bash
pytest -q tests/integration/test_contained_commands.py
```

Run it from a checkout with the repo venv. It should report 4 passed on macOS, where Seatbelt is available.

**5.1 Lab-free driver re-run, after the security fix.**

```bash
cd research/public/development && $PY package_task.py --task D02 --out $SSD/bundles/D02 && cd -
ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src $PY scripts/qualify/live_bundle_investigation.py \
  --bundle $SSD/bundles/D02 --python $PY --out $SSD/receipts/live-D02.json --max-requests 40 --max-turns 8
```

- Repeat with `--task D12` for the null case.
- The receipt's `isolation` list must show `macos:seatbelt-sealed` for every command.
- Without a sealed profile the driver refuses to run, unless `--allow-same-user` is given; that waiver is recorded.

**5.2 Full-lab investigation on D02.**
- Precondition: the lab is initialised from the wheels with `EIDOS_HOME` on the SSD, and it is stopped.
- Command:

  ```bash
  PRICE='{"input": <USD/Mtok>, "output": <USD/Mtok>}' ANTHROPIC_API_KEY=... \
    bash scripts/qualify/full_lab_live.sh --bundle $SSD/bundles/D02 --python $PY --out $SSD/receipts/full-lab-D02.json
  ```

- Hard caps: 5 turns × 2 decisions × 4 requests = 40 requests, `model_usd=3` at PRICE, `model_tokens=600000`.
- The receipt contains the served model ids and per-decision usage.
- `key_found_in_lab_files` must be `[]`; if it is not, the script exits 1.

**5.3 Send back:**
- `live-D02.json`, `live-D12.json` and `full-lab-D02.json`;
- `lab logs investigator-worker --lines 400`;
- the controller's spend figures.

## 6. What to send back
`$S/mcp-run/*.receipt.json`, `$S/mps-receipt.json`, the `du`/`ls` output from step 1.3, `lab doctor --json`, the Desktop log check output (if done), and any flag corrections made to `run_clients.sh`.

## Validated on Linux only (author)
`gen_configs.py` output files; `run_clients.sh` print mode and `--run` against a live lab with stub `claude`/`codex` executables (receipts pass and fail correctly, including a stub that lists tools but never calls them); `verify_receipt.py`; `mps_smoke.py` on CPU (MPS reported unavailable, `ok` true, `--require-mps` exits 1). **Untestable here:** the real Claude Code, Codex and Claude Desktop (flag syntax, approval behaviour, event stream shape, log wording), MPS itself and its fallbacks, macOS bash 3.2 behaviour of `run_clients.sh` (written without `mapfile`), the SSD placement checks.
