#!/usr/bin/env bash
# PreToolUse hook — read-only SQL runs without a prompt; anything else asks.
#
# Why: .claude/settings.json allows the Supabase execute_sql tools so research
# queries against production stop prompting. The claude.ai Supabase connector
# is NOT read-only (only the .mcp.json server runs --read-only), so the allow
# rule alone would also wave through an UPDATE/DELETE/DDL on production. This
# puts the prompt back for any statement sql_readonly.py cannot prove reads.
#
# Exit 0, no output   = read-only; the allow rule applies.
# Exit 0, "ask" JSON  = the user confirms this call. It never blocks.
set -uo pipefail

ask() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"%s"}}\n' "$1"
  exit 0
}

payload=$(cat)
py="${CLAUDE_PYTHON:-python3}"
command -v "$py" >/dev/null 2>&1 || ask "No python3 to check this SQL: confirm it only reads."

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
verdict=$(printf '%s' "$payload" | "$py" "$here/sql_readonly.py" 2>/dev/null) \
  || ask "Could not check this SQL: confirm it only reads."
[ "$verdict" = "read-only" ] || ask "This SQL may change the database ($verdict): confirm before it runs."
exit 0
