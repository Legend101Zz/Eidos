#!/usr/bin/env bash
# Full-lab real-LLM investigation on one task bundle (controller-run on the owner's Mac; capped).
#
#   ANTHROPIC_API_KEY=... bash scripts/qualify/full_lab_live.sh --bundle DIR --python RESEARCH_PY [--out receipt.json]
#
# Preconditions: a lab is initialised (EIDOS_HOME, on the SSD) from the release wheels and NOT running. Start it
# from this script so that the key reaches the supervisor's environment only. The supervisor passes provider
# credentials only to the investigator worker (scrub_secrets for every other service). Jobs get an
# allowlisted environment (job_environment), and the key is never written to lab config or job envs.
# This script checks that afterwards: it greps the lab directory and every job's recorded environment
# for the key.
# Caps (hard): max_turns=5 x max_model_calls_per_turn=2 x max_requests_per_decision=4 = 40 requests; model_usd=3
# (priced at the configured per-Mtok rates), model_tokens=600000; 4096 output tokens per request.
set -euo pipefail
BUNDLE="" PY="" OUT="full-lab-live-receipt.json" MODEL="${MODEL:-anthropic:claude-sonnet-5-5}"
PRICE="${PRICE:-{\"input\": 3.0, \"output\": 15.0}}"   # USD per Mtok used for the model_usd budget; set to the current list price
while [ $# -gt 0 ]; do case "$1" in --bundle) BUNDLE="$2"; shift 2;; --python) PY="$2"; shift 2;; --out) OUT="$2"; shift 2;;
  *) echo "unknown arg $1"; exit 2;; esac; done
[ -n "$BUNDLE" ] && [ -n "$PY" ] || { echo "usage: $0 --bundle DIR --python PY [--out FILE]"; exit 2; }
: "${EIDOS_HOME:?set EIDOS_HOME (on the SSD)}" "${ANTHROPIC_API_KEY:?key must be in this process environment only}"
LAB="${LAB:-lab}"
P="live$(date +%s)"
$LAB start >/dev/null
trap '$LAB stop --running-jobs cancel >/dev/null 2>&1 || true' EXIT
$LAB doctor --json > /tmp/eidos-live-doctor.json || true
$LAB project create "$P" --budget compute_seconds=20000 --budget trials=100 >/dev/null
IMP=$($LAB task import "$P" "$BUNDLE" --python "$PY")
CH=$(echo "$IMP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["charter_id"])')
INPUTS=$(echo "$IMP" | python3 -c 'import json,sys; a=json.load(sys.stdin)["artifacts"]; print(" ".join(f"--input {k}={v}" for k,v in a.items()))')
for c in eidos.project.command eidos.task.score-classification; do
  $LAB component install "$P" "$(python3 - "$c" <<'PY'
import json, subprocess, sys
for p in json.loads(subprocess.run(["lab", "component", "builtins"], capture_output=True, text=True).stdout):
    if p.endswith(sys.argv[1]): print(p)
PY
)" >/dev/null
done
SETTINGS="{\"max_requests_per_decision\": 4, \"max_output_tokens\": 4096, \"max_tokens_per_decision\": 100000, \"price_per_mtok\": $PRICE}"
INV=$($LAB investigate start "$P" --question "$(cat "$BUNDLE/brief.md")" --charter "$CH" $INPUTS \
  --runtime pydantic-ai --model "$MODEL" --max-turns 5 --max-model-calls-per-turn 2 --settings "$SETTINGS" \
  --limit model_usd=3 --limit model_tokens=600000 --limit compute_seconds=6000 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["investigation_id"])')
echo "investigation $INV (project $P); waiting up to 90 min"
for _ in $(seq 1 540); do
  S=$($LAB investigate show "$P" "$INV" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [ "$S" != "active" ] && break
  N=$($LAB investigate show "$P" "$INV" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["turns"]))')
  [ "$N" -ge 5 ] && sleep 30 && break
  sleep 10
done
python3 - "$P" "$INV" "$OUT" "$LAB" <<'PY'
import json, os, subprocess, sys
p, inv, out, lab = sys.argv[1:5]
q = lambda *a: json.loads(subprocess.run([lab, *a], capture_output=True, text=True, check=True).stdout)
d = q("investigate", "show", p, inv)
turns = []
for t in d["turns"]:
    tt = q("query", "get_turn", "-p", p, "--params", json.dumps({"turn_id": t["id"]}))
    turns.append({"seq": t["seq"], "stop_reason": t["stop_reason"], "decisions": [
        {"slot": x["slot"], "accepted": x["accepted"], "usage": (x["body"] or {}).get("usage"),
         "served_models": ((x["body"] or {}).get("model") or {}).get("served_models"),
         "calls": [c["tool"] for c in (x["body"] or {}).get("calls", [])],
         "control": (x["body"] or {}).get("control"), "error": (x["body"] or {}).get("error")} for x in tt["decisions"]]})
key = os.environ["ANTHROPIC_API_KEY"]
home = os.environ["EIDOS_HOME"]
leak = subprocess.run(["grep", "-rIl", "--", key, home], capture_output=True, text=True).stdout.split()
rec = {"receipt": "eidos-full-lab-live/1", "label": "full-lab real-LLM development run (owner Mac)", "project": p,
       "investigation": inv, "status": d["status"], "budgets": d.get("budgets"), "turns": turns,
       "jobs": [{k: j[k] for k in ("id", "status", "kind", "component")} for j in d["jobs"]],
       "conclusion": (d.get("body") or {}).get("conclusion"),
       "key_found_in_lab_files": leak,  # must be []
       "caps": {"max_turns": 5, "max_model_calls_per_turn": 2, "max_requests_per_decision": 4, "model_usd": 3,
                "model_tokens": 600000}}
open(out, "w").write(json.dumps(rec, indent=2, default=str))
print(json.dumps({k: rec[k] for k in ("investigation", "status", "key_found_in_lab_files")}))
sys.exit(1 if leak else 0)
PY
