#!/usr/bin/env bash
# PostToolUse hook — validate a Jinja template right after Claude edits it.
#
# Why: app/templates/dashboard.html is ~6.9k lines with large inline <script>
# blocks. A stray brace or a broken Jinja tag there is invisible to pytest (no
# test renders the whole dashboard) but breaks the entire app in the browser.
# CLAUDE.md says to validate by hand after every template edit; this runs it.
#
# Exit 0  = not a template, or the template is valid.
# Exit 2  = blocking error; stderr is fed back to Claude so it fixes it in-turn.
set -uo pipefail

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_response.filePath // empty')
[ -n "$file" ] || exit 0

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# Only Jinja templates. Matches both absolute and repo-relative paths.
case "$file" in
  */app/templates/*.html|app/templates/*.html) ;;
  *) exit 0 ;;
esac

py="${CLAUDE_PYTHON:-python3}"
command -v "$py" >/dev/null 2>&1 || exit 0   # no interpreter: stay out of the way

if ! out=$(cd "$root" && "$py" scripts/validate_templates.py "$file" 2>&1); then
  printf 'Template validation FAILED — %s\n\n%s\n\nFix the template before continuing.\n' \
    "$file" "$out" >&2
  exit 2
fi
exit 0
