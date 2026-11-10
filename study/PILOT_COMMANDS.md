# Controller launch commands

Run from `/Volumes/Mrigesh SSD/eidos-v1/wt-dev-pilot-1` on `task/dev-pilot-1`.
These are controller commands, outside all agent sandboxes. Local grading and
GitHub submission start only after the entire paired block finishes. The fixed
wheel environment is already installed; do not rebuild or replace it during the
pilot. See `eidos_arms/CHARTER_RULES.md` for its prospective rule and limits.

```sh
source "/Volumes/Mrigesh SSD/eidos-v1/env.sh"
cd "/Volumes/Mrigesh SSD/eidos-v1/wt-dev-pilot-1"
pilot_root="/Volumes/Mrigesh SSD/eidos-v1/pilot-1"
export EIDOS_STUDY_PYTHON="/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1-lab/bin/python"
export EIDOS_STUDY_HOME="$pilot_root/lab/fixed"
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab start
"$EIDOS_RESEARCH_PYTHON" study/launcher.py plan --seed 20261111 --out "$pilot_root/design.json"
```

Set `K` to the controller's 1-based randomized block order (1–9):

```sh
K=1
"$EIDOS_RESEARCH_PYTHON" study/launcher.py run \
  --design "$pilot_root/design.json" --runs-root "$pilot_root/runs" --block "$K"
BLOCK=$("$EIDOS_RESEARCH_PYTHON" - "$pilot_root/design.json" "$K" <<'PY'
import json, sys
rows = json.load(open(sys.argv[1]))
print(next(r['block_id'] for r in rows if r['block_order'] == int(sys.argv[2])))
PY
)
"$EIDOS_RESEARCH_PYTHON" study/grade_block.py \
  --block-record "$pilot_root/runs/$BLOCK/block_record.json" \
  --grading-root "$pilot_root/grading" --results "$pilot_root/results.json"
```

The launcher prepares all four bundles, then calls `Popen` for all four arms before
waiting for any. Arms 3 and 4 serialize lab startup only; their model work is
concurrent and each invocation creates a fresh UUID project in that same lab.
Complete blocks skip on resume. For a recorded infrastructure incident, rerun the
whole block with the same command plus `--replace-incident`; previous receipts are
archived. Never reroll a nonincident block.

Submit the four completed candidates through the controller-owned grading tool.
Use a new opaque batch tag each time, especially after an incident replacement.
These commands intentionally do not submit on behalf of the controller now.
`submit.py` pushes its own candidate-store branch in the separate grading repo;
this worker only pushed `task/dev-pilot-1` in the Eidos repo.

```sh
BATCH="p1-$(date -u +%Y%m%dT%H%M%SZ)"
for ARM in 1 2 3 4; do
  RUN_DIR="$pilot_root/runs/$BLOCK/arm$ARM"
  CARD=$("$EIDOS_RESEARCH_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["card"])' "$RUN_DIR/arm.json")
  TAG="$BATCH-k$K-a$ARM"
  REMOTE_DIR="$pilot_root/github-grading/$BATCH/$BLOCK/arm$ARM"
  mkdir -p "$REMOTE_DIR"
  "$EIDOS_RESEARCH_PYTHON" "/Volumes/Mrigesh SSD/eidos-v1/eidos-grading/tools/submit.py" \
    "$RUN_DIR" --card "$CARD" --run-tag "$TAG" > "$REMOTE_DIR/submission.json"
done
for ARM in 1 2 3 4; do
  RUN_DIR="$pilot_root/runs/$BLOCK/arm$ARM"
  TAG="$BATCH-k$K-a$ARM"
  REMOTE_DIR="$pilot_root/github-grading/$BATCH/$BLOCK/arm$ARM"
  RUN_ID=$("$EIDOS_RESEARCH_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$REMOTE_DIR/submission.json")
  "$EIDOS_RESEARCH_PYTHON" "/Volumes/Mrigesh SSD/eidos-v1/eidos-grading/tools/fetch.py" \
    --run-id "$RUN_ID" --run-tag "$TAG" --out "$REMOTE_DIR/result" --wait --run-dir "$RUN_DIR"
done
"$EIDOS_RESEARCH_PYTHON" study/analyze.py \
  --design "$pilot_root/design.json" --results "$pilot_root/results.json" --out "$pilot_root/analysis"
```

Analysis above uses the local roster. GitHub fetches are retained separately for
independent comparison; they do not silently overwrite local grades. To analyze
fetched grades, collect their joined `grade.json` files into a separate roster and
use it explicitly (one batch at a time; preserves unsubmitted local rows):

```sh
"$EIDOS_RESEARCH_PYTHON" - "$pilot_root" "$BATCH" <<'PY'
import json, sys
from pathlib import Path
from study.common import write_json
root, batch = Path(sys.argv[1]), sys.argv[2]
rows = json.loads((root / 'results.json').read_text())
key = lambda r: (r['family'], r['task'], r['run'], r['arm'])
joined = {key(r): r for r in rows}
for path in (root / 'github-grading' / batch).glob('*/arm*/result/grade.json'):
    grade = json.loads(path.read_text())
    joined[key(grade)] = grade
write_json(root / 'results-github.json', list(joined.values()))
PY
"$EIDOS_RESEARCH_PYTHON" study/analyze.py \
  --design "$pilot_root/design.json" --results "$pilot_root/results-github.json" --out "$pilot_root/analysis-github"
```

Controller review must invalidate the entire paired block on any remote grader
infrastructure incident, and archive/re-run that block. Do not combine partially
returned remote grades into a superiority claim. Repeat for each K; analyse again
after all nine blocks. Costs for historical incidents need the archived receipts.

After all arms finish, shut down only the shared lab started for this pilot:

```sh
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab stop
```

No arm receives component LLM-broker grants. The adapters set and verify
`policy.llm_broker.enabled=false`; records retain initial/final policies. Arm 4's
investigator calls have the common budget, `json`, output cap 16384 and request
timeout 300. Embedded task text remains in its question under ADR 0016.
