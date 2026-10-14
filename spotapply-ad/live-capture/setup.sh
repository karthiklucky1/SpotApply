#!/bin/bash
# One-time setup for the live captures: a Python 3.11 venv with the app's
# requirements MINUS the ML stack (exactly what CI installs), plus Playwright
# pinned to the version that matches the preinstalled Chromium.
set -euo pipefail
L="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$L/../.." && pwd)"
grep -viE '^(torch|faiss-cpu|sentence-transformers)' "$REPO/requirements.txt" > "$L/reqs.txt"
python3.11 -m venv "$L/venv"
"$L/venv/bin/pip" install -q --upgrade pip
"$L/venv/bin/pip" install -q -r "$L/reqs.txt" "playwright==${PLAYWRIGHT_VERSION:-1.56.0}"
echo "ok: $L/venv"
