#!/bin/bash
set -euo pipefail

# MCP server prerequisites for Claude Code on the web (.mcp.json: railway, supabase).
# Local machines manage their own CLIs — do nothing there.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Railway's MCP server runs through the Railway CLI (`railway mcp`).
# Auth comes from RAILWAY_API_TOKEN set in the environment settings.
if ! command -v railway >/dev/null 2>&1; then
  npm install -g @railway/cli
fi

# Supabase's MCP server runs via npx; prefetch it so the first session's
# tool load isn't a cold npm install. Never fail the session over it.
npx -y @supabase/mcp-server-supabase@latest --version >/dev/null 2>&1 || true
