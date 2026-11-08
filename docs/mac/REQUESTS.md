# Requests from the local Mac session

## For the cloud session

1. **Review and merge b7d9ed1** (sealed jobs/assessor could read `$HOME` on macOS). Consider whether any receipt
   recorded before it under `macos:seatbelt-sealed` should be annotated (the `no-credential-read` control did not
   cover the home directory).
2. **Temporal download fallback**: on 2026-11-08 the GitHub browser-download URL for the pinned server returned 504 for
   hours while `gh release download` (API asset endpoint) worked. Consider a fallback in `services._download` to
   `https://api.github.com/repos/temporalio/temporal/releases/assets/<id>` with `Accept: application/octet-stream`
   (digest still pinned), or document `EIDOS_TEMPORAL_ARCHIVE_CACHE` in INSTALL_AND_OPERATE.
3. (fixed in e36bfb1) `lab init` printed its "next" command without shell quoting.

4. **`eidos.project.command` vs the lab-free driver** (found in the first full-lab real-LLM run): the component copies
   inputs into the work dir only through `config.files`; the lab-free driver defaults `files` to
   `{train: train.npz, validation: validation.npz, task: task.json}`. The same investigator call works lab-free and
   fails in the lab. Align them (default the mapping in the component, or stop defaulting in the driver and say so in
   the catalog summary).
5. ~~Failed `project.command` jobs carry `error: null`~~ **Retracted (2026-11-08, re-run task):** the ledger stores the
   failure in `job.terminal.error` (category `user-code`, message with the stderr tail) and the investigator's context
   included it; `error: null` came from this session's own receipt query, which read `attempts[-1].error` (attempt
   records have no `error` field). The cloud's a39ab1a feedback improvement (work-dir listing + stderr tail) still helps.
6. Test hygiene from the macOS suite run: `components/build_fixtures.py` rewrites two tracked fixtures when numpy is
   importable (`branch-no-change-exact.json`, `branch-undeclared-state.json` gain `"rng.numpy": "copy"`); the
   listener thread in `test_contained_commands.py` raised `ConnectionAbortedError` on macOS at teardown (warning only;
   made tolerant on the Mac branch); there is no pytest for the Seatbelt access probes (only `scripts/ci/seatbelt_probes.py`).
7. Matrix rows to update from `docs/mac/STATUS.md`: A1 (macOS researcher machine runtime-verified, same OS user), A8
   (real-LLM development runs exist: lab-free D02/D12 and one full-lab D02), A9 (OpenAI-compatible adapter path against a
   local endpoint; small model fails conformance), A13 (MPS runtime-verified; CUDA unqualified), A14 (Claude Code and
   Codex verified; Desktop unqualified).

8. **Investigation stalls when every effect call fails and the decision waits** (re-run, 2026-11-08): a decision with
   an `author_component` call that returns `INVALID` and control `wait_for_jobs` ends the turn `waiting` with nothing
   outstanding; failed calls are not news, so it idles until max idle (6 h). Treat a waiting turn whose calls all
   failed (or that has nothing outstanding after execution) like a refused-proposal turn (bounded immediate retry with
   the failure in the next view), or count failed call receipts as news.
9. **Document `environment` as a name** in the investigator context and `describe_component` for `author_component`
   / `validate_component` (the model passed `{"base": "project-root"}`), and reject a non-string at proposal
   validation so it is fed back within the turn.
10. **`author_component` means different things** in the lab (build a component: validate → publish → run) and in
   the lab-free driver (candidate project files for `run_component inputs.source`). The investigator in the full lab
   used the lab-free meaning (authored a replacement `project.py`). Align the driver with the lab, or document the
   whole-trainer path (`project.command` `files`/`source`) in the context.

11. **Show authored sources in the packed context** (re-run 2): list the investigation's `author_source` bundles
   (id, files, turn) in `context-policy/1`, or feed the call result (bundle id) back so a later decision can pass it as
   `inputs.source`. Today the id only exists as an `artifact.registered` event subject, and the packer drops events, so
   the investigator re-authored the same candidate 4 times and never ran it.
12. **A bounded no-effect wait with nothing outstanding still parks**: the runtime's `request-budget-exhausted`
   outcome is exempt from the idle refusal; when nothing is outstanding, give it the same bounded retry → needs-input
   path as other waits (it parked for >10 min until an operator stop).
13. `max_requests_per_decision=5` allowed 6 requests in one decision (pydantic-ai `request_limit` 4 + output retries);
   make the bound exact or document the +1, since the full-lab caps are computed from it.

## For the owner

1. **Claude Desktop** (not installed here): merge `docs/mac/receipts/a14-claude-desktop-snippet.json` into
   `~/Library/Application Support/Claude/claude_desktop_config.json` yourself, restart Desktop, ask it to list components
   in project `macq`, then run `scripts/qualify/mcp_clients/check_desktop_log.sh`. The snippet itself passed
   `check_desktop_snippet.py` (static + live stdio launch).
2. **Seatbelt vs Lima**: `macos:seatbelt-sealed` held every probe on this Mac (after b7d9ed1) but runs as the same OS
   user; decide whether that is acceptable or a Lima VM profile is needed.
3. **Ollama update cache**: `~/Library/Caches/ollama/updates/0x8DF3BA621AC1750/Ollama-darwin.zip` (218 MB on the
   internal disk) appeared at 16:26 while this session's private `ollama serve` (port 11535) ran; likely Ollama's
   updater. Not removed because it belongs to your Ollama install; delete it if you don't want the update.
4. The pulled model `qwen2.5:3b-instruct` (1.9 GB) is in `/Volumes/Mrigesh SSD/eidos-mac/ollama-models`; delete it when
   no longer needed. `$S/suite` (~3.5 GB of test scratch) can be deleted except `logs*/`.
