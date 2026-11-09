#!/usr/bin/env bash
# Full-lab real-LLM investigation on one task bundle (controller-run on the owner's Mac; capped).
#
#   ANTHROPIC_API_KEY=... bash scripts/qualify/full_lab_live.sh --bundle DIR --python RESEARCH_PY [--out receipt.json]
#   printf 'ANTHROPIC_API_KEY=%s\n' "$K" | CREDENTIALS_STDIN=1 bash scripts/qualify/full_lab_live.sh ...   (preferred)
#
# With CREDENTIALS_STDIN=1 the key arrives on stdin and goes to `lab start --credentials-stdin`: it is in no
# process environment except the investigator worker's (not this script's, not the CLI calls', not the
# supervisor's or PostgreSQL's). Otherwise it is taken from this script's environment and unset before any
# other command runs.
#
# Preconditions: a lab is initialised (EIDOS_HOME, on the SSD) from the release wheels and NOT running. Start it
# from this script so that the key reaches the supervisor's environment only. The supervisor passes provider
# credentials only to the investigator worker (scrub_secrets for every other service). Jobs get an
# allowlisted environment (job_environment), and the key is never written to lab config or job envs.
# This script checks the lab directory afterwards with a byte scan of every file, including PostgreSQL heap
# files, logs and job attempt directories.
# Caps (hard; override with MAX_TURNS, MAX_CALLS_PER_TURN, MAX_REQ_PER_DECISION, MODEL_USD, MODEL_TOKENS):
# default max_turns=5 x max_model_calls_per_turn=2 x max_requests_per_decision=4 = 40 requests; model_usd=3
# (priced at the configured per-Mtok rates), model_tokens=600000; 4096 output tokens per request. Refused-proposal
# retries are turns too, so turns x calls x requests bounds the total number of model requests.
set -euo pipefail
BUNDLE="" PY="" OUT="full-lab-live-receipt.json" MODEL="${MODEL:-anthropic:claude-sonnet-5-5}"
DEFAULT_PRICE='{"input": 3.0, "output": 15.0}'
PRICE="${PRICE:-$DEFAULT_PRICE}"   # USD per Mtok used for the model_usd budget; set to the current list price
MAX_TURNS="${MAX_TURNS:-5}" MAX_CALLS_PER_TURN="${MAX_CALLS_PER_TURN:-2}" MAX_REQ_PER_DECISION="${MAX_REQ_PER_DECISION:-4}"
MODEL_USD="${MODEL_USD:-3}" MODEL_TOKENS="${MODEL_TOKENS:-600000}"
# (was "${PRICE:-{...}}": the first "}" closes the expansion, so a set PRICE got a stray "}" and broke --settings)
while [ $# -gt 0 ]; do case "$1" in --bundle) BUNDLE="$2"; shift 2;; --python) PY="$2"; shift 2;; --out) OUT="$2"; shift 2;;
  *) echo "unknown arg $1"; exit 2;; esac; done
[ -n "$BUNDLE" ] && [ -n "$PY" ] || { echo "usage: $0 --bundle DIR --python PY [--out FILE]"; exit 2; }
: "${EIDOS_HOME:?set EIDOS_HOME (on the SSD)}"
if [ "${CREDENTIALS_STDIN:-0}" = 1 ]; then
  IFS= read -r CRED_LINE
else
  : "${ANTHROPIC_API_KEY:?key must be in this process environment only (or use CREDENTIALS_STDIN=1)}"
  CRED_LINE="ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  unset ANTHROPIC_API_KEY
fi
case "$CRED_LINE" in ANTHROPIC_API_KEY=?*) ;; *) echo "no ANTHROPIC_API_KEY credential given" >&2; exit 2;; esac
LAB="${LAB:-lab}"
P="live$(date +%s)"
W="$(mktemp -d "${TMPDIR:-/tmp}/eidos-full-lab.XXXXXX")"
printf '%s\n' "$CRED_LINE" | "$LAB" start --credentials-stdin >/dev/null   # printf is a builtin: the key is never on argv
trap '"$LAB" stop --running-jobs cancel >/dev/null 2>&1 || true; rm -rf "$W"' EXIT
"$LAB" doctor --json > "$W/doctor.json" || true
"$LAB" project create "$P" --budget compute_seconds=20000 --budget trials=100 >/dev/null
IMP=$("$LAB" task import "$P" "$BUNDLE" --python "$PY")
CH=$(echo "$IMP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["charter_id"])')
INPUTS=$(echo "$IMP" | python3 -c 'import json,sys; a=json.load(sys.stdin)["artifacts"]; print(" ".join(f"--input {k}={v}" for k,v in a.items()))')
for c in eidos.project.command eidos.task.score-classification; do
  "$LAB" component install "$P" "$(python3 - "$c" "$LAB" <<'PY'
import json, subprocess, sys
for p in json.loads(subprocess.run([sys.argv[2], "component", "builtins"], capture_output=True, text=True).stdout):
    if p.endswith("/" + sys.argv[1]): print(p)
PY
)" >/dev/null
done
SETTINGS="{\"max_requests_per_decision\": $MAX_REQ_PER_DECISION, \"max_output_tokens\": 4096, \"max_tokens_per_decision\": 100000, \"price_per_mtok\": $PRICE}"
INV=$("$LAB" investigate start "$P" --question "$(cat "$BUNDLE/brief.md")" --charter "$CH" $INPUTS \
  --runtime pydantic-ai --model "$MODEL" --max-turns "$MAX_TURNS" --max-model-calls-per-turn "$MAX_CALLS_PER_TURN" --settings "$SETTINGS" \
  --limit model_usd="$MODEL_USD" --limit model_tokens="$MODEL_TOKENS" --limit compute_seconds=6000 \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["investigation_id"])')
echo "investigation $INV (project $P); waiting up to $((MAX_TURNS * 15)) min"
for _ in $(seq 1 $((MAX_TURNS * 90))); do
  # status, turn count and whether it parked as "needs operator input" (stays active; would otherwise wait 90 min)
  read -r S N NI < <("$LAB" investigate show "$P" "$INV" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], len(d["turns"]), int(bool(d.get("needs_input"))))')
  [ "$S" != "active" ] && break
  [ "$NI" = 1 ] && echo "investigation needs operator input; stopping the wait" && break
  [ "$N" -ge "$MAX_TURNS" ] && sleep 30 && break
  sleep 10
done
cat > "$W/receipt.py" <<'PY'
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
         "call_args": [json.dumps(c.get("args"), sort_keys=True)[:400] for c in (x["body"] or {}).get("calls", [])],
         "rationale": str((x["body"] or {}).get("rationale", ""))[:400],
         "control": (x["body"] or {}).get("control"), "error": (x["body"] or {}).get("error")} for x in tt["decisions"]]})
def jobinfo(j):
    out = {k: j[k] for k in ("id", "status", "kind", "component")}
    try:
        g = q("query", "get_job", "-p", p, "--params", json.dumps({"job_id": j["id"]}))
        att = (g.get("attempts") or [{}])[-1]
        out.update(error=att.get("error"), metrics=(att.get("receipt") or {}).get("metrics"),
                   returncode=(att.get("receipt") or {}).get("returncode"),
                   outputs=[o.get("slot") for o in g.get("outputs") or []])
    except Exception as exc:  # noqa: BLE001
        out["detail_error"] = str(exc)[:200]
    return out


key = sys.stdin.read()  # the key arrives on a pipe, never via argv, environment or a file
home = os.environ["EIDOS_HOME"]
if not key:
    sys.exit("no key on stdin for the scan")
# Byte scan of every file under the lab home, including binary PostgreSQL heap files and logs. The key is
# never placed on a command line.
needle, leak = key.encode(), []
for root, _dirs, files in os.walk(home):
    for f in files:
        fp = os.path.join(root, f)
        try:
            with open(fp, "rb") as fh:
                if needle in fh.read():
                    leak.append(fp)
        except OSError:
            pass
rec = {"receipt": "eidos-full-lab-live/1", "label": "full-lab real-LLM development run (owner Mac)", "project": p,
       "investigation": inv, "status": d["status"], "needs_input": d.get("needs_input"), "budgets": d.get("budgets"), "turns": turns,
       "jobs": [jobinfo(j) for j in d["jobs"]],
       "conclusion": (d.get("body") or {}).get("conclusion"),
       "key_found_in_lab_files": leak,  # must be []
       "caps": {"max_turns": int(sys.argv[5]), "max_model_calls_per_turn": int(sys.argv[6]),
                "max_requests_per_decision": int(sys.argv[7]), "model_usd": float(sys.argv[8]),
                "model_tokens": int(sys.argv[9])}}
open(out, "w").write(json.dumps(rec, indent=2, default=str))
print(json.dumps({k: rec[k] for k in ("investigation", "status", "key_found_in_lab_files")}))
sys.exit(1 if leak else 0)
PY
if printf '%s' "${CRED_LINE#*=}" | python3 "$W/receipt.py" "$P" "$INV" "$OUT" "$LAB" "$MAX_TURNS" "$MAX_CALLS_PER_TURN" "$MAX_REQ_PER_DECISION" "$MODEL_USD" "$MODEL_TOKENS"; then rc=0; else rc=$?; fi
CRED_LINE=""
exit $rc
