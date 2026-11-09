#!/usr/bin/env bash
# Non-destructive MCP client runs against a lab. Prints the commands; with --run executes them and verifies against the lab.
#   run_clients.sh --home EIDOS_HOME --lab /path/to/lab --project P --investigation I [--out DIR] [--claude PATH] [--codex PATH]
#                  [--only claude|codex] [--write] [--run]
# --write adds a write-path run per client: one steer_investigation call with a fresh marker, checked against the lab record.
# Never edits ~/.claude*, ~/.codex* or the Claude Desktop config: configs are passed per invocation (see gen_configs.py).
# The lab must be running (`lab start`). Exit status is non-zero if any selected client's receipt fails.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN=0; WRITE=0; ONLY=""; OUT="./mcp-client-run"; CLAUDE=claude; CODEX=codex; HOME_=""; LAB=""; PROJECT=""; INV=""
while [ $# -gt 0 ]; do case "$1" in
  --home) HOME_="$2"; shift 2;; --lab) LAB="$2"; shift 2;; --project) PROJECT="$2"; shift 2;;
  --investigation) INV="$2"; shift 2;; --out) OUT="$2"; shift 2;; --claude) CLAUDE="$2"; shift 2;;
  --codex) CODEX="$2"; shift 2;; --run) RUN=1; shift;; --write) WRITE=1; shift;; --only) ONLY="$2"; shift 2;; *) echo "unknown argument $1" >&2; exit 2;; esac; done
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

MARK="mcpq-$(date -u +%Y%m%dT%H%M%SZ)-$$"
WPROMPT() {  # client name -> write-path prompt (one mutating call, then a read-back)
  echo "You are verifying the write path of a research lab through its MCP server named eidos. Use only eidos tools. \
Project id: ${PROJECT}. Investigation id: ${INV}. \
(1) Call steer_investigation exactly once with project ${PROJECT}, investigation_id ${INV}, command_key ${MARK}-$1, and instruction \
\"MCP write-path check from $1 (${MARK}-$1): no action needed.\" If the call returns an error, report it and do not retry with a different key. \
(2) Call get_investigation for that investigation and check that body.steering contains your instruction. \
Finish with one line of JSON: {\"investigation\": \"${INV}\", \"status\": \"<status>\", \"steered\": <true|false>, \"revision\": <revision>}."
}

# (macOS ships bash 3.2: no mapfile)
CODEX_C=()
while IFS= read -r line; do CODEX_C+=("$line"); done < <(python3 -c "import json,sys; [print(x) for x in json.load(open(sys.argv[1]))]" "$OUT/configs/codex-overrides.json")
CLAUDE_BASE=("$CLAUDE" -p --mcp-config "$OUT/configs/claude-code-mcp.json" --strict-mcp-config --allowedTools 'mcp__eidos__*'
             --max-turns 6 --no-session-persistence --output-format stream-json --verbose)
CODEX_BASE=("$CODEX" exec --json --skip-git-repo-check --ephemeral)
for c in "${CODEX_C[@]}"; do CODEX_BASE+=(-c "$c"); done
CLAUDE_CMD=("${CLAUDE_BASE[@]}" "$PROMPT")
CODEX_CMD=("${CODEX_BASE[@]}" "$PROMPT")

show() { printf '%q ' "$@"; echo; }
echo "# Claude Code"; show "${CLAUDE_CMD[@]}"
echo "# Codex CLI";   show "${CODEX_CMD[@]}"
echo "# Claude Desktop (manual): merge $OUT/configs/claude-desktop-snippet.json into its config, restart it, then:"
echo "#   $HERE/check_desktop_log.sh   (greps ~/Library/Logs/Claude/mcp-server-eidos.log)"
[ "$RUN" = 1 ] || { echo "# print mode: nothing executed (add --run)"; exit 0; }

rc_all=0
run_one() {  # name client marker cmd...   (marker "" = read-only run)
  local name="$1" client="$2" marker="$3"; shift 3
  echo "== running $name"
  set +e; EIDOS_HOME="$HOME_" "$@" < /dev/null > "$OUT/$name.raw.jsonl" 2> "$OUT/$name.stderr.txt"; local rc=$?; set -e
  local extra=(); [ -n "$marker" ] && extra=(--steer-marker "$marker")
  EIDOS_HOME="$HOME_" python3 "$HERE/verify_receipt.py" --client "$client" --raw "$OUT/$name.raw.jsonl" --lab "$LAB" \
    --project "$PROJECT" --investigation "$INV" --receipt "$OUT/$name.receipt.json" --exit-code "$rc" ${extra[@]+"${extra[@]}"} || rc_all=1
}
if [ "$ONLY" != codex ]; then
  run_one claude claude "" "${CLAUDE_CMD[@]}"
  [ "$WRITE" = 1 ] && run_one claude-write claude "$MARK-claude" "${CLAUDE_BASE[@]}" "$(WPROMPT claude)"
fi
if [ "$ONLY" != claude ]; then
  run_one codex codex "" "${CODEX_CMD[@]}"
  [ "$WRITE" = 1 ] && run_one codex-write codex "$MARK-codex" "${CODEX_BASE[@]}" "$(WPROMPT codex)"
fi
echo "receipts in $OUT/*.receipt.json (exit $rc_all)"
exit "$rc_all"
