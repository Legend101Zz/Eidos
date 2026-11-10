# W1c final integration verification — 11 November 2026

All commands sourced `/Volumes/Mrigesh SSD/eidos-v1/env.sh`. All real model tasks
were `SYNTHETIC_LINEAR_SMOKE`, using MiniMax-M3.1-Flash only; D01/D02/D12 were
not given to a model. No `src/eidos`, `research/public` or `docs/design` file was
modified. The exact controller commands are in [PILOT_COMMANDS.md](PILOT_COMMANDS.md).
Machine-readable receipts: [W1c summary](eidos_arms/evidence/w1c-integration-summary.json).
Raw receipts/logs: `/Volumes/Mrigesh SSD/eidos-v1/pilot-1/w1c-checks/` and
`pilot-1/w1c-*.log`.

## Fixed build and charter

`bash study/eidos_arms/build_lab.sh` executed the requested
`uv build --all-packages -o "/Volumes/Mrigesh SSD/eidos-v1/dist-pilot-1"`, created
`/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1-lab`, and installed four new wheels.
The build source commit is `ba79b9144b594093fe18df57b1a9dfdc2042cba1` (contains
ADR 0016). Subsequent commits change study integration only; the Eidos build inputs
are identical. Installed versions: lab 1.1.0, wire 1.1.0, worker SDK 1.2.0,
PyTorch package 1.1.0. Wheel hashes are in the summary and venv's `pilot-build.json`.
The direct installation audit printed:

```text
Fixed build verified: 88 Eidos Python source files match installed wheel; lab_python matches new venv.
```

D02/D12 receive a prospective 95% paired-difference-CI rule with minimum effect
.02, ordinary accuracy encoded through `paired-report/1:reference-accuracy`.
D01 remains operational/exploration-only. See [CHARTER_RULES.md](eidos_arms/CHARTER_RULES.md)
for the exact rule, paired-row format and held-out/fresh-initialization gaps.
The live smokes carry this rule; neither launched an evidence plan or independent
assessor. Each project did execute and pass both protocol qualification gates
(author controls, not independent scientific qualification).

Both study project policies explicitly disable the component LLM broker. Initial
policies and post-run profile audits confirm `enabled=false`; every job lock was
checked and had no component LLM grant. The internal investigator's ordinary model
calls are separately metered under the common study envelope.

## Executed commands and actual outputs

```sh
bash study/dry_run.sh "/Volumes/Mrigesh SSD/eidos-v1/pilot-1/w1c-checks/dry-run"
```

```text
Planned 9 randomized paired blocks, 36 assignments.
BLOCK D02-r1: ok
GRADE D02-r1 arm 1: valid success=False
GRADE D02-r1 arm 2: valid success=False
GRADE D02-r1 arm 3: valid success=False
GRADE D02-r1 arm 4: valid success=False
Draft analysis written; no confirmatory claim implied.
Wrote descriptive tables and 2 PNGs to /Volumes/Mrigesh SSD/eidos-v1/pilot-1/w1c-checks/dry-run/analysis
CHECK: 4 valid baseline candidates, zero improvement successes, 2 plots
DRY RUN PASSED: baseline candidates graded; no model calls.
```

All four stubs render their own card prompt through the shared function. Their
recorded execution intervals overlap for 3.895652 seconds. The launcher creates all
four processes before waiting. Real arm 3/4 execution also overlapped for
112.784488 seconds in separate UUID projects on `pilot-1/lab/fixed` (the arm-3
attempt in that overlap later timed out; it remains a timeout). Startup locking
does not serialize model work.

```sh
"$EIDOS_RESEARCH_PYTHON" -m unittest discover -s study/tests -v
"/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1-lab/bin/python" -m unittest study.eidos_arms.test_contract -v
uvx ruff check study/eidos_arms study/tests/test_prompts.py
git diff --check
```

`study` suite:

```text
Ran 19 tests in 0.528s

OK
GRADE control arm 1: incident success=False
```

`contract` suite:

```text
Ran 10 tests in 1.557s

OK
```

The `GRADE control ... incident` line is the deliberate grader-infrastructure
failure test. Ruff printed `All checks passed!`; `git diff --check` printed nothing.
Prompt parity tests cover all four arms and all three cards; the new contract test
uses the installed lab's real plan compiler and verifies threshold overrides fail.

```sh
"/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1-lab/bin/python" -m study.eidos_arms.smoke --arm 4 --run-dir "/Volumes/Mrigesh SSD/eidos-v1/pilot-1/w1c-checks/arm4-fixed" --wall-s 660
"/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1-lab/bin/python" -m study.eidos_arms.smoke --arm 3 --run-dir "/Volumes/Mrigesh SSD/eidos-v1/pilot-1/w1c-checks/arm3-fixed-retry" --wall-s 275
```

The real commands emitted full JSON run records. The post-run artifact/provenance
verification printed this summary of those records:

```text
ARM 3: status=ok; wall_s=135.787; adapter_wall_s=146.757; tokens=546362; artifacts=valid; broker=off
ARM 4: status=ok; wall_s=113.384; adapter_wall_s=122.266; tokens=101935; artifacts=valid; broker=off
```

Adapter wall includes project preparation/export and remains below 5/12 minutes.
Arm 4 accepted two schema-valid decisions; one preceding malformed proposal was
rejected and corrected, preserved in the ledger. It completed a training job,
locked `job_2e7ecc1f2edc42f4`, and exported an artifact-valid answer. It reports
58/60 visible-validation accuracy (0.9666667), operational reproduction only.
There are nine successful project jobs: eight setup/validation/qualification jobs
and the investigator's one training job. Evidence plans: **0**; assessments: **0**.
Its JSON settings stayed `max_output_tokens=16384`, `request_timeout=300`;
packaged text remained embedded in the question and read requests were available.
Arm 3 completed the MCP training/artifact path and its own valid answer.
Observed billing is unknown (`null`) for both; the token receipts are not invoices.

The final setup review tightened the lab qualification environment's project root
from the checkout to the packaged task (the training environment already used the
task). A model-free rerun of both real qualification gates printed:

```text
TASK-ROOT CHECK: both environments use packaged task; 2 protocol qualifications passed; model_calls=0
```

No additional model calls were needed for that administrative root change.

## Retained failures and shutdown

The first two attempts, `arm3-smoke` and `arm4-smoke`, were stopped and disqualified
when inspection found the pre-existing lab directory's config still pointed to
`pilot-1b-110`. Their intended-build fields were incorrect for the running services;
original receipts are preserved as `run_record.before-operator-audit.json`, alongside
operator disqualification and corrected incident records. The actual old-wheel
source SHA is unknown, so it is explicitly null. Observed tokens were 218,492
(arm 3) and 13,953 (arm 4), with unknown final billing. These are not fixed-build
successes. The new default home is `lab/fixed`; startup now rejects an interpreter
mismatch instead of silently reusing the persisted interpreter.

The first genuine fixed-build arm-3 attempt, `arm3-fixed`, timed out at 240.179 s
(248.517 s including setup), with 1,111,578 observed tokens. Training/artifacts
completed but no answer did. Its transcript showed it searching for an unnecessary
lock API. Only the synthetic-smoke instructions were clarified: choosing/citing the
successful job locks its selection; there is no `lock_job` command. The retry above
completed. Study prompts changed only by the required practical-bar marker.

Both lab homes were stopped. After the final model-free qualification check,
shutdown again verified phase `stopped`, no recorded service PIDs alive and no
listeners on API/MCP/PostgreSQL/Temporal ports. Internal free space remained above
12 GiB. No lab, PostgreSQL or Temporal process started here was left running.

GitHub submit/fetch commands are documented for the controller; no candidate was
submitted and no study-card model run was launched by this integration ticket.

---

# Lane A verification — 11 November 2026

All shell commands sourced `/Volumes/Mrigesh SSD/eidos-v1/env.sh`. Source changes
are confined to `study/`; research/design sources were read only. No study-card
model runs were launched. No lab, PostgreSQL or Temporal process was started.
The root filesystem had 24,719,720 KiB free at the recorded intermediate check,
above the 12 GiB stop threshold.

## Executed checks and actual outputs

`$EIDOS_RESEARCH_PYTHON -m unittest discover -s study/tests -v`:

```text
Ran 17 tests in 0.534s
OK
```

Coverage includes cumulative/partial/cache token accounting, duplicate message
IDs, child-key redaction, wall/token stops, provider errors, deterministic paired
allocation, model changes/missing IDs, D01/D02/D12 rules, reproduction arguments,
symlink rejection, infrastructure incidents and the grading timing gate.

`study/sandbox/probe.py --run-dir .../lane-a-checks/sandbox`:

```text
DENIED /Volumes/Mrigesh SSD/eidos-v1/wt-dev-pilot-1a/README.md
DENIED /Volumes/Mrigesh SSD/eidos-v1/pilot-1/grading
DENIED /Users/comreton/.ssh
WRITE_OK TORCH_OK 2.15.0
```

`study/toolkit/verify.py --run-dir .../lane-a-checks/toolkit-hardened`:

```text
eidos.pytorch.train succeeded
eidos.pytorch.evaluate succeeded
lab.pairs.by_group succeeded
eidos.assessors.paired-ci succeeded
eidos.qualification-runner succeeded
```

Those are real worker SDK invocations from copied sources inside Seatbelt. The
assessor and qualification runner require the small pure-domain dependency closure
now exported under `toolkit/support/`; a lab/server install is not needed.
The final no-network CLI startup probe printed
`CLAUDE_STARTUP_OK network=DENIED provider_calls=0 partial_events=enabled`.
The completed live transcript audit printed `{"transcripts": 1, "flagged": 0}`.

`bash study/dry_run.sh '/Volumes/Mrigesh SSD/eidos-v1/pilot-1/lane-a-dry-run-final'`:

```text
Planned 9 randomized paired blocks, 36 assignments.
BLOCK D02-r1: ok
GRADE D02-r1 arm 1: valid success=False
GRADE D02-r1 arm 2: valid success=False
GRADE D02-r1 arm 3: valid success=False
GRADE D02-r1 arm 4: valid success=False
Draft analysis written; no confirmatory claim implied.
Wrote descriptive tables and 2 PNGs to /Volumes/Mrigesh SSD/eidos-v1/pilot-1/lane-a-dry-run-final/analysis
CHECK: 4 valid baseline candidates, zero improvement successes, 2 plots
DRY RUN PASSED: baseline candidates graded; no model calls.
```

The earlier repeated dry run also printed `SKIP D02-r1: complete (ok)`, verifying
the completed-block resume behavior. Matplotlib 3.11.2 was installed in the shared
research venv using `uv pip --python ...`; its cache/temp stayed on the SSD.

`study/tests/qualify_grading.py --out .../lane-a-checks/grader-qualification-final`:

```text
GRADE control-D01 arm 1: valid success=True
GRADE control-D02 arm 1: valid success=True
GRADE control-D12 arm 1: valid success=False
GRADE control-D12 arm 1: valid success=False
GRADE control-D12 arm 1: invalid_output success=False
QUALIFIED: D01 equality, D02 positive + two fresh seeds, D12 honest null, false claim and missing output
```

The authored D02 positive control scored baseline `.8583333333333333`, candidate
`.9805555555555555`, gain `.12222222222222223` on 360 test cases. Fresh seed gains
were `.11388888888888893` (23) and `.1166666666666667` (37). These are explicitly
authored development controls, not autonomous research results. D12 honest reporting
had `null_correct=true` and zero primary success; its deliberately positive control
set `false_positive_claim=true`.

## Live smoke

`study/live_smoke.py --run-dir .../lane-a-checks/live-toy-system-paths` used arm 1,
MiniMax-M3.1-Flash and a tiny authored one-dimensional regression. It was capped
at 300 seconds and did not use D01/D02/D12.

```text
LIVE SMOKE status=ok tokens={'input': 19336, 'cache_read': 290313, 'cache_write': 0, 'output': 11903, 'total': 321552} wall_s=172.57
structural check passed; artifacts, security, costs and science not independently verified
```

Replaying its transcript through the current meter reproduced the exact same
token map and returned model set `['MiniMax-M3.1-Flash']`. Its verbatim final claim
has 2,027 characters. Billing remains unknown (`null`), not an invented zero.

Three preceding bootstrap attempts are retained alongside it: `live-toy` stopped
on Claude's default `/tmp/claude-501` write; `live-toy-verified` hit the 300-second
timeout with no stream events; `live-toy-final` was stopped after 86.44 seconds with
no stream events. Their recorded usage is zero; these are startup failures, not
research outcomes. Setting `CLAUDE_CODE_TMPDIR` and permitting the remaining system
library/database paths resolved startup. The final profile retains other-process
and credential-broker denials; offline startup probes initialized with both denials
and network prohibited, without making another paid model call.

The live run showed final cumulative token reporting. The current runner also
requests `--include-partial-messages` for enforcement before that final summary;
the partial-message meter has synthetic tests, and final CLI startup with this flag
is checked by the no-network `sandbox/cli_probe.py`.

## Remaining boundaries

- Lane B supplies arms 3/4 and replaces their tools-paragraph placeholders. They
  have not been live-tested by this lane; stubs exercised their launcher slots.
- Model-backed synthetic generation needs an equal scoped grant/broker across
  arms. No such grant is supplied here; see `toolkit/COVERAGE.md`.
- No hard memory ceiling or hostile-code VM qualification is claimed. Thread
  settings and task code implement the CPU rule; Seatbelt enforces tested file
  access, writes, process information and credential-broker restrictions.
- Ordinary-project fresh reload uses the documented adapter because the immutable
  reference-only `fresh_check.py` cannot load the investigator bundle's format.
- Natural-language claim scope and transcript flags need controller review.
  Historical incident costs remain in archived receipts. This tiny development
  pilot and the draft interval do not support confirmatory superiority claims.
