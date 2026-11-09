#!/usr/bin/env bash
# Make the pinned Temporal server available to `lab init` without depending on GitHub's release CDN.
#   1. a verified archive in $EIDOS_TEMPORAL_ARCHIVE_CACHE (restored by actions/cache): nothing to do;
#   2. else download the pinned release archive (retries) and verify its sha256 into that cache;
#   3. else build the pinned version from source through the Go module proxy (go.sum-verified modules) and
#      export TEMPORAL_SOURCE (lab init records its provenance as operator-supplied/source-built).
# FORCE_SOURCE_BUILD=1 skips 1-2 (used by the CI job that proves the fallback).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Quoted heredoc at top level, output via a file: bash 3.2 (macOS) mis-parses heredocs inside <(...)/$(...).
PINFILE=$(mktemp)
python3 - "$REPO/src/eidos/supervisor/services.py" > "$PINFILE" <<'PY'
import ast, platform, sys
src = open(sys.argv[1]).read(); tree = ast.parse(src); env = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("TEMPORAL_VERSION", "_REL"):
        env[node.targets[0].id] = ast.literal_eval(node.value)
osn = platform.system().lower(); arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
table = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TEMPORAL_RELEASES":
        for k, v in zip(node.value.keys, node.value.values):
            key = tuple(ast.literal_eval(k)); fmt = ast.literal_eval(v.elts[0].args[0]); sha = ast.literal_eval(v.elts[1])
            table[key] = (env["_REL"].format(fmt), sha)
url, sha = table[(osn, arch)]
print(env["TEMPORAL_VERSION"], url, sha)
PY
read -r VERSION URL SHA < "$PINFILE"
rm -f "$PINFILE"
CACHE="${EIDOS_TEMPORAL_ARCHIVE_CACHE:-${RUNNER_TEMP:-/tmp}/eidos-temporal}"
ARCHIVE="$CACHE/temporal-server-$SHA.tgz"
mkdir -p "$CACHE"
if [ -z "${FORCE_SOURCE_BUILD:-}" ]; then
  ok=""
  if [ -f "$ARCHIVE" ] && echo "$SHA  $ARCHIVE" | shasum -a 256 -c - >/dev/null 2>&1; then
    echo "temporal: verified cached archive $ARCHIVE"; ok=1
  elif curl -fsSL --retry 4 --retry-all-errors --retry-delay 5 -o "$ARCHIVE.part" "$URL" \
     && echo "$SHA  $ARCHIVE.part" | shasum -a 256 -c - >/dev/null 2>&1; then
    mv "$ARCHIVE.part" "$ARCHIVE"; echo "temporal: downloaded and verified $URL"; ok=1
  fi
  if [ -n "$ok" ]; then
    if [ -n "${EXTRACT_TO_SOURCE:-}" ]; then  # for installs whose own lab init cannot use the cache (older releases)
      OUT="${TEMPORAL_SRC_OUT:-${RUNNER_TEMP:-/tmp}/temporal-src}"; mkdir -p "$OUT"
      tar -xzf "$ARCHIVE" -C "$OUT" temporal-server temporal-sql-tool
      printf '{"kind": "release-archive", "url": "%s", "sha256": "%s", "verified_against_pinned": true}\n' "$URL" "$SHA" > "$OUT/provenance.json"
      echo "TEMPORAL_SOURCE=$OUT" >> "${GITHUB_ENV:-/dev/null}"
    fi
    exit 0
  fi
  rm -f "$ARCHIVE.part"
  echo "temporal: release download unavailable; building v$VERSION from source"
fi
command -v go >/dev/null 2>&1 || { echo "temporal: the source-build fallback needs a Go toolchain on PATH (none found)" >&2; exit 127; }
OUT="${TEMPORAL_SRC_OUT:-${RUNNER_TEMP:-/tmp}/temporal-src}"
mkdir -p "$OUT"
export GOTOOLCHAIN=auto GOBIN="$OUT/gobin" GOFLAGS=-mod=mod
go install "go.temporal.io/server/cmd/server@v$VERSION" "go.temporal.io/server/cmd/tools/sql@v$VERSION"
cp "$GOBIN/server" "$OUT/temporal-server"; cp "$GOBIN/sql" "$OUT/temporal-sql-tool"
python3 - "$OUT" "$VERSION" <<'PY'
import hashlib, json, subprocess, sys
out, ver = sys.argv[1], sys.argv[2]
info = subprocess.run(["go", "version", "-m", f"{out}/temporal-server"], capture_output=True, text=True).stdout
json.dump({"kind": "source-built", "module": "go.temporal.io/server", "version": f"v{ver}",
           "built_with": subprocess.run(["go", "version"], capture_output=True, text=True).stdout.strip(),
           "verification": "Go module proxy + checksum database (go.sum); NOT the checksum-pinned release archive",
           "sha256": {n: hashlib.sha256(open(f"{out}/{n}", "rb").read()).hexdigest()
                      for n in ("temporal-server", "temporal-sql-tool")},
           "go_version_m": info[:4000]}, open(f"{out}/provenance.json", "w"), indent=2)
PY
echo "TEMPORAL_SOURCE=$OUT" >> "${GITHUB_ENV:-/dev/null}"
echo "temporal: source-built server in $OUT (export TEMPORAL_SOURCE=$OUT)"
