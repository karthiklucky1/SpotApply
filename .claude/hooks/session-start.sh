#!/bin/bash
set -euo pipefail

# MCP server prerequisites for Claude Code on the web (.mcp.json: railway, supabase).
# Local machines manage their own CLIs — do nothing there.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Both MCP servers launch via npx (.mcp.json), which fetches on demand —
# MCP servers spawn before this hook finishes, so nothing may depend on an
# install done here. Prefetching just warms the npx cache; a global railway
# install additionally gives Bash the CLI for deploys/logs. Auth comes from
# RAILWAY_API_TOKEN / SUPABASE_ACCESS_TOKEN in the environment settings.
# Never fail the session over any of it.
npx -y @railway/cli --version >/dev/null 2>&1 || true
npx -y @supabase/mcp-server-supabase@latest --version >/dev/null 2>&1 || true
command -v railway >/dev/null 2>&1 || npm install -g @railway/cli || true
