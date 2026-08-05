#!/usr/bin/env bash
# PostToolUse hook — rebuild the committed Tailwind stylesheet after a template
# edit, so the compiled CSS never drifts from the classes the template uses.
#
# Why: both compiled stylesheets are COMMITTED and guarded by
# tests/test_landing_assets.py, which only fails later, in CI, after the stale
# file is already in a commit. Each config scans exactly one template:
#   app/templates/landing.html   -> app/static/tailwind-landing.css  (build:css)
#   app/templates/dashboard.html -> app/static/tailwind.css (build:css:dashboard)
# so we rebuild only the one that can possibly have changed.
#
# Exit 0 = nothing to do, or rebuild succeeded (or node deps are absent, in
#          which case Claude is told the CSS is now stale).
# Exit 2 = the build ran and failed.
set -uo pipefail

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_response.filePath // empty')
[ -n "$file" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

case "$file" in
  */app/templates/landing.html|app/templates/landing.html)
      target="build:css";           out="app/static/tailwind-landing.css" ;;
  */app/templates/dashboard.html|app/templates/dashboard.html)
      target="build:css:dashboard"; out="app/static/tailwind.css" ;;
  *)  exit 0 ;;
esac

# node_modules is gitignored and not always installed. Don't block the edit —
# tell Claude the committed CSS is now stale so it can decide what to do.
if [ ! -x "$root/node_modules/.bin/tailwindcss" ]; then
  jq -nc --arg o "$out" --arg t "$target" '{
    systemMessage: ("Tailwind not installed — " + $o + " was NOT rebuilt."),
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: ("The committed stylesheet " + $o + " is now stale relative to the template you just edited, because node_modules is missing. Run `npm install && npm run " + $t + "` and commit the output, or tell the user to. tests/test_landing_assets.py will fail until this is done.")
    }
  }'
  exit 0
fi

if ! log=$(cd "$root" && npm run --silent "$target" 2>&1); then
  printf 'Tailwind build FAILED (npm run %s)\n\n%s\n' "$target" "$log" >&2
  exit 2
fi

jq -nc --arg o "$out" '{systemMessage: ("Rebuilt " + $o)}'
exit 0
