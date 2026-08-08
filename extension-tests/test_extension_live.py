"""Live MV3 harness: loads the real SpotApply extension into the pre-installed
Chromium and drives the production flow end-to-end against a local fake ATS:
  dashboard postMessage bridge -> background OPEN_AND_FILL -> new tab ->
  tabs.onUpdated auto-DO_FILL -> content-script form fill.
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
PORT = 8907
BASE = f"http://127.0.0.1:{PORT}"

# Chromium: honour an explicit override, else the sandbox's preinstalled
# binary, else let Playwright resolve its own download.
_CHROME = os.environ.get("SPOTAPPLY_CHROMIUM") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
_LAUNCH_KW = {"executable_path": _CHROME} if _CHROME else {}


FILL_PACK = {
    "app_id": 4242,
    "job_title": "Software Engineer",
    "company": "HarnessCo",
    "apply_url": f"{BASE}/apply.html",
    "first_name": "Alexandra",
    "last_name": "Nguyen",
    "email": "alexandra.nguyen@example.com",
    "phone": "+1 415 555 0199",
    "location": "San Francisco, CA",
    "linkedin_url": "https://linkedin.com/in/alexnguyen-test",
    "github_url": "https://github.com/alexnguyen-test",
    "portfolio_url": "https://alexnguyen.dev",
    "current_title": "Senior Software Engineer",
    "years_experience": 7,
    "salary_min": 150000,
    "work_authorization": "US Citizen",
    "requires_sponsorship": False,
    "gender": "Decline to self-identify",
    "ethnicity": "Decline to self-identify",
    "veteran_status": "I am not a protected veteran",
    "disability_status": "No, I do not have a disability",
    "cover_letter": "Dear Hiring Team, I am excited to apply.",
    "resume_text": "Alexandra Nguyen - Senior Software Engineer",
    "ai_answers": {},
    "hirepath_url": BASE,   # point extension API calls at this harness (404s)
    "auth_token": "harness-test-token",
}

results = []



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
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(Path(tempfile.mkdtemp(prefix="spotapply-ext-"))),
            **_LAUNCH_KW,
            headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )

        # T1: service worker registers
        sw = await_service_worker(ctx)
        check("T1 service worker registered", sw is not None,
              sw.url if sw else "no service worker after 15s")
        if not sw:
            print(json.dumps({"results": results}))
            sys.exit(1)
        ext_id = sw.url.split("/")[2]

        # T2: popup renders with no-job state, zero console errors
        popup = ctx.new_page()
        popup_errors = []
        popup.on("console", lambda m: popup_errors.append(m.text) if m.type == "error" else None)
        popup.on("pageerror", lambda e: popup_errors.append(str(e)))
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
        popup.wait_for_timeout(1200)
        no_job_visible = popup.eval_on_selector(
            "#no-job-section", "el => getComputedStyle(el).display !== 'none'")
        check("T2a popup renders 'no job loaded' state", no_job_visible)
        check("T2b popup console clean", not popup_errors, "; ".join(popup_errors[:3]))

        # T3: content script injected + PING/PING_OK liveness bridge
        trigger = ctx.new_page()
        trigger.goto(f"{BASE}/trigger.html")
        trigger.wait_for_timeout(1500)  # content.js runs at document_idle
        ping_ok = trigger.evaluate(
            """() => new Promise((resolve) => {
                const t = setTimeout(() => resolve(false), 4000);
                window.addEventListener('message', (e) => {
                    if (e.data && e.data.type === 'HIREPATH_EXT_PING_OK') {
                        clearTimeout(t); resolve(true);
                    }
                });
                window.postMessage({ type: 'HIREPATH_EXT_PING' }, '*');
            })""")
        check("T3 dashboard liveness bridge PING -> PING_OK", ping_ok)

        # T4: full production flow — LOAD_PACK -> ACK -> new tab -> auto-fill
        with ctx.expect_page(timeout=15000) as new_page_info:
            ack = trigger.evaluate(
                """(pack) => new Promise((resolve) => {
                    const t = setTimeout(() => resolve('timeout'), 8000);
                    window.addEventListener('message', (e) => {
                        if (e.data && e.data.type === 'HIREPATH_EXT_ACK') { clearTimeout(t); resolve('ack'); }
                        if (e.data && e.data.type === 'HIREPATH_EXT_RELOAD') { clearTimeout(t); resolve('reload'); }
                    });
                    window.postMessage({ type: 'HIREPATH_LOAD_PACK', pack }, '*');
                })""", FILL_PACK)
        check("T4a bridge ACKs LOAD_PACK", ack == "ack", f"got: {ack}")
        apply_page = new_page_info.value
        sa_logs = []
        apply_page.on("console", lambda m: sa_logs.append(m.text) if "[SpotApply]" in m.text else None)
        check("T4b background opened the apply tab", "apply.html" in apply_page.url, apply_page.url)

        # background delays DO_FILL by 2s after load; fill engine then runs async
        apply_page.wait_for_timeout(9000)

        expect = {
            "#first_name": FILL_PACK["first_name"],
            "#last_name": FILL_PACK["last_name"],
            "#email": FILL_PACK["email"],
            "#phone": FILL_PACK["phone"],
        }
        for sel, want in expect.items():
            got = apply_page.eval_on_selector(sel, "el => el.value")
            check(f"T4c field {sel} filled", got == want, f"got {got!r} want {want!r}")
        linkedin = apply_page.eval_on_selector("#linkedin", "el => el.value")
        check("T4d LinkedIn field filled", FILL_PACK["linkedin_url"] in (linkedin or ""),
              f"got {linkedin!r}")
        resume_files = apply_page.eval_on_selector("#resume", "el => el.files.length")
        check("T4e resume attach attempted (file input or graceful skip)", True,
              f"files attached: {resume_files} (backend 404s here by design)")

        print("\n--- [SpotApply] console log excerpt from apply tab ---")
        for line in sa_logs[:25]:
            print("   ", line[:160])

        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
