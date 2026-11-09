#!/usr/bin/env bash
# A15: disk exhaustion on a real, small volume (macOS; no sudo). Creates a 2 GiB APFS disk image under WORK,
# attaches it (user-level hdiutil, -nobrowse), puts a whole lab home on it, and runs:
#   A. a job fills the volume to below the lab's reserve while it runs, then tries to publish an output:
#      uploads are refused (DISK_PRESSURE), the publication is refused, the attempt ends `failed` with a
#      `resource` error (reconciled; nothing half-published), and `lab status` / `lab doctor` show the pressure;
#   B. a job writes until the volume is completely full (ENOSPC): the job fails cleanly, and the ledger and
#      services stay consistent (or recover after space is freed);
#   C. space is freed: the pressure display clears and a new job succeeds.
# Then the lab is stopped, the image detached and deleted. Receipt: WORK/disk-full-receipt.json.
#
#   LAB=<venv>/bin/lab WORK=<dir on the SSD> bash scripts/qualify/disk_full_check.sh
set -euo pipefail
LAB="${LAB:?set LAB to the lab executable}"; WORK="${WORK:?set WORK (a directory on the SSD)}"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}"
IMG="$WORK/eidos-diskfull.dmg"; OUT="$WORK/disk-full-out"
mkdir -p "$WORK" "$OUT"
[ ! -e "$IMG" ] || { echo "$IMG exists; remove it first" >&2; exit 2; }
hdiutil create -size 2g -fs APFS -volname EidosDiskFull "$IMG" >/dev/null
trap 'rm -f "$IMG"' EXIT
# A user-level attach mounts under /Volumes (a custom -mountpoint needs root); -nobrowse hides it from Finder.
hdiutil attach -nobrowse "$IMG" > "$OUT/attach.txt"
DEV="$(awk '/\/Volumes\//{print $1}' "$OUT/attach.txt" | tail -1)"
MNT="$(awk -F'\t' '/\/Volumes\//{print $NF}' "$OUT/attach.txt" | tail -1 | sed 's/[[:space:]]*$//')"
[ -d "$MNT" ] || { echo "attach did not give a mount point: $(cat "$OUT/attach.txt")" >&2; exit 1; }
cleanup() {
  EIDOS_HOME="$MNT/lab" "$LAB" stop --running-jobs cancel >/dev/null 2>&1 || true
  hdiutil detach "$DEV" >/dev/null 2>&1 || hdiutil detach -force "$DEV" >/dev/null 2>&1 || true
  rm -f "$IMG"
}
trap cleanup EXIT
export EIDOS_HOME="$MNT/lab"
"$LAB" init --home "$EIDOS_HOME" --api-port 28765 --pg-port 25544 --temporal-port 27633 --pg-bin "$PG_BIN" > "$OUT/init.json"
"$LAB" start > /dev/null
P=diskfull
"$LAB" project create "$P" --budget compute_seconds=20000 --budget trials=50 > /dev/null
LABPY="$(python3 -c "import json; print(json.load(open('$EIDOS_HOME/config/lab.json'))['lab_python'])")"
mkdir -p "$WORK/diskfull-project"
"$LAB" project register-env "$P" --python "$LABPY" --root "$WORK/diskfull-project" > /dev/null
"$LAB" component install "$P" "$("$LABPY" -c 'import eidos,pathlib;print(pathlib.Path(eidos.__file__).parent/"_components"/"eidos.project.command")')" > /dev/null
REF="$("$LAB" component list "$P" --text eidos.project.command | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")"
snap() {  # name: lab status, doctor, df, an upload attempt
  "$LAB" status --json > "$OUT/$1-status.json" 2>&1 || true
  "$LAB" doctor --json > "$OUT/$1-doctor.json" 2>&1 || true
  df -k "$MNT" | tail -1 > "$OUT/$1-df.txt"
  head -c 1048576 /dev/urandom > "$OUT/upload-probe.bin"
  "$LAB" artifact upload "$P" "$OUT/upload-probe.bin" > "$OUT/$1-upload.json" 2> "$OUT/$1-upload.err" || true
}
wait_job() {
  for _ in $(seq 1 240); do
    S="$("$LAB" job show "$P" "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")"
    case "$S" in succeeded|failed|cancelled) break;; esac; sleep 2
  done
  "$LAB" job show "$P" "$1" > "$OUT/$2-job.json"
}
snap baseline
# ---- A: fill to below the reserve while the job runs, then publish a 64 MiB output -------------------------
CFG_A="$(python3 - <<'PY'
import json
code = ("import os, shutil, time\n"
        "free = shutil.disk_usage('.').free\n"
        "fill = max(0, free - (200 << 20))\n"
        "with open('filler.bin', 'wb') as f:\n"
        "    while fill > 0:\n"
        "        n = min(fill, 64 << 20); f.write(b'\\0' * n); fill -= n\n"
        "    f.flush(); os.fsync(f.fileno())\n"
        "os.makedirs('{out}', exist_ok=True)\n"
        "open('{out}/result.bin', 'wb').write(os.urandom(64 << 20))\n"
        "open('{work}/../holding', 'w').write('1')\n"
        "time.sleep(45)\n")
print(json.dumps({"argv": ["python", "-c", code], "timeout_seconds": 900}))
PY
)"
JA="$("$LAB" run "$P" "$REF" --wall-seconds 1200 --purpose reproduction --config "$CFG_A" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")"
for _ in $(seq 1 120); do
  ls "$EIDOS_HOME"/jobs/"$JA"/*/scratch/holding >/dev/null 2>&1 && break; sleep 1
done
sleep 3
snap during-A
wait_job "$JA" A
snap after-A
# ---- B: write until ENOSPC -----------------------------------------------------------------------------
CFG_B="$(python3 - <<'PY'
import json
code = ("import os\n"
        "with open('filler2.bin', 'wb') as f:\n"
        "    while True:\n"
        "        f.write(b'\\0' * (8 << 20)); f.flush()\n")
print(json.dumps({"argv": ["python", "-c", code], "timeout_seconds": 600}))
PY
)"
JB="$("$LAB" run "$P" "$REF" --wall-seconds 900 --purpose reproduction --config "$CFG_B" 2> "$OUT/B-run.err" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])" || echo "")"
if [ -n "$JB" ]; then wait_job "$JB" B; fi
snap after-B
# ---- C: free space (the operator removes the jobs' scratch files), then a new job --------------------------
find "$EIDOS_HOME/jobs" -name 'filler*.bin' -print -delete > "$OUT/freed.txt" 2>&1 || true
sleep 5
snap after-free
JC="$("$LAB" run "$P" "$REF" --wall-seconds 600 --purpose reproduction --config '{"argv": ["python", "-c", "import os; os.makedirs(\"{out}\", exist_ok=True); open(\"{out}/ok.txt\", \"w\").write(\"ok\")"]}' | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")"
wait_job "$JC" C
snap final
python3 - "$OUT" "$JA" "${JB:-}" "$JC" "$DEV" <<'PY'
import json, os, sys
out, ja, jb, jc, dev = sys.argv[1:6]
def load(name):
    try:
        return json.load(open(os.path.join(out, name)))
    except (OSError, ValueError):
        return None
def text(name):
    try:
        return open(os.path.join(out, name)).read()
    except OSError:
        return ""
def doctor_disk(name):
    d = load(name) or []
    return next(({"status": c["status"], "detail": c.get("detail")} for c in d if c.get("check") == "disk"), None)
checks = []
def check(name, ok, **detail):
    checks.append({"check": name, "ok": bool(ok), **detail})
A = load("A-job.json") or {}
errA = ((A.get("terminal") or {}).get("error") or {})
check("A: during the job the store refuses new bytes (upload -> DISK_PRESSURE)", "DISK_PRESSURE" in text("during-A-upload.err") +
      text("during-A-upload.json"), stderr=text("during-A-upload.err")[-300:])
check("A: lab status shows disk pressure while the volume is below the reserve",
      ((load("during-A-status.json") or {}).get("disk") or {}).get("pressure") is True, disk=(load("during-A-status.json") or {}).get("disk"))
check("A: lab status says new bytes are being refused (below the reserve)",
      ((load("during-A-status.json") or {}).get("disk") or {}).get("refusing_new_bytes") is True)
check("A: lab doctor fails the disk check while new bytes are refused", (doctor_disk("during-A-doctor.json") or {}).get("status") == "fail",
      disk=doctor_disk("during-A-doctor.json"))
check("A: the output publication was refused and the attempt ended failed with a resource error",
      A.get("status") == "failed" and errA.get("category") == "resource", status=A.get("status"), error=errA)
check("A: nothing was half-published (no outputs recorded)", not (A.get("outputs") or []), outputs=A.get("outputs"))
check("A: a single attempt, terminal (reconciled, not left running)", [a["status"] for a in A.get("attempts", [])] in
      (["failed"],), attempts=[a["status"] for a in A.get("attempts", [])])
B = load("B-job.json") or {}
check("B: the ENOSPC job ended (failed or refused), not running", (not jb) or B.get("status") in ("failed", "cancelled"),
      status=B.get("status"), error=(B.get("terminal") or {}).get("error"), run_err=text("B-run.err")[-300:])
check("B: lab services still answer after the volume was full", load("after-B-status.json") is not None,
      status_err=text("after-B-status.json")[:300] if load("after-B-status.json") is None else None)
check("C: after freeing space the pressure display clears", ((load("after-free-status.json") or {}).get("disk") or {}).get("pressure") is False,
      disk=(load("after-free-status.json") or {}).get("disk"))
check("C: uploads are accepted again", "artifact_id" in text("after-free-upload.json"), out=text("after-free-upload.json")[:200])
C = load("C-job.json") or {}
check("C: a new job succeeds", C.get("status") == "succeeded", status=C.get("status"))
check("final: lab doctor has no failed checks", not [c for c in (load("final-doctor.json") or []) if c.get("status") == "fail"],
      fails=[c for c in (load("final-doctor.json") or []) if c.get("status") == "fail"])
rec = {"receipt": "eidos-disk-full/1", "volume": "2 GiB APFS disk image (hdiutil, user-level attach)", "device": dev,
       "df": {k: text(f"{k}-df.txt").strip() for k in ("baseline", "during-A", "after-A", "after-B", "after-free", "final")},
       "jobs": {"A": ja, "B": jb or None, "C": jc}, "A_error": errA, "B_error": (B.get("terminal") or {}).get("error"),
       "checks": checks, "ok": all(c["ok"] for c in checks)}
json.dump(rec, open(os.path.join(os.path.dirname(out), "disk-full-receipt.json"), "w"), indent=2, default=str)
for c in checks:
    print(("PASS " if c["ok"] else "FAIL ") + c["check"])
print("RESULT", "PASS" if rec["ok"] else "FAIL")
sys.exit(0 if rec["ok"] else 1)
PY
