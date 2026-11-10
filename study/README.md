# Development pilot tooling

All commands below run from the merged checkout, after sourcing the shared SSD
environment. This is development evidence, not a confirmatory study.

```sh
source "/Volumes/Mrigesh SSD/eidos-v1/env.sh"
pilot_root="/Volumes/Mrigesh SSD/eidos-v1/pilot-1"
"$EIDOS_RESEARCH_PYTHON" study/launcher.py plan --seed 20261111 --out "$pilot_root/design.json"
"$EIDOS_RESEARCH_PYTHON" study/launcher.py run --design "$pilot_root/design.json" --runs-root "$pilot_root/runs" --block 1
```

`plan` writes the analysis assignment roster: 36 rows, nine randomized blocks.
`--block` accepts the 1-based randomized order or a stable ID such as `D02-r1`.
Omitting it runs all blocks sequentially, with four arms concurrent inside each.
The launcher gives arms 2–4 the identical `METHOD.md`, packages each task with
`package_task.py`, and adds the common structural answer interface to each bundle.
Arms 3/4 need lane B's `study.eidos_arms` exports and tools paragraphs; otherwise
the dispatcher explicitly records “not implemented” as an infrastructure incident.

Complete blocks are skipped. To replace an incident, repeat the same run command
with `--replace-incident`. It archives the entire previous block under
`runs/incidents/` before starting all four arms again. Interrupted blocks are also
archived as incidents on resume. No successful/nonincident block can be rerolled
through that flag. Archived run records retain transcripts, token use and costs.

Run grading only after a whole block has ended:

```sh
"$EIDOS_RESEARCH_PYTHON" study/grade_block.py --block-record "$pilot_root/runs/D02-r1/block_record.json" --grading-root "$pilot_root/grading" --results "$pilot_root/results.json"
"$EIDOS_RESEARCH_PYTHON" study/analyze.py --design "$pilot_root/design.json" --results "$pilot_root/results.json" --out "$pilot_root/analysis"
```

Each grading run writes `grading/BLOCK/armN/grade.json`; the results roster updates
atomically under a lock. A replacement preserves prior grading under
`grading/incidents/`. A grader infrastructure failure marks the entire block as an
incident. Current results contain one outcome per assignment; historical incident
costs remain in the archived records and must be included in the controller's full
cost audit. The descriptive resource tables cover the current assignment receipts.

The grader validates structure and artifact hashes, rejects links/special files,
and snapshots the whole candidate bundle. It regenerates trusted inputs and trains
the original supplied baseline with seed 11; it never trusts the agent's potentially
edited task directory as baseline source. Candidate inference has read-only source
and inputs, no network, and no API key. Only inference inputs, never test labels,
enter its sandbox. `grade.py --split test` runs separately with labels and does not
import candidate code.

`study/fresh_check.py` adapts the fresh-process equality check to the ordinary
submitted project's train/infer CLI. The immutable public `fresh_check.py` hard-codes
`ProjectModel` and a reference-only `config.name`; it cannot load even the supplied
investigator bundle's `Model` format. The adapter checks the locked validation
predictions against a fresh load, then checks two independent test inference
processes for exact behavior equality. Candidate source is never imported into the
controller. Recipes use the declared reproduction command with evaluator-owned
input/output/seed arguments; the shared prompt documents the interface.

- D01: verified operational import/export equality; `primary_eligible=false`.
- D02: a declared supported improvement, test gain at least the card's `.02`,
  immutable test inputs, valid predictions and fresh reload equality. Recipe and
  architecture claims must also clear the gain bar at fresh seeds 23 and 37.
  These cards specify no critical slices; the protocol's explicitly draft slice
  defaults are not silently adopted. Broad natural-language claims still need the
  controller's scope review; the automated scope check covers declared claim types.
- D12: `null_correct`/`card_success` records honest nonpositive reporting.
  `verified_success` is always false, so null correctness never becomes a primary
  improvement success. A positive claim sets `false_positive_claim=true`.

Analysis calls the immutable `analyze_results.py` and writes CSV/JSON/Markdown
tables, `success_by_arm_card.png`, `resources_by_arm.png`, and a transcript term
audit. Only D02 is primary eligible; missing assigned outcomes remain in denominators.
The draft interval is exploratory. Unknown billing remains null. Matplotlib is
the only additional analysis dependency (`requirements-analysis.txt`); it has been
installed in the shared research environment for the controller.

## Isolation and accounting

Arms 1/2 run the required bare MiniMax Claude command, adding
`--include-partial-messages` so message-start and message-delta usage is visible
before the final cumulative summary. The meter deduplicates assistant message IDs
and reconciles cumulative result usage without double counting. Cache reads and
cache creation count toward the 8,000,000 processed-token ceiling. The first wall
or token limit stops the process tree; provider 429/5xx/quota errors are incidents.
Only the launched process and its descendants/session group members are signaled,
individually by PID. Child keys are redacted if they appear in stream output.
The final result text is retained verbatim as `agent_claim` (except key redaction).

Seatbelt permits data reads only in the run directory, the read-only research venv,
node installation and system paths. It denies writes outside the run directory
and devices, host-service process launch, other-process information and credential
broker lookup. `HOME`, all caches and both `TMPDIR` and `CLAUDE_CODE_TMPDIR` point
inside the run. System `/Library` and `/private/var/db` reads are needed for this
native Claude build to initialize. Network remains allowed for the model API;
public-source fetching is a residual risk and requires transcript review.

`OMP_NUM_THREADS=MKL_NUM_THREADS=1` (plus OpenBLAS/Accelerate equivalents) and the
task's `torch.set_num_threads(1)` provide the CPU thread rule. macOS does not supply
a cgroup-style per-run CPU/memory cap here: there is **no hard memory ceiling**,
and malicious code can override thread settings. Memory remains a planning/best
effort limit; Seatbelt is the tested filesystem boundary, not a qualified VM.

## Verification and smoke commands

```sh
"$EIDOS_RESEARCH_PYTHON" -m unittest discover -s study/tests -v
"$EIDOS_RESEARCH_PYTHON" study/sandbox/probe.py --run-dir "$pilot_root/lane-a-checks/sandbox"
"$EIDOS_RESEARCH_PYTHON" study/sandbox/cli_probe.py --run-dir "$pilot_root/lane-a-checks/offline-startup-final"
"$EIDOS_RESEARCH_PYTHON" study/toolkit/verify.py --run-dir "$pilot_root/lane-a-checks/toolkit-final"
bash study/dry_run.sh "$pilot_root/lane-a-dry-run-final"
```

The CLI probe has network denied and makes zero provider calls. The dry run uses
baseline stubs for all arms, then grades and analyzes one D02 block without model
calls. Grader qualification is separate authored development evidence:
`study/tests/qualify_grading.py --out FRESH_SSD_DIRECTORY` checks D01, a positive
D02 recipe with fresh seeds, the null, a deliberately false claim and missing output.

The single completed live arm-1 smoke used a tiny regression fixture, **not a study
card**, with a 300-second ceiling. Its receipt is in
`pilot-1/lane-a-checks/live-toy-system-paths/`. Do not rerun it just to repeat the
offline checks; `study/live_smoke.py --run-dir FRESH_SSD_DIRECTORY` makes paid
MiniMax calls. See `VERIFICATION.md` for the actual outputs and startup attempts.

The pilot explicitly disables component LLM-broker grants in every arm.
See `PILOT_COMMANDS.md` for the complete controller sequence and
`eidos_arms/CHARTER_RULES.md` for the prospective Eidos rule.

See `toolkit/COVERAGE.md` for every public component and the model-generator grant
gap. The controller must supply equal grants or leave model-backed generation
unavailable to all arms. No synthetic model grant is supplied by this lane.
