# End-to-end journey from a clean wheel install (development run)

Status: hosted-Linux **development** run, CPU, Python 3.12.11, torch 2.15.0+cpu, Postgres 16, Temporal 1.32.2. It shows that a lab installed
from freshly built wheels can be driven through the `lab` CLI and the official MCP SDK client against one lab directory. It makes no
scientific or capability claim. Every investigator decision is a **FIXTURE** policy (`bundle-baseline-replicate`, `conclude-immediately`):
deterministic scripts, no model, no provider call.

Rerun: `bash scripts/journeys/e2e_journey.sh` (see the header for env; CI job `e2e-journey`). Driver: `scripts/journeys/e2e_journey.py`
(runs with the installed venv's python; the checkout's `src/` is not importable). Machine-readable record: `$CI_WORK/out/journey-record.json`
(step, interface, status, ids, detail, seconds). Local run: 8/8 steps pass, wall time 82 s including the wheel build (research env pre-built).

| step | interface | status | s | ids |
|---|---|---|---|---|
| 1 lab init / start / doctor on a clean wheel install | lab CLI | pass | 9.4 |  |
| 2a package D01 bundle (research owner script, research/public untouched) | research package_task.py | pass | 2.4 |  |
| 2b project create, task import, install generic components | lab CLI | pass | 7.4 | charter=`charter:d01-dev`, eidos.project.command=`eidos.project.command@1.0.6`, eidos.task.score-classification=`eidos.task.score-classification@1.0.1`, task=`art_1a657f429d4a4f49`, train=`art_224ce90fe5c94707`, validation=`art_001554f2066c401d` |
| 2c FIXTURE investigation bundle-baseline-replicate to conclusion | lab CLI | pass | 14.2 | investigation=`inv_5777b4573a2b`, score_jobs=["job_cbe324582726465b", "job_8b8b81e4832d4447", "job_1aeca143de3f4a26"], train_jobs=["job_1e25033425d741d1", "job_4ed50680c7814ac3", "job_36bcfea374e84385"] |
| 2d task answer export + official validate_answer.py | lab CLI + research validate_answer.py | pass | 4.0 |  |
| 3 switch-order project: train, checkpoint branch, fresh-process behaviour check, model export | lab CLI | pass | 33.6 | behavior_job=`job_7a7520ec9e4d4354`, branch_checkpoint=`art_8972102362074bbf_checkpoint`, branch_job=`job_8972102362074bbf`, export_id=`export_605870b4e4c9`, parent_checkpoint=`art_232539bc2eb8432f_checkpoint`, project=`sw1`, wrong_builder_job=`job_efb68f0abfb04ca0` |
| 4 MCP SDK client: tools, read D01, start investigation, disconnect, reconnect, read back, components | MCP stdio (mcp.Client) | pass | 3.0 | components_listed=`True`, d01_events_via_mcp=`124`, mcp_inv=`inv_0dea395195b5`, tools=`34` |
| 5 HTML report, status, stop | lab CLI | pass | 3.7 |  |

## Which interface did what

* **`lab` CLI**: init/start/doctor, project create, `task import`, component install, `investigate start`, `task answer` (with the release's
  `validate_answer.py`), the switch-order project (`run`, `checkpoint branch`, `job wait`, `export`), `report`, `stop`.
* **MCP SDK client** (`mcp.Client` over stdio to `lab mcp`, admin token file): `list_tools` (34 tools), `get_investigation` and
  `list_events` on the D01 investigation started by the CLI, `start_investigation` (second fixture investigation, `conclude-immediately`),
  disconnect, a new session, read the investigation back by tool and by `lab://` resource, `list_components`, and a lost-reply retry
  of the same `command_key` that replays the original receipt. The CLI then reads the MCP-started investigation (concluded).
* **Research owner scripts, run from the untouched `research/public/development/`**: `package_task.py --task D01`, test-input export (inputs
  only, no labels), `validate_answer.py` (structural check only; its own disclaimer says artifacts, security, costs and science are not verified).
* Nothing in the journey uses `LabClient` directly or touches the database.

## D01 (whole-project command)

Three seeds were trained by `eidos.project.command` and scored on the visible validation split by `eidos.task.score-classification`
(seven turns, 3 train + 3 score jobs, validation accuracy 1.0 / 0.9667 / 1.0). The answer export reloads the exported model in a fresh process
(30/30 identical predictions) and passes `validate_answer.py`. Status `no_improvement_found`, claim type `operational_reliability`.

## Checkpoint branch and export on the switch-order project

D01 runs through one whole-project command and has no branchable checkpoints, so this part uses `examples/switch-order-project` in the same lab:
train `OrderedSwitchNet` 60 steps; `lab checkpoint branch` from the committed final checkpoint (restore class `exact`, continuing to step 90);
`eidos.pytorch.behavior-check` in a fresh process (match); `lab export` (record plus local `checkpoint.pt`/`eidos-export.json`); the same export
with a wrong-builder check is refused. All through the CLI.

## Not covered, and defects found while building the journey

* **Qualified assessment is not covered.** `tests/journeys/switch_journey.py --steps 1,3,4,5` does not run on this checkout, so the journey does
  not use it. Its blockers: (1) it pins `eidos.pytorch.train@1.0.0` but the checkout ships 1.2.0; (2) `lab project create` installs the four lab-env
  built-ins, then step 1 reinstalls them: **`lab component install` of an already published component fails with `COMMAND_KEY_CONFLICT`** (publish
  key `publish:<bundle artifact id>` is reused with a new validation job id), a CLI idempotency defect reproducible with `lab project create p;
  lab component install p components/eidos.assessors.paired-ci --env lab`; (3) step 1's published-extension checks (`python_path` for
  `limited_adapter`/`unscale_probe`) fail with `No module named ...`; (4) step 3 reads `artifact.meta.checkpoint` of the *intermediate* slot
  `checkpoint-step-00000045`, but the lab now registers only declared checkpoint slots (`checkpoint`) as branchable (`launcher.py`: role downgraded to
  `output`), so branching from an intermediate checkpoint is refused with "not a committed checkpoint manifest".
* The journey installs the two switch components from the checkout via `tests/journeys/staging.py` (binary `.ckpt` fixtures cannot travel in the
  CLI's text bundle). The D01 components come from the wheel (`lab component builtins`).
