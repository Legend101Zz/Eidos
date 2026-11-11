# P2S — prospective pilot-2 parity corrections

Completed 11 November 2026 before the 12:45 IST deadline on
`task/pilot-2-tooling`, after P2T. No model calls or pilot runs were made.
The corrections are informed by D1 §3 and apply prospectively to pilot 2;
pilot-1 files, records, grades and its stopped lab were not changed.

## Changes and verification

| Change | Implementation | Executed evidence |
| --- | --- | --- |
| Environment parity | `protocols.py` registers `lab` with `EIDOS_RESEARCH_PYTHON`, its inspected package versions/digest, and the task-only root, matching `default`. The separate fixed lab-service interpreter is unchanged. | Real D03 and D06 projects: matching interpreters and complete package locks; NumPy/Torch/SciPy/scikit-learn/joblib imports and supplied baseline training succeed in both environments. Each project passes all 10 assessor and 3 template controls. |
| Information parity | `arm4.py` embeds the unchanged `study/METHOD.md` in the same JSON text map as task files. `tools_arm4.md` names METHOD and adds arm 3's no-source baseline sentence verbatim. | Adapter-boundary fixture inspects the actual `start_investigation.question`; METHOD bytes and task brief match. Prompt test checks the shared baseline sentence. Arms 1–3 tool paragraphs and METHOD itself have no diff. |
| Final-outcome classification | `run_arm.py` tracks the last result event's error state instead of sticky intermediate errors; a successful result proceeds to answer validation even after a nonzero process exit. `common.classify` accepts a concluded investigation with a valid answer despite earlier failed requests/turns; an unfinished investigation remains a crash. Invalid concluded answers remain `invalid_output`. | Subprocess fixtures cover arms 1/2/3 and the arm-4 classifier: recovered request/result errors followed by a valid abstention are `ok`; missing/failed finals crash; invalid answers fail validation. Incident, timeout and token-budget precedence remain tested. Provider incident detection is unchanged. |
| Setup outside timer | Arm 4 writes `setup.json` at its existing investigation-clock boundary. `worker.py` copies setup duration/timestamps into every arm-4 run record, including failed preparation (`setup_complete=false`). `launcher.py` waits for that boundary and adds completed setup duration to arm 4's outer deadline. Arms 1–3 and stub deadlines are unchanged. | Actual worker/adapter fixture simulates 300 seconds of setup followed by 5 seconds of timed work under a 30-second envelope. Recorded `setup_wall_s=300.0`, `investigation_wall_s=5.0`, status `ok`; provider deadline starts after setup. Separate preparation-failure and outer-deadline checks pass. |
| Prospective charter and E2 merge | `charters.py` pins these setup/information/classification policies in `pilot_2_setup` for D03–D06, and documentation identifies the language for the forthcoming pilot-2 charter. D01/D02/D12 charter bodies have no new policy. | All four pilot-2 charters validate. AST comparison confirms `run` is the only changed function in `arm4.py`; its additions only embed METHOD and record setup. `export_answer` and its status mapping are unchanged for E2. |

Unused-import and line-layout cleanup in the touched runner/launcher files makes
the full targeted Ruff check pass; it changes no other behavior.

The machine-readable live evidence is
[p2s-parity-qualification.json](eidos_arms/evidence/p2s-parity-qualification.json).
Raw records are under `~/eidos-internal/p2s-qualification-final/`, with command
logs `~/eidos-internal/p2s-{study-tests,arm-tests,qualification-final,dry,lab-stop}.log`.
The first qualification attempt passed its controls and first baseline, then the
new verifier incorrectly expected the polling helper to include a full job lock.
The final verifier explicitly reads that lock and passes both complete projects;
the original attempt is retained under `p2s-qualification/` and its log.

## Commands and real output

Every shell sourced `~/eidos-internal/env.sh`. Installed-runtime checks explicitly
selected the pilot-2 interpreter and separate `~/eidos-internal/p2s-lab` home.

```sh
source ~/eidos-internal/env.sh
cd ~/eidos-internal/p2
export EIDOS_STUDY_HOME=~/eidos-internal/p2s-lab
export EIDOS_STUDY_PYTHON=~/eidos-internal/venvs/pilot-2-lab/bin/python
"$EIDOS_RESEARCH_PYTHON" -m unittest discover -s study/tests -v
"$EIDOS_STUDY_PYTHON" -m unittest study.eidos_arms.test_contract study.eidos_arms.test_parity -v
uvx ruff check study/eidos_arms study/run_arm.py study/launcher.py
git diff --check
```

```text
Ran 23 tests in 1.522s
OK
GRADE control arm 1: incident success=False

Ran 21 tests in 2.533s
OK

All checks passed!
```

The intentional grader-infrastructure control produces the `incident` line.
`git diff --check` prints nothing. All 44 tests pass; no test is skipped.

```sh
source ~/eidos-internal/env.sh
cd ~/eidos-internal/p2
export EIDOS_STUDY_HOME=~/eidos-internal/p2s-lab
export EIDOS_STUDY_PYTHON=~/eidos-internal/venvs/pilot-2-lab/bin/python
export EIDOS_STUDY_TEMPORAL_SOURCE='/Volumes/Mrigesh SSD/eidos-v1/a8lab/services/temporal/bin'
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.qualify_p2_parity \
  --out ~/eidos-internal/p2s-qualification-final
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab stop
```

```text
Investigator bundle prepared; references and test material excluded.
D03: default/lab research interpreters and package locks match; scientific imports + no-source baseline PASS in both; assessor/template qualifications passed; model_calls=0
Investigator bundle prepared; references and test material excluded.
D06: default/lab research interpreters and package locks match; scientific imports + no-source baseline PASS in both; assessor/template qualifications passed; model_calls=0
{"stopped": "/Users/comreton/eidos-internal/p2s-lab"}
Pilot-2 qualification lab phase: stopped
```

Both environments use `/Users/comreton/eidos-internal/venvs/research/bin/python`
with lock `sha256:868816875984ce55469c67c8886df34532c136f0399d3b2423b7c3c4f406c191`:
NumPy 2.5.4, Torch 2.15.0, SciPy 1.18.1, scikit-learn 1.9.1, joblib 1.6.0.
The fixed Eidos service build remains P2T's
`f4b2a853060294378f5e2635ce749fb51cf050eb`; its wheel hashes are in the evidence.
The 26 passing controls are authored operational controls, not independent
scientific confirmation. D04/D05 retain their documented RMSE-assessor gap.

```sh
source ~/eidos-internal/env.sh
cd ~/eidos-internal/p2
bash study/dry_run_p2.sh ~/eidos-internal/p2s-dry
```

```text
Planned 2 randomized paired blocks, 8 assignments.
BLOCK D04-r1: ok
BLOCK D03-r1: ok
GRADE D03-r1 arm 1: valid success=False
GRADE D03-r1 arm 2: valid success=False
GRADE D03-r1 arm 3: valid success=False
GRADE D03-r1 arm 4: valid success=False
GRADE D04-r1 arm 1: valid success=False
GRADE D04-r1 arm 2: valid success=False
GRADE D04-r1 arm 3: valid success=False
GRADE D04-r1 arm 4: valid success=False
Matplotlib is building the font cache; this may take a moment.
Draft analysis written; no confirmatory claim implied.
Wrote descriptive tables and 2 PNGs to /Users/comreton/eidos-internal/p2s-dry/analysis
DRY RUN PASSED: 8 valid baseline candidates, zero gain, zero successes, 2 plots; no model calls
```

`research/public/`, METHOD, and arms 1–3 tool paragraphs are unchanged.
The qualification lab is stopped. Root free space remained above 14 GiB
(latest check: 17,722,648 KiB available). No optional model smoke was run.
The private grading repository was unchanged.

## Pilot-2 charter language and timing fields

Record this prospective policy in the pilot-2 study charter before launch:
all investigator-selectable environments have the arms-1–3 research interpreter's
scientific capabilities; arm 4 receives the shared METHOD text and the same
factual no-authored-source baseline note as arm 3. Recovered intermediate errors
do not override a valid final result, symmetrically across arms, while existing
incident, timeout and token-budget rules retain precedence. These corrections do
not reclassify pilot-1 runs or change any scientific success bar.

Arm-4 setup precedes its investigation clock and is reported per run as
`setup_wall_s`, with `setup_started_at`, `setup_ended_at`, and `setup_complete`.
Setup includes lab startup/waiting, project import, charter registration,
component installation, protocol qualification and prompt assembly. The measured
interval uses a monotonic clock; timestamps are UTC. A failed setup records
elapsed preparation time and `setup_complete=false`. `wall_s` starts after
completed setup; `investigation_wall_s` retains the adapter's existing timed-work
measurement, including export/accounting up to its return. `adapter_wall_s`
includes the whole worker invocation, including setup and teardown. Provider
bridge startup remains within the timed interval, and no scientific work is
admitted after the deadline. The launcher excludes setup from arm 4's outer
deadline and keeps its existing 60-second teardown allowance. Per-run records
support separately reporting this setup overhead in the paper.

The existing automatic text embedding, controller-built exports/hashes and
preinstalled protocols remain arm-4 affordances and should be declared in the
charter. Adapter export/answer validation is covered by contract tests and the
stub block path; this ticket makes no new live-model completion claim. E2's new
claim-state mapping is a separate change to merge before the pilot is chartered.
