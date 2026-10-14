"""Run the real SpotApply web app locally (SQLite, single user "local").

Usage:   . live/env.sh && live/venv/bin/python live/launcher.py
Serves app.api.server:app on 127.0.0.1:$DEMO_PORT with every lane disabled
(LANES_ENABLED=0 in env.sh), so nothing scrapes, scores or calls an LLM.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _stubs  # noqa: E402,F401  (ML stubs + chdir to the run root)
import _netguard  # noqa: E402,F401  (refuse every outbound connect from this process)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("DEMO_PORT", "8765"))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.pid"), "w") as f:
        f.write(str(os.getpid()))
    uvicorn.run("app.api.server:app", host="127.0.0.1", port=port, log_level="info",
                workers=1, reload=False)
