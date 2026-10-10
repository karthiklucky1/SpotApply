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
# pdf: the stub has a one-page PDF to offer (the server's tailored PDF), sent
# when the field's ?accept= allows it, exactly as /api/fill-pack/{id}/resume.
STATE = {"pack": None, "recall": {}, "resume_code": 200, "pdf": False,
         "asked": [], "resume_accept": []}
# What a model says when handed a field NAME instead of a question: the reply
# the live test (2026-10-09) found typed into Ashby's reCAPTCHA textarea.
META_REPLY = ("I'd be happy to help, but I notice the essay question appears incomplete or "
              "unclear. \"g-recaptcha-response\" looks like a technical parameter.")


def _accepts_pdf(accept):
    toks = [t.strip().lower() for t in (accept or "").split(",") if t.strip()]
    return not toks or any(t in (".pdf", "application/pdf", "application/*", "*/*", "*") for t in toks)
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
        if self.path.split("?")[0].endswith("/resume"):
            if STATE["resume_code"] != 200:
                return self._json({"detail": "Add your work history to your profile first."},
                                  STATE["resume_code"])
            from urllib.parse import parse_qs, urlparse
            accept = (parse_qs(urlparse(self.path).query).get("accept") or [""])[0]
            STATE["resume_accept"].append(accept)
            if STATE["pdf"] and _accepts_pdf(accept):
                return self._json({"filename": "Alexandra_Nguyen_Resume.pdf", "mime": "application/pdf",
                                   "base64": base64.b64encode(b"%PDF-1.4 synthetic").decode()})
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
        raw = self.rfile.read(n) if n else b""
        if "recall-answers" in self.path:
            return self._json({"answers": STATE["recall"]})
        if "answer-question" in self.path:
            try:
                q = json.loads(raw or b"{}").get("question", "")
            except ValueError:
                q = ""
            STATE["asked"].append(q)
            # A real question gets an answer; anything else gets the meta reply
            # an older server cached for the captcha field.
            if "interested in this role" in q.lower():
                return self._json({"answer": "I build integrations that customers rely on.",
                                   "cached": False})
            return self._json({"answer": META_REPLY, "cached": True})
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
    # A multi-step form: the widget may say "click Next" here, and only here.
    "https://careers.example-e.test/step1": """
<form><label for="fn">First name *</label><input id="fn" name="first_name" required>
<label for="em">Email *</label><input id="em" type="email" name="email" required>
<label for="fav">Favorite color *</label><input id="fav" name="favorite_color" required>
<button type="button" id="next">Next</button></form>""",
}

# ── Ashby, as the live test met it (2026-10-09) ─────────────────────────────
# A single-page form: required marked by a label CLASS whose "*" is CSS
# ::after content (never in the text), Yes/No questions as two buttons, a
# Location autocomplete that keeps only a PICKED suggestion (typed text is
# cleared on blur), and the hidden captcha/honeypot fields a form carries.
ASHBY_FORM = """
<style>
  label._required_x1::after { content: "*"; color: #c00; }
  ._yesno_x1 button._active_x1 { background: #222; color: #fff; }
  #loc-list { list-style: none; margin: 0; padding: 0; }
</style>
<form id="ashby-app" onsubmit="return false">
  <div class="ashby-application-form-field-entry">
    <label class="_required_x1 ashby-application-form-question-title" for="_systemfield_name">Name</label>
    <input id="_systemfield_name" name="_systemfield_name" type="text"></div>
  <div class="ashby-application-form-field-entry">
    <label class="_required_x1 ashby-application-form-question-title" for="_systemfield_email">Email</label>
    <input id="_systemfield_email" name="_systemfield_email" type="email"></div>
  <div class="ashby-application-form-field-entry">
    <label class="_required_x1 ashby-application-form-question-title" for="loc">Location</label>
    <input id="loc" type="text" role="combobox" aria-autocomplete="list" aria-expanded="false"
           aria-controls="loc-list" placeholder="Start typing..." autocomplete="off">
    <ul id="loc-list" role="listbox"></ul></div>
  <div class="ashby-application-form-field-entry" id="q-auth">
    <label class="_required_x1 ashby-application-form-question-title">Are you legally authorized to work in the United States?</label>
    <div class="_yesno_x1"><button type="button" class="_option_x1">Yes</button><button type="button" class="_option_x1">No</button></div></div>
  <div class="ashby-application-form-field-entry" id="q-sponsor">
    <label class="_required_x1 ashby-application-form-question-title">Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?</label>
    <div class="_yesno_x1"><button type="button" class="_option_x1">Yes</button><button type="button" class="_option_x1">No</button></div></div>
  <div class="ashby-application-form-field-entry">
    <label class="_required_x1 ashby-application-form-question-title" for="hear">How did you hear about this opportunity?</label>
    <input id="hear" name="hear_source" type="text"></div>
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title" for="why">Why are you interested in this role at Acme?</label>
    <textarea id="why" name="why_acme"></textarea></div>
  <div class="ashby-application-form-field-entry">
    <label class="_required_x1 ashby-application-form-question-title" for="_systemfield_resume">Resume</label>
    <input id="_systemfield_resume" name="_systemfield_resume" type="file" accept=".pdf,.doc,.docx">
    <span id="resume-chip"></span></div>
  <textarea id="g-recaptcha-response" name="g-recaptcha-response" class="g-recaptcha-response" style="display:none"></textarea>
  <textarea name="h-captcha-response" style="display:none"></textarea>
  <div style="position:absolute;left:-9999px;top:0"><label for="hp1">Leave this field blank</label>
    <textarea id="hp1" name="comments_extra"></textarea></div>
  <button type="submit" id="submit-app">Submit Application</button>
</form>"""

ASHBY_INIT = """
window.__initAshbyForm = function () {
  document.querySelectorAll('._yesno_x1').forEach(function (box) {
    box.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        box.querySelectorAll('button').forEach(function (o) { o.classList.remove('_active_x1'); });
        b.classList.add('_active_x1');
      });
    });
  });
  var loc = document.getElementById('loc'), list = document.getElementById('loc-list');
  var committed = false, timer = null;
  var PLACES = ['Cincinnati, Iowa, United States', 'Cincinnati, Ohio, United States',
                'Columbus, Ohio, United States'];
  loc.addEventListener('input', function () {
    committed = false; clearTimeout(timer);
    var q = loc.value.toLowerCase();
    timer = setTimeout(function () {
      list.innerHTML = '';
      if (q.length < 3) return;
      PLACES.filter(function (p) { return p.toLowerCase().indexOf(q) === 0; }).forEach(function (p) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option'); li.textContent = p;
        li.addEventListener('mousedown', function (e) {
          e.preventDefault(); loc.value = p; committed = true; list.innerHTML = '';
        });
        list.appendChild(li);
      });
    }, 250);
  });
  loc.addEventListener('blur', function () { if (!committed) loc.value = ''; });
  document.getElementById('_systemfield_resume').addEventListener('change', function (e) {
    var f = e.target.files[0];
    document.getElementById('resume-chip').textContent = f ? f.name : '';
  });
};
"""

# The job page itself. Its description says "Design integration…", whose
# letters "deSIGN INtegration" were read as "sign in" (a false login wall),
# and its path contains "professor", whose "sso" matched the SSO-gateway rule.
PAGES["https://jobs.ashbyhq.com/acme/assistant-professor-integrations"] = f"""
<div id="app">
  <h1>Software Engineer, Integrations Delivery &amp; Support</h1>
  <p>Design integration schemas and data mappings that translate findings into
     the structures customer platforms expect.</p>
  <p>Build connectors to SIEM, SOAR and ITSM tools.</p>
  <button id="apply-btn" type="button">Apply for this Job</button>
</div>
<template id="form-tpl">{ASHBY_FORM}</template>
<script>{ASHBY_INIT}
document.getElementById('apply-btn').addEventListener('click', function () {{
  history.pushState({{}}, '', location.pathname.replace(/\\/$/, '') + '/application');
  document.getElementById('app').innerHTML = document.getElementById('form-tpl').innerHTML;
  window.__initAshbyForm();
}});
</script>"""
PAGES["https://jobs.ashbyhq.com/acme/assistant-professor-integrations/application"] = (
    ASHBY_FORM + f"<script>{ASHBY_INIT}window.__initAshbyForm();</script>")
PAGES["https://jobs.ashbyhq.com/acme/job-2/application"] = (
    ASHBY_FORM + f"<script>{ASHBY_INIT}window.__initAshbyForm();</script>")


# The employer hosts above are served by a local HTTPS server that Chrome is
# pointed at with --host-resolver-rules. Playwright's ctx.route could not do it:
# on Chrome 153 (CI) it attaches to a tab the EXTENSION opens after that tab's
# first request has gone out, so the real boards.greenhouse.io loaded instead.
HTTPS_PORT = PORT + 1
_PAGE_HOSTS = sorted({u.split("/")[2] for u in PAGES})


class EmployerPages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        html = PAGES.get(f"https://{host}{self.path.split('?')[0]}")
        body = ("not found" if html is None else
                f"<!doctype html><html><body>{html}</body></html>").encode()
        self.send_response(404 if html is None else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_employer_pages(tmpdir):
    import ssl
    import subprocess
    key, cert = os.path.join(tmpdir, "k.pem"), os.path.join(tmpdir, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-keyout", key, "-out", cert, "-subj", "/CN=spotapply-test"],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HTTPS_PORT), EmployerPages)
    srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return ["--host-resolver-rules=" + ", ".join(
                f"MAP {h} 127.0.0.1:{HTTPS_PORT}" for h in _PAGE_HOSTS),
            "--ignore-certificate-errors", "--no-proxy-server"]


def sw(ctx):
    return ctx.service_workers[0]


def sessions(ctx):
    return sw(ctx).evaluate("""() => new Promise(r => chrome.storage.local.get(
        ['spotapply_sessions'], d => r(d.spotapply_sessions || {})))""")


BRIDGE_READY_JS = """() => new Promise((resolve) => {
        const end = Date.now() + 15000;
        const onMsg = (e) => {
            if (e.data && /_EXT_PING_OK$/.test(e.data.type || '')) {
                window.removeEventListener('message', onMsg); clearInterval(t); resolve(true);
            }
        };
        window.addEventListener('message', onMsg);
        const t = setInterval(() => {
            if (Date.now() > end) { clearInterval(t); resolve(false); return; }
            window.postMessage({ type: 'SPOTAPPLY_EXT_PING' }, '*');
        }, 300);
    })"""


def launch(ctx, pack):
    """The dashboard's Auto-Fill & Apply: bridge -> new bound tab."""
    STATE["pack"] = pack
    trig = ctx.new_page()
    trig.goto(f"{BASE}/trigger")
    # Wait until the content script answers the dashboard bridge: on a slow CI
    # runner a fixed 1.2 s was sometimes too early and the pack went nowhere.
    trig.evaluate(BRIDGE_READY_JS)
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


OVERLAY_JS = """() => { const h = document.getElementById('hp-copilot-overlay');
    const b = h && h.shadowRoot && h.shadowRoot.querySelector('.body');
    return b ? b.textContent : ''; }"""

# Records every overlay text the page shows (it is replaced as the fill runs).
WATCH_OVERLAY_JS = """() => { window.__overlays = window.__overlays || [];
    setInterval(() => { const h = document.getElementById('hp-copilot-overlay');
      const b = h && h.shadowRoot && h.shadowRoot.querySelector('.body');
      const t = b ? b.textContent : '';
      if (t && window.__overlays[window.__overlays.length - 1] !== t) window.__overlays.push(t);
    }, 100); }"""


def wait_until(page, js, timeout_ms=15000):
    """Poll a page predicate; True once it holds."""
    end = timeout_ms
    while end > 0:
        try:
            if page.evaluate(js):
                return True
        except Exception:
            pass
        page.wait_for_timeout(250)
        end -= 250
    return False


def owner_like_pack(url, **over):
    """The live tester's shape: F-1 OPT with no end date (authorized_now None),
    needs sponsorship, lives in Cincinnati, OH."""
    fields = dict(location="Cincinnati, OH", residence_country="United States",
                  work_authorization="F-1 OPT", work_auth_status="OPT",
                  requires_sponsorship=True, authorized_now=None,
                  work_auth_note=("Add your work authorization end date in your SpotApply "
                                  "profile so this question can be answered for you."))
    fields.update(over)
    return pack_for(url, **fields)


def ashby_live_test(ctx):
    """Every finding of the 2026-10-09 Ashby live test, on one page."""
    print("\n11. Ashby live test (2026-10-09)")
    STATE["pdf"] = True
    STATE["asked"].clear()
    STATE["resume_accept"].clear()
    jd = "https://jobs.ashbyhq.com/acme/assistant-professor-integrations"
    STATE["pack"] = owner_like_pack(jd, app_id=6060)
    trig = ctx.new_page()
    trig.goto(f"{BASE}/trigger")
    trig.evaluate(BRIDGE_READY_JS)
    console = []
    with ctx.expect_page(timeout=15000) as opened:
        trig.evaluate("(p) => window.postMessage({ type: 'SPOTAPPLY_LOAD_PACK', pack: p }, '*')",
                      STATE["pack"])
    page = opened.value
    page.on("console", lambda m: console.append(m.text))
    page.wait_for_load_state()
    page.evaluate(WATCH_OVERLAY_JS)
    filled = wait_until(page, "() => !!document.getElementById('_systemfield_email') && "
                              "document.getElementById('_systemfield_email').value !== ''", 20000)
    # Let the essay, Yes/No, location and resume steps finish, and the final recount.
    wait_until(page, "() => (document.getElementById('resume-chip') || {}).textContent", 15000)
    page.wait_for_timeout(4000)
    trig.close()
    seen = page.evaluate("() => window.__overlays || []")
    login = [t for t in seen if "log in" in t.lower()] + \
            [c for c in console if "Login wall detected" in c]
    check("11a a job page whose text says 'Design integration' is not a login wall",
          not login, str(login)[:200])
    check("11b ...the extension clicked Apply and filled the single-page form", filled,
          page.url)

    vals = page.evaluate("""() => ({
        recaptcha: document.getElementById('g-recaptcha-response').value,
        hcaptcha: document.querySelector('[name="h-captcha-response"]').value,
        honeypot: document.getElementById('hp1').value,
        why: document.getElementById('why').value,
        loc: document.getElementById('loc').value,
        hear: document.getElementById('hear').value,
        authYes: document.querySelector('#q-auth button').classList.contains('_active_x1'),
        authNo: document.querySelectorAll('#q-auth button')[1].classList.contains('_active_x1'),
        authFlag: document.querySelector('#q-auth ._yesno_x1').dataset.spotapply || '',
        sponYes: document.querySelector('#q-sponsor button').classList.contains('_active_x1'),
        sponNo: document.querySelectorAll('#q-sponsor button')[1].classList.contains('_active_x1'),
        locFlag: document.getElementById('loc').dataset.spotapply || '',
        hearFlag: document.getElementById('hear').dataset.spotapply || '',
        resume: (document.getElementById('_systemfield_resume').files[0] || {}).name || '',
    })""")
    asked = [q.lower() for q in STATE["asked"]]
    check("11c the hidden reCAPTCHA textarea is never filled", vals["recaptcha"] == "", vals["recaptcha"][:80])
    check("11d ...nor the hCaptcha response, nor an off-page honeypot",
          vals["hcaptcha"] == "" and vals["honeypot"] == "", json.dumps(vals)[:160])
    check("11e ...and no AI answer is ever asked for them",
          not [q for q in asked if "captcha" in q or "leave this field" in q], str(asked))
    check("11f a real essay question still gets its answer",
          vals["why"].startswith("I build integrations"), vals["why"][:60])
    check("11g Location autocomplete: the profile's city is PICKED from the suggestions (Ohio, not Iowa)",
          vals["loc"] == "Cincinnati, Ohio, United States", vals["loc"])
    check("11h sponsorship Yes/No button answered Yes (requires_sponsorship)",
          vals["sponYes"] and not vals["sponNo"], json.dumps(vals))
    check("11i authorization left for the user while authorized_now is unknown",
          not vals["authYes"] and not vals["authNo"], json.dumps(vals))
    check("11j ...and that unanswered required Yes/No is flagged red (counted)",
          vals["authFlag"] == "needs-fill-required", vals["authFlag"])
    check("11k a CSS-asterisk required text field is flagged required",
          vals["hearFlag"] == "needs-fill-required", vals["hearFlag"])
    check("11l how-did-you-hear is left for the user (no saved answer)", vals["hear"] == "", vals["hear"])
    check("11m the resume request names what the field accepts",
          ".pdf" in "".join(STATE["resume_accept"]), str(STATE["resume_accept"]))
    check("11n the one-page PDF is attached where the field takes PDF",
          vals["resume"].endswith(".pdf"), vals["resume"])
    overlay = page.evaluate(OVERLAY_JS)
    check("11o single-page form: no 'click Next', the user reviews and submits",
          "click Next" not in overlay and "submit it yourself" in overlay, overlay[:220])
    check("11p the reason the authorization question is left is shown",
          "end date" in overlay, overlay[:300])
    reply = do_fill(ctx, page, owner_like_pack(jd, app_id=6060)) or {}
    check("11q every required blank is counted (authorization Yes/No + how did you hear)",
          reply.get("needUser", 0) >= 2 and reply.get("state") == "needs_review", json.dumps(reply)[:200])
    page.close()

    # A profile whose authorization IS settled: the authorization Yes is pressed,
    # and a dated status's sponsorship question stays the applicant's.
    # Opened directly on the form (no job page, no Apply click), so these
    # checks do not depend on the login-wall fix.
    STATE["asked"].clear()
    url2 = "https://jobs.ashbyhq.com/acme/job-2/application"
    p2 = open_page(ctx, url2)
    reply2 = do_fill(ctx, p2, owner_like_pack(url2, app_id=6161, authorized_now=True,
                                              requires_sponsorship=None, work_auth_note="")) or {}
    wait_until(p2, "() => document.querySelector('#q-auth button._active_x1') !== null", 8000)
    p2.wait_for_timeout(1500)
    v2 = p2.evaluate("""() => ({
        auth: (document.querySelector('#q-auth button._active_x1') || {}).textContent || '',
        spon: (document.querySelector('#q-sponsor button._active_x1') || {}).textContent || '',
        sponFlag: document.querySelector('#q-sponsor ._yesno_x1').dataset.spotapply || '',
        recaptcha: document.getElementById('g-recaptcha-response').value,
        honeypot: document.getElementById('hp1').value,
        loc: document.getElementById('loc').value,
        resume: (document.getElementById('_systemfield_resume').files[0] || {}).name || '',
        chip: document.getElementById('resume-chip').textContent })""")
    asked2 = [q.lower() for q in STATE["asked"]]
    check("11r authorized_now=true answers the authorization Yes button", v2["auth"] == "Yes", json.dumps(v2))
    check("11s ...and an undecided sponsorship stays for the user, flagged as required",
          v2["spon"] == "" and v2["sponFlag"] == "needs-fill-required", json.dumps(v2))
    check("11u on the form itself: the reCAPTCHA textarea and the honeypot stay empty",
          v2["recaptcha"] == "" and v2["honeypot"] == "", json.dumps(v2)[:200])
    check("11v ...and the AI is never asked about them",
          not [q for q in asked2 if "captcha" in q or "leave this field" in q], str(asked2))
    check("11w ...the location is picked from the suggestions",
          v2["loc"] == "Cincinnati, Ohio, United States", v2["loc"])
    check("11x ...the reply counts the unanswered sponsorship Yes/No and how-did-you-hear",
          reply2.get("needUser", 0) >= 2, json.dumps(reply2)[:200])
    # The server now answers with the PDF; a build that sends no ?accept=
    # (the Store's 1.0.0) must attach it unchanged. Known tradeoff (documented
    # on get_tailored_resume): that old build cannot say what the field takes,
    # so on a field whose accept EXCLUDES PDF (e.g. ".doc,.docx") it refuses
    # the PDF and attaches nothing, where it used to attach the .docx. This
    # check covers only a field that takes PDF (".pdf,.doc,.docx"). 1.0.1+
    # always sends ?accept= ("*" when the field names none).
    check("11y the server's PDF is attached as-is (filename and upload chip)",
          v2["resume"].endswith(".pdf") and v2["chip"].endswith(".pdf"), json.dumps(v2)[-120:])
    p2.close()

    # A multi-step form keeps "click Next".
    p3 = open_page(ctx, "https://careers.example-e.test/step1")
    do_fill(ctx, p3, pack_for("https://careers.example-e.test/step1"))
    p3.wait_for_timeout(2500)
    ov3 = p3.evaluate(OVERLAY_JS)
    check("11t a form with a Next button still says 'click Next'", "click Next" in ov3, ov3[:160])
    p3.close()
    STATE["pdf"] = False


def main():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_playwright() as p, tempfile.TemporaryDirectory() as prof, \
            tempfile.TemporaryDirectory() as certs:
        pages_args = start_employer_pages(certs)
        ctx = p.chromium.launch_persistent_context(
            prof, headless=True, ignore_https_errors=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--disable-features=DisableLoadExtensionCommandLineSwitch", *pages_args],
            **_LAUNCH_KW)
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

        ashby_live_test(ctx)
        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
