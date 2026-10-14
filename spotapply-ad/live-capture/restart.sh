#!/bin/bash
# Stop the demo server (if running), re-seed the fictional data, start it again.
L="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$L/env.sh"
# The run root: the repo's app/ (symlinked, read-only use) + a scratch data/.
mkdir -p "$L/runroot/data" "$L/logs"
ln -sfn "$(cd "$L/../.." && pwd)/app" "$L/runroot/app"
if [ -f "$L/server.pid" ] && kill -0 "$(cat "$L/server.pid")" 2>/dev/null; then
  kill "$(cat "$L/server.pid")"; sleep 2
fi
if curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:$DEMO_PORT/health" 2>/dev/null; then
  echo "port $DEMO_PORT still in use - refusing to continue"; exit 1
fi
"$L/venv/bin/python" "$L/seed.py" > "$L/logs/seed.log" 2>&1 || { echo "seed failed"; tail -20 "$L/logs/seed.log"; exit 1; }
cd "$L" && nohup "$L/venv/bin/python" "$L/launcher.py" > "$L/logs/server.log" 2>&1 &
for i in $(seq 1 60); do
  curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "http://127.0.0.1:$DEMO_PORT/health" 2>/dev/null | grep -q 200 && { echo "server up pid $(cat "$L/server.pid")"; exit 0; }
  sleep 1
done
echo "server did not start"; tail -30 "$L/logs/server.log"; exit 1
