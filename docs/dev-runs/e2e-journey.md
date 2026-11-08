# End-to-end journey from a clean wheel install (development run)

Status: hosted-Linux **development** run, CPU, Python 3.12.11, torch 2.15.0+cpu, Postgres 16, Temporal 1.32.2. It shows that a lab installed
from freshly built wheels can be driven through the `lab` CLI and the official MCP SDK client against one lab directory. It makes no
scientific or capability claim. Every investigator decision is a **FIXTURE** policy (`bundle-baseline-replicate`, `conclude-immediately`):
deterministic scripts, no model, no provider call.

Rerun: `bash scripts/journeys/e2e_journey.sh` (see the header for env; CI job `e2e-journey`). Driver: `scripts/journeys/e2e_journey.py`
(runs with the installed venv's python; the checkout's `src/` is not importable). Machine-readable record: `$CI_WORK/out/journey-record.json`
(step, interface, status, ids, detail, seconds). Local run: 9/9 steps pass, wall time 122 s including the wheel build (research env pre-built). This run needed the one-line-class `evidence.py` fix listed under defects (applied locally, not part of this commit).

| step | interface | status | s | ids |
|---|---|---|---|---|
| 1 lab init / start / doctor on a clean wheel install | lab CLI | pass | 9.4 |  |
| 2a package D01 bundle (research owner script, research/public untouched) | research package_task.py | pass | 2.4 |  |
| 2b project create, task import, install generic components | lab CLI | pass | 7.4 | charter=`charter:d01-dev`, eidos.project.command=`eidos.project.command@1.0.6`, eidos.task.score-classification=`eidos.task.score-classification@1.0.1`, task=`art_1a657f429d4a4f49`, train=`art_224ce90fe5c94707`, validation=`art_001554f2066c401d` |
| 2c FIXTURE investigation bundle-baseline-replicate to conclusion | lab CLI | pass | 14.2 | investigation=`inv_5777b4573a2b`, score_jobs=["job_cbe324582726465b", "job_8b8b81e4832d4447", "job_1aeca143de3f4a26"], train_jobs=["job_1e25033425d741d1", "job_4ed50680c7814ac3", "job_36bcfea374e84385"] |
| 2d task answer export + official validate_answer.py | lab CLI + research validate_answer.py | pass | 4.0 |  |
| 3 switch-order project: train, checkpoint branch, fresh-process behaviour check, model export | lab CLI | pass | 33.6 | behavior_job=`job_7a7520ec9e4d4354`, branch_checkpoint=`art_8972102362074bbf_checkpoint`, branch_job=`job_8972102362074bbf`, export_id=`export_605870b4e4c9`, parent_checkpoint=`art_232539bc2eb8432f_checkpoint`, project=`sw1`, wrong_builder_job=`job_efb68f0abfb04ca0` |
| 3b | lab CLI (`lab cmd` / `query` / `run` / `artifact`) | pass | 39.1 | assessment=`assess_ca8f6f15134024976327726d`, outcome=`inconclusive`, conformance=`conformant`, effect=0.64525, claim=`claim_a4cd6d0a8825`, plan=`plan:sw1`, assessor qualification=`qual:eidos.assessors.paired-ci@1.0.0:7ee0ed3c`, template qualification=`qual:eidos.templates.improves-under-budget@1.1.0:34e5d073`, 6 evidence jobs |
| 4 MCP SDK client: tools, read D01, start investigation, disconnect, reconnect, read back, components | MCP stdio (mcp.Client) | pass | 3.0 | components_listed=`True`, d01_events_via_mcp=`124`, mcp_inv=`inv_0dea395195b5`, tools=`34` |
| 5 HTML report, status, stop | lab CLI | pass | 3.7 |  |

## Which interface did what

* **`lab` CLI**: init/start/doctor, project create, `task import`, component install, `investigate start`, `task answer` (with the release's
  `validate_answer.py`), the switch-order project (`run`, `checkpoint branch`, `job wait`, `export`), `report`, `stop`.
* **MCP SDK client** (`mcp.Client` over stdio to `lab mcp`, admin token file): `list_tools` (34 tools), `get_investigation` and
  `list_events` on the D01 investigation started by the CLI, `get_assessment` for the step 3b assessment (same outcome as the CLI), `start_investigation` (second fixture investigation, `conclude-immediately`),
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

## Qualified assessment (step 3b), on the same switch-order project

All through the CLI (`lab cmd <command> --payload ... -p sw1`, `lab query`, `lab run`, `lab artifact`): commit the charter; a promotion without a
qualification receipt is refused (`QUALIFICATION_MISSING`); register the paired-ci author control suite and qualify the assessor with the lab's
qualification runner (all 7 planted/null controls executed and passed; the receipt flags the suite as an author suite, not independent); promote
it; qualify the `improves-under-budget` template with the journey's runner (planted, identical-arms and smuggled-threshold controls) and
promote it; file a claim; expand the template into a draft (15 jobs planned, no open obligations); draft the plan against charter revision 1
(rule copied from the charter; a `min_effect` override is refused with `THRESHOLD_OVERRIDE`); resolve arms to one evaluate run per (arm, seed)
over checkpoints trained for seeds 41/53/67 (every checkpoint from the declared final `checkpoint` slot, then `eidos.pytorch.weight-edit`);
freeze; launch; six evidence jobs succeed; the AssessmentWorkflow records the assessment. Outcome **inconclusive** (conformance conformant,
paired both-correct effect 0.645 over three seeds), the same outcome the earlier switch journey recorded. Any of the three outcomes would
be a legitimate result of this step; it checks that the success rule is the charter's and that the outcome is recorded, not what it is.
The plan runs with an `isolation_waiver` because same-user execution is weaker than the available `linux:bwrap-sealed` profile; the waiver is
visible in the plan. The numbers are development evidence, not a research result. The original journey's withdrawal drill is not repeated.

## Defects found by this journey (not fixed here; `src/` is out of scope)

1. **`submit_assessment` fails for every evidence plan** (`src/eidos/application/handlers/evidence.py:322`): `launch.jobs` now maps an arm to a
   *list* of job ids (pipeline arms), but the loop treats the value as one id, so `get_job(list)` returns `None` and the handler raises
   `TypeError: 'NoneType' object is not subscriptable` (HTTP 500 in `assess_plan`; the workflow gives up after five retries and no assessment is
   recorded). Evidence: assessor-worker and api logs of the failing run; plan record `launch.jobs = {"baseline-s41": ["job_..."], ...}`.
   Fix that makes the journey pass: `for _arm_id, jids in plan.body["launch"]["jobs"].items(): for jid in jids if isinstance(jids, list) else [jids]:`
   around the existing loop body.
2. **`lab doctor` reports `secret-permissions` as failed after any sealed assessment.** `orchestration/activities.py:186` creates
   `config/secrets/assessor-label-canary` with the default mode (0644), and doctor requires every file in that directory to be 0600. The file
   is deliberately readable (a 0600 canary would make the sandbox's denied-read probe trivial; chmod 0600 was tried and the file was still
   0644 in the run, so it was not pursued), so the canary belongs outside `config/secrets` or doctor should exempt it. The journey tolerates
   exactly this failure, only while that file is the sole offender, and records it.

## Not covered

* The original journey's withdrawal drill, and `tests/journeys/switch_journey.py` steps 1 and 3 on this checkout (drift: published-extension
  checks with `python_path` modules; the default-env reinstall of lab-env built-ins now conflicts on content; branching from an intermediate
  checkpoint slot, which is no longer allowed). This commit only updates that file's pinned train ref and its plan's isolation waiver.
* The D01 components come from the wheel; the switch components are installed from the checkout via `tests/journeys/staging.py` (binary `.ckpt`
  fixtures cannot travel in the CLI's text bundle).
