#!/bin/bash
# Copy the reel's screen recordings (record_reel.py, record_extension.py) into
# ../public/reel/footage/ with their marks.
set -euo pipefail
L="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$L/../public/reel/footage"
mkdir -p "$OUT"
for take in phone_match phone_gap desk_tailor desk_dashboard_fill desk_extension; do
  cp "$L/shots/reel/$take.mp4" "$L/shots/reel/$take.json" "$OUT/"
done
echo "published to $OUT"
