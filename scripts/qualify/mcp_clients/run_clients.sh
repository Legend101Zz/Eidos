#!/usr/bin/env bash
# Non-destructive MCP client runs against a lab. Prints the commands; with --run executes them and verifies against the lab.
#   run_clients.sh --home EIDOS_HOME --lab /path/to/lab --project P --investigation I [--out DIR] [--claude PATH] [--codex PATH] [--run]
# Never edits ~/.claude*, ~/.codex* or the Claude Desktop config: configs are passed per invocation (see gen_configs.py).
# The lab must be running (`lab start`). Exit status is non-zero if any selected client's receipt fails.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN=0; OUT="./mcp-client-run"; CLAUDE=claude; CODEX=codex; HOME_=""; LAB=""; PROJECT=""; INV=""
while [ $# -gt 0 ]; do case "$1" in
  --home) HOME_="$2"; shift 2;; --lab) LAB="$2"; shift 2;; --project) PROJECT="$2"; shift 2;;
  --investigation) INV="$2"; shift 2;; --out) OUT="$2"; shift 2;; --claude) CLAUDE="$2"; shift 2;;
  --codex) CODEX="$2"; shift 2;; --run) RUN=1; shift;; *) echo "unknown argument $1" >&2; exit 2;; esac; done
[ -n "$HOME_" ] && [ -n "$LAB" ] && [ -n "$PROJECT" ] && [ -n "$INV" ] || { echo "need --home --lab --project --investigation" >&2; exit 2; }
mkdir -p "$OUT"
python3 "$HERE/gen_configs.py" --home "$HOME_" --lab "$LAB" --out "$OUT/configs" >/dev/null

# Fixed, read-only prompt. The eidos MCP server has no list-projects tool and no report resource (src/eidos/mcp/tools.py,
# gateway.py RESOURCE_TEMPLATES), so the project id is given and the "report" read is the investigation resource.
PROMPT="You are verifying a research lab through its MCP server named eidos. Use only eidos tools and resources, make no changes. \
Project id: ${PROJECT}. Investigation id: ${INV}. Do these four things: \
(1) call get_investigation for that investigation and note its status; \
(2) call list_events for it with limit 5; \
(3) call list_components for the project and count the active components; \
(4) read the resource lab://projects/${PROJECT}/investigations/${INV}. \
Finish with one line of JSON: {\"investigation\": \"<id>\", \"status\": \"<status>\", \"components\": <count>, \"events_read\": <n>}."

# (macOS ships bash 3.2: no mapfile)
CODEX_C=()
while IFS= read -r line; do CODEX_C+=("$line"); done < <(python3 -c "import json,sys; [print(x) for x in json.load(open(sys.argv[1]))]" "$OUT/configs/codex-overrides.json")
CLAUDE_CMD=("$CLAUDE" -p --mcp-config "$OUT/configs/claude-code-mcp.json" --strict-mcp-config --allowedTools 'mcp__eidos__*'
            --max-turns 6 --output-format stream-json --verbose "$PROMPT")
CODEX_CMD=("$CODEX" exec --json --skip-git-repo-check)
for c in "${CODEX_C[@]}"; do CODEX_CMD+=(-c "$c"); done
CODEX_CMD+=("$PROMPT")

show() { printf '%q ' "$@"; echo; }
echo "# Claude Code"; show "${CLAUDE_CMD[@]}"
echo "# Codex CLI";   show "${CODEX_CMD[@]}"
echo "# Claude Desktop (manual): merge $OUT/configs/claude-desktop-snippet.json into its config, restart it, then:"
echo "#   $HERE/check_desktop_log.sh   (greps ~/Library/Logs/Claude/mcp-server-eidos.log)"
[ "$RUN" = 1 ] || { echo "# print mode: nothing executed (add --run)"; exit 0; }

rc_all=0
run_one() {  # name cmd...
  local name="$1"; shift
  echo "== running $name"
  set +e; EIDOS_HOME="$HOME_" "$@" > "$OUT/$name.raw.jsonl" 2> "$OUT/$name.stderr.txt"; local rc=$?; set -e
  python3 "$HERE/verify_receipt.py" --client "$name" --raw "$OUT/$name.raw.jsonl" --lab "$LAB" --project "$PROJECT" \
    --investigation "$INV" --receipt "$OUT/$name.receipt.json" --exit-code "$rc" || rc_all=1
}
run_one claude "${CLAUDE_CMD[@]}"
run_one codex "${CODEX_CMD[@]}"
echo "receipts: $OUT/claude.receipt.json $OUT/codex.receipt.json (exit $rc_all)"
exit "$rc_all"
