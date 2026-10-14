#!/bin/bash
# Copy the captures the two videos use into ../public/:
#   live/    five phone screens (430x932 @2.5x) for the 30-second vertical ad
#   live60/  eight desktop screens (1280x800 @2x) for the 60-second explainer
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

D="$L/shots/desktop"; OUT60="$L/../public/live60"
mkdir -p "$OUT60"
cp "$D/01_board_top.png"              "$OUT60/board.png"         # W3: board + "Apply today"
cp "$D/02_match_report.png"           "$OUT60/match.png"         # W3: match report + fit breakdown
cp "$D/v3_sponsor_needs.png"          "$OUT60/match_sponsor.png" # W4: sponsorship card (F-1 OPT profile)
cp "$D/03_tailoring_studio.png"       "$OUT60/tailor.png"        # W5: tailored resume
cp "$D/03_tailoring_studio_cover.png" "$OUT60/tailor_cover.png"  # W5: cover letter tab
cp "$D/06_settings_one_click.png"     "$OUT60/settings.png"      # W6: One-click submit setting
cp "$D/v2_submitted_lumen.png"        "$OUT60/submitted.png"     # W7: earliness receipt
cp "$D/v2_outreach_lumen_email.png"   "$OUT60/outreach.png"      # W7: outreach email draft
echo "published to $OUT60"
