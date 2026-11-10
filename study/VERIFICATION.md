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
