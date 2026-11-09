#!/usr/bin/env bash
# Claude Desktop verification (after you merged claude-desktop-snippet.json yourself and restarted the app):
# the client log for server key `eidos` must show a successful initialize and a tools/list answer. Read-only.
#   check_desktop_log.sh [logfile]   (default ~/Library/Logs/Claude/mcp-server-eidos.log)
set -euo pipefail
LOG="${1:-$HOME/Library/Logs/Claude/mcp-server-eidos.log}"
[ -f "$LOG" ] || { echo "no log at $LOG (is the server key 'eidos'? was Claude Desktop restarted?)" >&2; exit 2; }
init=$(grep -c '"method":"initialize"' "$LOG" || true)
init_ok=$(grep -c '"serverInfo"' "$LOG" || true)
tools=$(grep -c '"name":"start_investigation"' "$LOG" || true)
listed=$(grep -c '"method":"tools/list"' "$LOG" || true)
echo "{\"log\": \"$LOG\", \"initialize_requests\": $init, \"initialize_results_with_serverInfo\": $init_ok, \"tools_list_requests\": $listed, \"tools_list_results_naming_start_investigation\": $tools}"
[ "$init_ok" -gt 0 ] && [ "$tools" -gt 0 ] && echo "PASS: initialize and tools/list succeeded" || { echo "FAIL: see $LOG" >&2; exit 1; }
