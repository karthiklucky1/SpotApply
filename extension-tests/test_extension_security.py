"""Security-gate regression harness.

Loads the real extension, establishes a live copilot session (as a dashboard
visit does), then visits an UNRELATED site and asserts the extension stays
inert: no PII autofill into a newsletter box, no /api/save-answer beacon when
a dropdown option is picked, no false "submitted" report on a random form.

Every request the extension makes is recorded, so a leak shows up as a real
HTTP request, not an inference.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
EXT = str(HERE.parent / "extension")
PORT = 8908
BASE = f"http://127.0.0.1:{PORT}"

# Chromium: honour an explicit override, else the sandbox's preinstalled
# binary, else let Playwright resolve its own download.
_CHROME = os.environ.get("SPOTAPPLY_CHROMIUM") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
_LAUNCH_KW = {"executable_path": _CHROME} if _CHROME else {}


api_hits = []          # every /api/* request the extension makes
results = []


class Handler(http.server.SimpleHTTPRequestHandler):
    def _record(self):
        if "/api/" in self.path or "/application/" in self.path:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            api_hits.append({"method": self.command, "path": self.path, "body": body})

    def do_POST(self):
        self._record()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        if "/api/" in self.path:
            self._record()
            self.send_response(404)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *a):
        pass


FILL_PACK = {
    "app_id": 4242,
    "job_title": "Software Engineer",
    "company": "HarnessCo",
    "apply_url": f"{BASE}/apply.html",
    "first_name": "Alexandra", "last_name": "Nguyen",
    "email": "alexandra.nguyen@example.com", "phone": "+1 415 555 0199",
    "location": "San Francisco, CA",
    "linkedin_url": "https://linkedin.com/in/alexnguyen-test",
    "spotapply_url": BASE, "auth_token": "harness-test-token",
    "ai_answers": {},
}



def await_service_worker(ctx, timeout=45):
    """Wait for the MV3 background worker to register.

    An idle worker can be evicted (or not yet spawned) when the browser starts,
    so poll ctx.service_workers AND nudge it with a page load — visiting a page
    injects the content script, which wakes the worker.
    """
    deadline = time.time() + timeout
    nudged = False
    while time.time() < deadline:
        if ctx.service_workers:
            return ctx.service_workers[0]
        if not nudged and time.time() > deadline - timeout + 5:
            nudged = True
            try:
                page = ctx.new_page()
                page.goto("about:blank")
            except Exception:
                pass
        time.sleep(0.25)
    return None

def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def main():
    handler = partial(Handler, directory=str(HERE / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(Path(tempfile.mkdtemp(prefix="spotapply-ext-sec-"))),
            **_LAUNCH_KW,
            headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )
        sw = await_service_worker(ctx)
        if not sw:
            print("no service worker"); sys.exit(1)

        # Establish a live copilot session exactly as the dashboard does.
        trigger = ctx.new_page()
        trigger.goto(f"{BASE}/trigger.html")
        trigger.wait_for_timeout(1500)
        trigger.evaluate("""(pack) => window.postMessage(
            { type: 'HIREPATH_INIT_EXTENSION', pack }, '*')""", FILL_PACK)
        trigger.wait_for_timeout(1500)
        session = sw.evaluate("""() => new Promise(r => chrome.storage.local.get(
            ['spotapply_copilot_pack','spotapply_copilot_ts'],
            d => r({ pack: !!d.spotapply_copilot_pack, ts: !!d.spotapply_copilot_ts })))""")
        check("S0 live copilot session established (precondition)",
              session["pack"] and session["ts"], json.dumps(session))

        api_hits.clear()

        # Visit an unrelated site WITH the session live.
        blog = ctx.new_page()
        blog.goto(f"{BASE}/random-site.html")
        blog.wait_for_timeout(9000)   # well past the 2s + 3s auto-resume windows

        nl = blog.eval_on_selector("#nl-email", "el => el.value")
        check("S1 no PII autofilled into unrelated site's email box", not nl,
              f"newsletter box contains {nl!r}")

        # Pick a dropdown option — the old click-learner would POST this.
        blog.click("#opt-secret")
        blog.wait_for_timeout(2500)
        leaks = [h for h in api_hits if "save-answer" in h["path"]]
        check("S2 dropdown choice NOT sent to /api/save-answer", not leaks,
              json.dumps(leaks)[:200])

        # Submit an unrelated form — the old submit listener marked the app SUBMITTED.
        blog.evaluate("""() => document.getElementById('newsletter').requestSubmit()""")
        blog.wait_for_timeout(2500)
        submits = [h for h in api_hits if "/submit" in h["path"]]
        check("S3 unrelated form submit did NOT mark the application submitted",
              not submits, json.dumps(submits)[:200])

        still_live = sw.evaluate("""() => new Promise(r => chrome.storage.local.get(
            ['spotapply_copilot_pack'], d => r(!!d.spotapply_copilot_pack)))""")
        check("S4 fill session survived (pack not destroyed by the stray submit)",
              still_live)

        # Regression: the real ATS flow must STILL fill after all this gating.
        apply_page = ctx.new_page()
        apply_page.goto(f"{BASE}/apply.html")
        apply_page.wait_for_timeout(1200)
        sw.evaluate("""(pack) => new Promise(r => chrome.storage.local.set(
            { spotapply_fill_pack: pack, spotapply_auto_fill: true }, r))""", FILL_PACK)
        apply_page.reload()
        apply_page.wait_for_timeout(9000)
        first = apply_page.eval_on_selector("#first_name", "el => el.value")
        email = apply_page.eval_on_selector("#email", "el => el.value")
        check("S5 REGRESSION: real apply form still autofills",
              first == FILL_PACK["first_name"] and email == FILL_PACK["email"],
              f"first={first!r} email={email!r}")

        # And submit tracking must still work where it legitimately applies.
        api_hits.clear()
        apply_page.evaluate(
            """() => document.getElementById('application_form').requestSubmit()""")
        apply_page.wait_for_timeout(3000)
        real_submits = [h for h in api_hits if "/submit" in h["path"]]
        check("S6 REGRESSION: real application submit IS still reported",
              bool(real_submits), json.dumps(real_submits)[:160])

        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
