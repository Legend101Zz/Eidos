#!/usr/bin/env bash
# Eidos macOS owner qualification kit (run by the researcher on their own Mac; nothing is sent anywhere).
#
#   bash scripts/qualify/macos_owner_kit.sh [--keep]
#
# From a checkout at the release tag. It builds the wheels, installs them into a fresh venv in a
# temporary directory, runs init/start/doctor, creates a project, runs one fixture investigation
# (deterministic, no LLM, no API key) and one real component job through the runner, then stops
# the lab and deletes everything it created (unless --keep). It writes a JSON receipt to
# ~/eidos-macos-receipt-<timestamp>.json. No credentials or file contents are recorded.
#
# Requirements: macOS, `uv` (https://docs.astral.sh/uv/), PostgreSQL >= 14 binaries
# (e.g. `brew install postgresql@16`), network access to GitHub release assets (pinned Temporal
# server, sha256-verified). Uses ports 18765 (API), 15544 (PostgreSQL), 17633-17641 (Temporal).
set -uo pipefail
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TS=$(date -u +%Y%m%dT%H%M%SZ)
W="$(mktemp -d /tmp/eidos-macos-qual.XXXXXX)"
chmod 755 "$W"  # PostgreSQL may run under a separate account when the lab runs as root
RECEIPT="$HOME/eidos-macos-receipt-$TS.json"
STEPS="$W/steps.jsonl"; : > "$STEPS"
export EIDOS_HOME="$W/lab" UV_CACHE_DIR="${UV_CACHE_DIR:-$W/uv-cache}"
step() {  # step <name> <command...>: run, time and record pass/fail (stdout/stderr kept in $W/logs)
  local name="$1"; shift; mkdir -p "$W/logs"; local t0=$(date +%s)
  if "$@" > "$W/logs/$name.out" 2> "$W/logs/$name.err"; then rc=0; else rc=$?; fi
  printf '{"step":"%s","rc":%d,"seconds":%d}\n' "$name" "$rc" "$(( $(date +%s) - t0 ))" >> "$STEPS"
  echo "[$([ $rc = 0 ] && echo ok || echo FAIL)] $name"; return $rc
}
PG_BIN="${PG_BIN:-$( (brew --prefix postgresql@16 2>/dev/null || true) )/bin}"
LAB="$W/venv/bin/lab"
step preflight bash -c "{ [ \"\$(uname)\" = Darwin ] || [ \"${EIDOS_KIT_ALLOW_LINUX:-0}\" = 1 ]; } && command -v uv && [ -x '$PG_BIN/pg_ctl' ]"
step build bash -c "cd '$REPO' && uv build -q --all-packages -o '$W/dist'"
step install bash -c "uv venv -q -p 3.12 '$W/venv' && uv pip install -q -p '$W/venv/bin/python' --find-links '$W/dist' eidos-lab"
step not_from_checkout "$W/venv/bin/python" -c "import eidos,sys; sys.exit('$REPO' in eidos.__file__)"
step init "$LAB" init --home "$EIDOS_HOME" --api-port 18765 --pg-port 15544 --temporal-port 17633 --pg-bin "$PG_BIN"
step start "$LAB" start
step doctor bash -c "'$LAB' doctor --json > '$W/doctor.json'; true"
step project "$LAB" project create macq --budget compute_seconds=2000 --budget trials=50
cat > "$W/charter.json" <<'JSON'
{"study_id": "macos-qual", "question": "macOS qualification smoke (fixture)", "exploration_only": true}
JSON
step charter "$LAB" charter commit macq "$W/charter.json"
step investigation "$LAB" investigate start macq --question "macOS smoke" --charter charter:macos-qual \
  --policy note-then-wait --investigation-id inv-macq --limit turns=2 --limit model_tokens=100000
printf 'pair_id,reference_logit,variant_logit,reference_label,variant_label,stratum\n1,1.2,-0.4,1,0,a\n2,-0.3,0.8,0,1,b\n3,0.5,0.2,1,0,a\n' > "$W/pairs.csv"
step job bash -c "A=\$('$LAB' artifact upload macq '$W/pairs.csv' --schema labeled-binary-pairs/1 --media-type text/csv | python3 -c 'import json,sys;print(json.load(sys.stdin)[\"result\"][\"artifact_id\"])') && '$LAB' run macq lab.pairs.by_group@1.0.0 --input predictions=\$A --config '{\"group_by\": \"stratum\"}' --environment lab --wait > '$W/job.json' && grep -q '\"status\": \"succeeded\"' '$W/job.json'"
sleep 20
step inspect bash -c "'$LAB' investigate show macq inv-macq > '$W/inv.json' && '$LAB' status --json > '$W/status.json'"
step stop "$LAB" stop
python3 - "$W" "$RECEIPT" "$REPO" <<'PY'
import json, platform, subprocess, sys, pathlib
w, out, repo = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def load(n):
    try:
        return json.loads((w / n).read_text())
    except Exception:
        return None
inv, job, doc = load("inv.json"), load("job.json"), load("doctor.json")
r = {"kit": "eidos-macos-owner-kit/1", "label": "researcher-machine qualification run (owner-run)",
     "git": subprocess.run(["git", "-C", repo, "describe", "--always", "--tags", "--dirty"], capture_output=True, text=True).stdout.strip(),
     "macos": platform.mac_ver()[0], "machine": platform.machine(), "python": platform.python_version(),
     "steps": [json.loads(l) for l in (w / "steps.jsonl").read_text().splitlines()],
     "doctor": doc, "job_status": (job or {}).get("job", {}).get("status") if job else None,
     "investigation": {"status": inv.get("status"), "turns": [(t["seq"], t["stop_reason"]) for t in inv.get("turns", [])]} if inv else None}
r["passed"] = all(s["rc"] == 0 for s in r["steps"]) and not any(c.get("status") == "fail" for c in (doc or []))
pathlib.Path(out).write_text(json.dumps(r, indent=2))
print(json.dumps({"receipt": out, "passed": r["passed"]}))
PY
if [ "$KEEP" = 0 ]; then rm -rf "$W"; echo "removed $W"; else echo "kept $W (logs in $W/logs)"; fi
