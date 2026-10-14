#!/bin/bash
# Copy the five captures the ad uses into ../public/live/ (phone viewport).
set -euo pipefail
L="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P="$L/shots/phone"; OUT="$L/../public/live"
mkdir -p "$OUT"
cp "$P/01_board_divider.png"        "$OUT/board.png"      # S3: board + "Apply today"
cp "$P/02_match_report.png"         "$OUT/match.png"      # S3: match report drawer
cp "$P/03_tailoring_studio_scroll01.png" "$OUT/tailor.png" # S4: Tailoring Studio
cp "$P/v2_submitted_lumen.png"      "$OUT/submitted.png"  # S6: earliness receipt
cp "$P/v2_outreach_lumen_email.png" "$OUT/outreach.png"   # S6: outreach email draft
echo "published to $OUT"
