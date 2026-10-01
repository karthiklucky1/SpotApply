#!/usr/bin/env python3
"""Regression suite for the 2026-09-30 extension audit — page-level cases.

Loads the real MV3 extension in Chromium. Employer hostnames
(boards.greenhouse.io, jobs.lever.co, …) are answered by THIS harness via
request routing, never by a real employer; the SpotApply API is a local stub.
Profiles and forms are synthetic. Nothing is submitted anywhere.

Each check is an expectation the audit found violated. The rule-level cases
(screening wording, work authorization, host matching, accepted formats) run
in pytest: tests/test_extension_rules.py.

    python3 extension-tests/test_extension_audit.py
"""
import base64
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
PORT = 8911
BASE = f"http://127.0.0.1:{PORT}"
_CHROME = os.environ.get("SPOTAPPLY_CHROMIUM") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
_LAUNCH_KW = {"executable_path": _CHROME} if _CHROME else {}

REQUESTS = []           # (method, path) the extension sent to the stub API
STATE = {"pack": None, "recall": {}, "resume_code": 200}
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def pack_for(url, **over):
    p = {
        "app_id": 4242, "job_title": "Software Engineer", "company": "Example Employer",
        "apply_url": url,
        "first_name": "Alexandra", "last_name": "Nguyen",
        "email": "alexandra.nguyen@example.com", "phone": "+1 415 555 0199",
        "location": "Toronto, ON, Canada", "residence_country": "Canada",
        "current_title": "Software Engineer", "years_experience": 7,
        "key_skills": "Python, SQL", "degree": "Bachelor of Arts",
        "work_authorization": "US Citizen", "requires_sponsorship": False,
        "gender": "Female", "ethnicity": "Decline to self-identify",
        "veteran_status": "Decline to self-identify", "disability_status": "Decline to self-identify",
        "work_experience": [{"company": "Example Employer", "title": "Software Engineer",
                             "end_date": "Present"}],
        "education": [], "ai_answers": {}, "cover_letter": "",
        "spotapply_url": BASE, "auth_token": "harness-test-token",
    }
    p.update(over)
    return p


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        REQUESTS.append(("GET", self.path))
        if self.path.endswith("/resume"):
            if STATE["resume_code"] != 200:
                return self._json({"detail": "Add your work history to your profile first."},
                                  STATE["resume_code"])
            return self._json({"filename": "Alexandra_Resume.docx", "tailored": False,
                               "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               "base64": base64.b64encode(b"PK\x03\x04 synthetic").decode()})
        if "/api/fill-pack/" in self.path:
            return self._json(STATE["pack"])
        if self.path.startswith("/trigger"):
            body = b"<!doctype html><title>dashboard (harness)</title><h1>harness</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({}, 404)

    def do_POST(self):
        REQUESTS.append(("POST", self.path))
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if "recall-answers" in self.path:
            return self._json({"answers": STATE["recall"]})
        self._json({"ok": True})


# ── employer pages, served by the harness ────────────────────────────────────
PAGES = {
    "https://boards.greenhouse.io/acme/jobs/1": """
<form id="f" action="/acme/thanks" method="get">
  <label for="fn">First Name *</label><input id="fn" name="first_name" required>
  <label for="em">Email *</label><input id="em" type="email" name="email" required>
  <label for="q1">Why this team? *</label><input id="q1" name="why_team" required>
  <button id="go" type="submit">Submit Application</button>
</form>""",
    "https://boards.greenhouse.io/acme/jobs/2": """
<form id="f" action="/acme/thanks" method="get">
  <label for="fn">First Name *</label><input id="fn" name="first_name" required>
  <label for="em">Email *</label><input id="em" type="email" name="email" required>
  <label for="ph">Phone</label><input id="ph" name="phone">
  <button id="go" type="submit">Submit Application</button>
</form>""",
    "https://boards.greenhouse.io/acme/thanks": "<h1>Thank you for applying!</h1><p>We have received your application.</p>",
    "https://boards.greenhouse.io/login": """
<form id="login" action="/acme/after-login" method="get">
  <label for="u">Email</label><input id="u" type="email" name="user">
  <label for="pw">Password</label><input id="pw" type="password" name="pw" value="x">
  <label for="n">Name</label><input id="n" name="name" value="x">
  <button id="go" type="submit">Submit</button>
</form>""",
    "https://boards.greenhouse.io/acme/after-login": "<p>Signed in.</p>",
    "https://boards.greenhouse.io/other/jobs/9": """
<form><label for="em">Email *</label><input id="em" type="email" name="email">
<label for="fn">First name</label><input id="fn" name="first_name">
<label for="ln">Last name</label><input id="ln" name="last_name"></form>""",
    "https://careers.example-a.test/apply": """
<form><label for="fn">First name</label><input id="fn" name="first_name">
<label for="ln">Last name</label><input id="ln" name="last_name">
<label for="em">Email</label><input id="em" type="email" name="email">
<label for="g">Gender</label><select id="g" name="gender"><option value="">Select</option>
<option>Female</option><option>Male</option><option>Decline to self-identify</option></select>
<label for="c">Country</label><select id="c" name="country"><option value="">Select</option>
<option>United States</option><option>Canada</option><option>Mexico</option></select></form>""",
    "https://careers.example-b.test/apply": """
<form><label for="fn">First name</label><input id="fn" name="first_name">
<label for="em">Email</label><input id="em" type="email" name="email">
<div><label for="cl">Cover letter *</label><input id="cl" type="file" name="cover_letter" required></div></form>""",
    "https://careers.example-c.test/apply": """
<form><label for="fn">First name</label><input id="fn" name="first_name">
<label for="em">Email</label><input id="em" type="email" name="email">
<div><label for="rs">Resume (PDF only) *</label><input id="rs" type="file" name="resume" accept=".pdf" required></div></form>""",
    "https://careers.example-d.test/apply": """
<form><label for="fn">First name</label><input id="fn" name="first_name">
<label for="em">Email</label><input id="em" type="email" name="email">
<label for="lnk">Project link</label><input id="lnk" type="url" name="project_link" value="not a link">
<label for="n">Years in role</label><input id="n" type="number" name="role_years" min="5" value="2">
<label><input type="checkbox" name="agree" value="a" required checked> I agree to the terms *</label>
<label><input type="checkbox" name="agree" value="b" required> I confirm the above is true *</label>
<div><label for="rs">Resume *</label><input id="rs" type="file" name="resume" required></div></form>""",
    "https://jobs.lever.co/acme/123/apply": """
<form><input name="name" aria-label="Full name"><input name="email" type="email" aria-label="Email">
<label for="org">Current company</label><input id="org" name="org"></form>""",
}


def route_pages(route):
    url = route.request.url.split("?")[0]
    html = PAGES.get(url)
    if html is None:
        return route.fulfill(status=404, body="not found")
    return route.fulfill(status=200, content_type="text/html; charset=utf-8",
                         body=f"<!doctype html><html><body>{html}</body></html>")


def sw(ctx):
    return ctx.service_workers[0]


def sessions(ctx):
    return sw(ctx).evaluate("""() => new Promise(r => chrome.storage.local.get(
        ['spotapply_sessions'], d => r(d.spotapply_sessions || {})))""")


def launch(ctx, pack):
    """The dashboard's Auto-Fill & Apply: bridge -> new bound tab."""
    STATE["pack"] = pack
    trig = ctx.new_page()
    trig.goto(f"{BASE}/trigger")
    trig.wait_for_timeout(1200)
    with ctx.expect_page(timeout=15000) as opened:
        trig.evaluate("(p) => window.postMessage({ type: 'SPOTAPPLY_LOAD_PACK', pack: p }, '*')", pack)
    page = opened.value
    page.wait_for_load_state()
    page.wait_for_timeout(6500)
    trig.close()
    return page


def do_fill(ctx, page, pack):
    """Popup-style fill of an open tab; returns the content script's reply."""
    STATE["pack"] = pack
    return sw(ctx).evaluate("""(a) => new Promise((res) => chrome.tabs.query({}, (tabs) => {
        const t = tabs.find(t => t.url && t.url.startsWith(a.url));
        if (!t) return res({ error: 'no-tab' });
        chrome.tabs.sendMessage(t.id, { type: 'DO_FILL', fillPack: a.pack }, (r) => res(r || null));
    }))""", {"url": page.url.split("?")[0], "pack": pack})


def open_page(ctx, url):
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_timeout(1500)
    return page


def main():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_playwright() as p, tempfile.TemporaryDirectory() as prof:
        ctx = p.chromium.launch_persistent_context(
            prof, headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--disable-features=DisableLoadExtensionCommandLineSwitch"], **_LAUNCH_KW)
        ctx.route("https://**/*", route_pages)
        for _ in range(60):
            if ctx.service_workers:
                break
            ctx.pages[0].wait_for_timeout(250)
        if not ctx.service_workers:
            print("FAIL: no service worker")
            sys.exit(1)

        # ── 1. submission: attempt ≠ application ────────────────────────────
        print("\n1. Submission tracking")
        url1 = "https://boards.greenhouse.io/acme/jobs/1"
        page = launch(ctx, pack_for(url1))
        check("1a launched tab filled", page.eval_on_selector("#em", "e => e.value") == "alexandra.nguyen@example.com")
        REQUESTS.clear()
        page.click("#go")                 # q1 is empty and required: the browser refuses
        page.wait_for_timeout(3000)
        s = sessions(ctx)
        check("1b Submit on an incomplete form is NOT reported", not [r for r in REQUESTS if r[1].endswith("/submit")],
              str(REQUESTS))
        check("1c ...and the session survives, with no attempt recorded",
              len(s) == 1 and not list(s.values())[0].get("attempt"), json.dumps(s)[:160])
        # A login form on the same ATS host, in the same tab.
        page.goto("https://boards.greenhouse.io/login")
        page.wait_for_timeout(2500)
        page.click("#go")
        page.wait_for_timeout(2500)
        s = sessions(ctx)
        check("1d a login form's submit is not an application attempt",
              len(s) == 1 and not list(s.values())[0].get("attempt")
              and not [r for r in REQUESTS if r[1].endswith("/submit")], json.dumps(s)[:160])
        page.close()
        # A complete form whose submit lands on the employer's thank-you page.
        url2 = "https://boards.greenhouse.io/acme/jobs/2"
        page = launch(ctx, pack_for(url2, app_id=4343))
        REQUESTS.clear()
        page.click("#go")
        page.wait_for_timeout(6000)
        confirmed = [r for r in REQUESTS if r[1] == "/application/4343/submit"]
        check("1e the employer's confirmation page marks it Submitted", bool(confirmed), str(REQUESTS))
        check("1f ...and ends that tab's session",
              not any(v["pack"].get("app_id") == 4343 for v in sessions(ctx).values()))
        page.close()

        # ── 2. sessions are per tab ─────────────────────────────────────────
        print("\n2. Session isolation")
        page_a = launch(ctx, pack_for(url1, app_id=5001))
        other = ctx.new_page()             # opened by the user, not by SpotApply
        other.goto("https://boards.greenhouse.io/other/jobs/9")
        other.wait_for_timeout(9000)
        check("2a job A's details are NOT typed into an unrelated job's tab",
              other.eval_on_selector("#em", "e => e.value") == "", other.eval_on_selector("#em", "e => e.value"))
        other.close()
        page_a.close()

        # ── 5 + 8. demographic consent · residence country · manual edits ────
        print("\n5/8. Consent, country, manual corrections")
        STATE["recall"] = {"gender": "Female", "Gender": "Female"}
        pa = open_page(ctx, "https://careers.example-a.test/apply")
        do_fill(ctx, pa, pack_for("https://careers.example-a.test/apply"))
        pa.wait_for_timeout(4000)
        check("5a a saved Gender answer is NOT written while EEO autofill is off",
              pa.eval_on_selector("#g", "e => e.value") == "", pa.eval_on_selector("#g", "e => e.value"))
        check("8a a Toronto profile's country is Canada, not the United States",
              pa.eval_on_selector("#c", "e => e.value") == "Canada", pa.eval_on_selector("#c", "e => e.value"))
        pa.focus("#c")
        pa.keyboard.press("ArrowDown")    # a real keyboard change = the user's own choice
        pa.wait_for_timeout(300)
        chosen = pa.eval_on_selector("#c", "e => e.value")
        do_fill(ctx, pa, pack_for("https://careers.example-a.test/apply"))
        pa.wait_for_timeout(4000)
        check("8b a country the user picked is not reset by the next fill",
              pa.eval_on_selector("#c", "e => e.value") == chosen and chosen != "Canada", chosen)
        STATE["recall"] = {}
        no_country = pack_for("https://careers.example-a.test/apply", residence_country="")
        pa2 = open_page(ctx, "https://careers.example-a.test/apply")
        do_fill(ctx, pa2, no_country)
        pa2.wait_for_timeout(3500)
        check("8c an unknown residence leaves the country for the user",
              pa2.eval_on_selector("#c", "e => e.value") == "", pa2.eval_on_selector("#c", "e => e.value"))
        pa.close(); pa2.close()

        # ── 6. résumé targeting ─────────────────────────────────────────────
        print("\n6. Résumé upload targeting")
        pb = open_page(ctx, "https://careers.example-b.test/apply")
        do_fill(ctx, pb, pack_for("https://careers.example-b.test/apply"))
        pb.wait_for_timeout(5000)
        check("6a the résumé is never put into a Cover letter field",
              pb.eval_on_selector("#cl", "e => e.files.length") == 0)
        pc = open_page(ctx, "https://careers.example-c.test/apply")
        reply = do_fill(ctx, pc, pack_for("https://careers.example-c.test/apply"))
        pc.wait_for_timeout(5000)
        check("6b a PDF-only field is not given a DOCX", pc.eval_on_selector("#rs", "e => e.files.length") == 0)
        overlay = pc.evaluate("""() => { const h = document.getElementById('hp-copilot-overlay');
            const b = h && h.shadowRoot && h.shadowRoot.querySelector('.body');
            return b ? b.textContent : ''; }""")
        check("6c ...and the user is told why", "accepts only" in overlay, overlay[:160])
        pb.close(); pc.close()

        # ── 7. honest completion status ─────────────────────────────────────
        print("\n7. Completion status")
        pd = open_page(ctx, "https://careers.example-d.test/apply")
        reply = do_fill(ctx, pd, pack_for("https://careers.example-d.test/apply")) or {}
        check("7a the reply is a result, not a bare ok", "state" in reply, json.dumps(reply)[:200])
        check("7b an unconfirmed upload, an invalid link and a number under its minimum are problems",
              reply.get("failed", 0) >= 3, json.dumps(reply)[:300])
        check("7c two required checkboxes sharing a name need BOTH ticked",
              reply.get("needUser", 0) >= 1 and reply.get("state") == "needs_review", json.dumps(reply)[:200])
        empty = do_fill(ctx, pd, pack_for("https://careers.example-d.test/apply",
                                          first_name="", last_name="", email=""))
        check("7d an empty profile is reported as nothing filled, not ok",
              isinstance(empty, dict) and empty.get("ok") is False, json.dumps(empty)[:160])
        pd.close()
        STATE["resume_code"] = 422
        pe = open_page(ctx, "https://careers.example-c.test/apply")
        do_fill(ctx, pe, pack_for("https://careers.example-c.test/apply"))
        pe.wait_for_timeout(9000)         # past the final recount
        overlay = pe.evaluate("""() => { const h = document.getElementById('hp-copilot-overlay');
            const b = h && h.shadowRoot && h.shadowRoot.querySelector('.body');
            return b ? b.textContent : ''; }""")
        check("7e the server's reason for no résumé survives the final recount",
              "work history" in overlay, overlay[:200])
        STATE["resume_code"] = 200
        pe.close()

        # ── 10. Lever current company ───────────────────────────────────────
        print("\n10. Lever")
        pl = open_page(ctx, "https://jobs.lever.co/acme/123/apply")
        do_fill(ctx, pl, pack_for("https://jobs.lever.co/acme/123/apply"))
        pl.wait_for_timeout(4000)
        check("10a Lever's current-company field gets the employer, not the job title",
              pl.eval_on_selector("#org", "e => e.value") == "Example Employer",
              pl.eval_on_selector("#org", "e => e.value"))
        pl.close()
        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
