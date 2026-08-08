"""Regressions for the payload-handoff class of bug.

A live test across Greenhouse, Ashby and Lever produced: "13 fields filled" on
a completely empty form, and the literal string "undefined undefined" written
into name fields and highlighted green as a success. Root cause: the
dashboard's INIT_EXTENSION pack is credentials-only, it was being stored as the
copilot's FILL pack, and every profile lookup on it was undefined.

These checks pin the whole chain:
  P1  an init (auth-only) pack never becomes a fill pack
  P2  a fill driven by an unfillable pack writes NOTHING
  P3  ...and says so instead of reporting success
  P4  "undefined" never reaches a field, even if a branch tries
  P5  a real pack still fills (the fix must not just disable filling)
  P6  demographic questions are left alone unless explicitly opted in
  P7  ...and ARE answered once the user opts in
"""
import http.server
import json
import os
import sys
import tempfile
import threading
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
EXT = str(HERE.parent / "extension")
PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"
_CHROME = os.environ.get("SPOTAPPLY_CHROMIUM") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
_LAUNCH_KW = {"executable_path": _CHROME} if _CHROME else {}

# Exactly what dashboard.html broadcasts on INIT_EXTENSION: credentials, no
# profile whatsoever.
INIT_PACK = {
    "spotapply_url": BASE,
    "auth_token": "harness-token",
    "refresh_token": "harness-refresh",
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon",
}

REAL_PACK = dict(INIT_PACK, **{
    "app_id": 77,
    "job_title": "Senior Engineer",
    "company": "HarnessCo",
    "first_name": "Alexandra",
    "last_name": "Nguyen",
    "email": "alexandra.nguyen@example.com",
    "phone": "+1 415 555 0199",
    "location": "San Francisco, CA",
    "gender": "Decline to self-identify",
    "ethnicity": "Decline to self-identify",
    "veteran_status": "I am not a protected veteran",
    "disability_status": "No, I do not have a disability",
})

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


class Stub(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._json({"ok": True, "answers": []})


def sw_of(ctx):
    return ctx.service_workers[0]


def seed(ctx, pack):
    sw_of(ctx).evaluate(
        """(p) => new Promise((r) => chrome.storage.local.set({
            spotapply_fill_pack: p, spotapply_copilot_pack: p,
            spotapply_copilot_ts: Date.now(), spotapply_auto_fill: false }, r))""", pack)


def fill(ctx, url, pack):
    page = ctx.new_page()
    logs = []
    page.on("console", lambda m: logs.append(m.text))
    page.goto(url)
    page.wait_for_timeout(1400)
    sw_of(ctx).evaluate(
        """(a) => new Promise((res) => chrome.tabs.query({}, (tabs) => {
            const t = tabs.find(t => t.url && t.url.includes(a.needle));
            if (!t) return res('no-tab');
            chrome.tabs.sendMessage(t.id, { type: 'DO_FILL', fillPack: a.pack }, () => res('sent'));
        }))""", {"needle": url.rsplit("/", 1)[-1], "pack": pack})
    page.wait_for_timeout(5000)
    return page, logs


def main():
    handler = partial(Stub, directory=str(HERE / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    with sync_playwright() as p, tempfile.TemporaryDirectory() as prof:
        ctx = p.chromium.launch_persistent_context(
            prof, headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"], **_LAUNCH_KW)
        for _ in range(60):
            if ctx.service_workers:
                break
            ctx.pages[0].wait_for_timeout(250)
        if not ctx.service_workers:
            print("FAIL: no service worker")
            sys.exit(1)

        # P1 — INIT_EXTENSION must stash auth WITHOUT creating a fill session.
        sw_of(ctx).evaluate(
            """() => new Promise((r) => chrome.storage.local.clear(r))""")
        trigger = ctx.new_page()
        trigger.goto(f"{BASE}/trigger.html")
        trigger.wait_for_timeout(1200)
        trigger.evaluate("""(pack) => window.postMessage(
            { type: 'SPOTAPPLY_INIT_EXTENSION', pack }, '*')""", INIT_PACK)
        trigger.wait_for_timeout(1200)
        stored = sw_of(ctx).evaluate(
            """() => new Promise((r) => chrome.storage.local.get(
                ['spotapply_copilot_pack','spotapply_fill_pack','spotapply_auth'],
                d => r({ copilot: !!d.spotapply_copilot_pack, fill: !!d.spotapply_fill_pack,
                         auth: !!(d.spotapply_auth && d.spotapply_auth.access_token) })))""")
        check("P1a init pack did NOT become a copilot fill pack", stored["copilot"] is False, str(stored))
        check("P1b init pack did NOT become a fill pack", stored["fill"] is False, str(stored))
        check("P1c auth WAS stashed from the init pack", stored["auth"] is True, str(stored))
        trigger.close()

        # P2/P3/P4 — filling with the credentials-only pack must write nothing.
        seed(ctx, INIT_PACK)
        page, logs = fill(ctx, f"{BASE}/greenhouse.html", INIT_PACK)
        # Exclude submit/button inputs — their `value` is the button caption
        # baked into the fixture, not something the extension wrote.
        vals = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                    "input:not([type='submit']):not([type='button']), textarea, select"))
                .map(e => String(e.value || '')).filter(Boolean)""")
        check("P2 nothing written from an unfillable pack", vals == [], f"wrote {vals[:4]}")
        check("P3 abort is reported, not silent success",
              any("no profile data" in l for l in logs),
              next((l for l in logs if "aborted" in l.lower()), "no abort log"))
        check("P4 no literal 'undefined' anywhere on the form",
              not any("undefined" in v.lower() for v in vals), str(vals[:4]))
        # Read the message out of the shadow root's body, not the whole root
        # (which would include the <style> block).
        overlay = page.evaluate(
            """() => { const h = document.getElementById('hp-copilot-overlay');
                       if (!h) return '';
                       const root = h.shadowRoot || h;
                       const body = root.querySelector('.body');
                       return (body ? body.textContent : root.textContent).trim(); }""")
        check("P4b user is told why nothing happened", "isn't connected" in (overlay or ""),
              (overlay or "")[:70])
        page.close()

        # P5 — a real pack still fills (the guard must not disable filling).
        seed(ctx, REAL_PACK)
        page, _ = fill(ctx, f"{BASE}/greenhouse.html", REAL_PACK)
        v = lambda s: page.eval_on_selector(s, "e => e.value")
        check("P5a real pack fills first name", v("#first_name") == "Alexandra", v("#first_name"))
        check("P5b real pack fills email", v("#email") == REAL_PACK["email"], v("#email"))

        # P6 — demographics untouched while the opt-in is off (the default).
        check("P6a gender left for the user (opt-in off)", v("#gender") == "", repr(v("#gender")))
        check("P6b veteran left for the user (opt-in off)", v("#veteran_status") == "",
              repr(v("#veteran_status")))
        page.close()

        # P7 — once opted in, they ARE answered.
        sw_of(ctx).evaluate(
            """() => new Promise((r) => chrome.storage.local.set({ spotapply_eeo_autofill: true }, r))""")
        seed(ctx, REAL_PACK)
        page, _ = fill(ctx, f"{BASE}/greenhouse.html", REAL_PACK)
        v = lambda s: page.eval_on_selector(s, "e => e.value")
        check("P7 gender answered after explicit opt-in",
              v("#gender") == "Decline to self-identify", repr(v("#gender")))
        page.close()

        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
