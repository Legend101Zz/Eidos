#!/usr/bin/env bash
# Cross-release upgrade proof: install the OLD release's wheels, create a lab with real data, upgrade the SAME lab
# directory to the checked-out version (`pip install` over the same venv, then `lab upgrade`) and verify the data.
# Env: OLD_TAG (default eidos-v0.1.0), CI_WORK (scratch; default $RUNNER_TEMP/eidos-upgrade or a mktemp dir),
#      LAB_API_PORT/LAB_PG_PORT/LAB_TEMPORAL_PORT (Temporal uses 9 consecutive ports from LAB_TEMPORAL_PORT),
#      PG_BIN, TEMPORAL_SOURCE (as in lab_cycle.sh), PYTHON (default 3.12), KEEP_WORK=1 to keep the scratch dir.
# Output: $CI_WORK/out/result.json (+ seed.json, snapshot*.json, reports, doctor-after.json), $CI_WORK/lab/logs.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OLD_TAG="${OLD_TAG:-eidos-v0.1.0}"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-upgrade}"
PYTHON="${PYTHON:-3.12}"
export LAB_API_PORT="${LAB_API_PORT:-8765}" LAB_PG_PORT="${LAB_PG_PORT:-5544}" LAB_TEMPORAL_PORT="${LAB_TEMPORAL_PORT:-7633}"
if [ -z "${PG_BIN:-}" ]; then PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n1 || true)"; fi
export PG_BIN
START=$SECONDS

git -C "$REPO" rev-parse --verify -q "refs/tags/$OLD_TAG^{commit}" >/dev/null \
  || { echo "tag $OLD_TAG not found (CI must fetch tags: actions/checkout fetch-depth: 0)" >&2; exit 2; }
rm -rf "$CI_WORK"; mkdir -p "$CI_WORK/out"
cd "$CI_WORK"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$CI_WORK/uv-cache}"

export EIDOS_HOME="$CI_WORK/lab" OUT="$CI_WORK/out" WORK="$CI_WORK/out" OLD_TAG
export LAB_BIN="$CI_WORK/venv/bin/lab" LAB_PYTHON="$CI_WORK/venv/bin/python" ECHO_DIR="$CI_WORK/echo"
export PATH="$CI_WORK/venv/bin:$PATH"   # services spawned by the lab resolve from the venv
cleanup() { rc=$?; if [ "$rc" != 0 ]; then echo "--- FAILED (rc=$rc); stopping lab"; fi; "$LAB_BIN" stop >/dev/null 2>&1 || true
  [ -n "${KEEP_WORK:-}" ] || rm -rf "$CI_WORK/venv" "$CI_WORK/old-src" "$CI_WORK/uv-cache"; exit $rc; }
trap cleanup EXIT

step() { echo; echo "=== $* (t+$((SECONDS-START))s)"; }

step "build $OLD_TAG wheels (git archive, no tag is moved)"
mkdir old-src
git -C "$REPO" archive "$OLD_TAG" | tar x -C old-src
( cd old-src && uv build --all-packages -o "$CI_WORK/dist-old" ) >/dev/null
ls dist-old | grep -E 'eidos_lab'
step "build head wheels"
( cd "$REPO" && uv build --all-packages -o "$CI_WORK/dist-new" ) >/dev/null
ls dist-new | grep -E 'eidos_lab'

step "install OLD wheels into a fresh venv"
uv venv -q --python "$PYTHON" venv
env -u PYTHONPATH uv pip install -q --python venv/bin/python --find-links dist-old eidos-lab
OLD_VERSION="$(venv/bin/python -c 'import eidos; print(eidos.__version__)')"
echo "installed eidos $OLD_VERSION"

# The echo test component and the fixture component bundle come from the old tag itself.
mkdir echo
git -C "$REPO" show "$OLD_TAG:tests/fixtures/components/echo/echo.py" > echo/echo.py
git -C "$REPO" show "$OLD_TAG:tests/fixtures/components/echo/eidos-component.json" > echo/eidos-component.json

step "OLD lab init + start"
init_args=(init --home "$EIDOS_HOME" --api-port "$LAB_API_PORT" --pg-port "$LAB_PG_PORT" --temporal-port "$LAB_TEMPORAL_PORT")
[ -n "${PG_BIN:-}" ] && init_args+=(--pg-bin "$PG_BIN")
[ -n "${TEMPORAL_SOURCE:-}" ] && init_args+=(--temporal-source "$TEMPORAL_SOURCE")
"$LAB_BIN" "${init_args[@]}" >/dev/null
"$LAB_BIN" start >/dev/null
"$LAB_BIN" doctor --json | python3 -c 'import json,sys; b=[c for c in json.load(sys.stdin) if c["status"]=="fail"]; print("old doctor fails:", b); sys.exit(1 if b else 0)'

step "seed real data with the OLD lab"
python3 "$HERE/upgrade/check.py" seed
python3 "$HERE/upgrade/check.py" snapshot >/dev/null
echo "old ledger snapshot: $(python3 -c 'import json;d=json.load(open("'"$OUT"'/snapshot.json"));print(d["revision"], d["counts"])')"

step "UPGRADE: install head wheels over the same venv, then lab upgrade (lab was running)"
env -u PYTHONPATH uv pip install -q --python venv/bin/python --reinstall --find-links dist-new eidos-lab
NEW_VERSION="$(venv/bin/python -c 'import eidos; print(eidos.__version__)')"
echo "eidos $OLD_VERSION -> $NEW_VERSION"
[ "$OLD_VERSION" != "$NEW_VERSION" ] || { echo "version did not change" >&2; exit 1; }
"$LAB_BIN" upgrade | tee "$OUT/upgrade.json"
python3 - <<PY
import json; d = json.load(open("$OUT/upgrade.json"))
assert d["restarted"] is True and d["ledger_revision"]["after"] == "0003", d
PY

step "verify on the upgraded lab"
for i in $(seq 1 30); do "$LAB_BIN" status --json >/dev/null 2>&1 && break; sleep 1; done
python3 "$HERE/upgrade/check.py" verify
step "done"
python3 -c 'import json; print("result.json ok =", json.load(open("'"$OUT"'/result.json"))["ok"])'
echo "wall time: $((SECONDS-START))s"
