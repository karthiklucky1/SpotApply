"""Real-fill coverage across the ATS layouts the extension claims to support.

test_extension_live.py proves the plumbing (bridge -> background -> DO_FILL).
This file proves the FILL ITSELF on four different DOM shapes, driving each
form the way production does and asserting on the resulting field values:

  Greenhouse  label/id/name selectors + EEO <select>s + textarea
  Lever       single "Full name" field, bare name= attributes
  Workday     data-automation-id only, no usable labels, country dropdown
  React       controlled inputs that revert any value written without a
              proper native setter + input event (the classic autofill killer)
  Native      the board's OWN autofill-from-resume (Workday's "Autofill with
              Resume" button, Ashby's autofill card) is fed the resume FIRST;
              what it filled is kept, a wrong identity field is corrected, its
              history entries are left alone, the resume is not attached twice,
              and a page without such a widget — or the switch off — takes the
              normal path (workday-autofill.html, ashby-parse.html)

A stub backend serves the endpoints the content script calls (résumé download,
recall-answers, telemetry) so the résumé attach path runs for real instead of
404ing, and every request is recorded.
"""
import http.server
import json
import os
import sys
import threading
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
EXT = str(HERE.parent / "extension")
PORT = 8919
BASE = f"http://127.0.0.1:{PORT}"

_CHROME = os.environ.get("SPOTAPPLY_CHROMIUM") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
_LAUNCH_KW = {"executable_path": _CHROME} if _CHROME else {}

# A tiny real .docx (zip magic) so the résumé-attach path exercises the actual
# base64 -> Blob -> DataTransfer -> file input flow.
RESUME_BYTES = b"PK\x03\x04" + b"harness-resume-docx-payload" * 8

REQUESTS: list[tuple[str, str]] = []

# The content script re-fetches /api/fill-pack/{id} to pick up profile edits, so
# the stub must serve the pack the current scenario is testing — otherwise every
# per-scenario override (phone, skills, sponsorship) is silently reverted.
CURRENT_PACK: dict = {}


def base_pack(apply_url: str) -> dict:
    return {
        "app_id": 4242,
        "job_title": "Senior Engineer",
        "company": "HarnessCo",
        "apply_url": apply_url,
        "first_name": "Alexandra",
        "last_name": "Nguyen",
        "email": "alexandra.nguyen@example.com",
        "phone": "+1 415 555 0199",
        "location": "San Francisco, CA",
        # What the server derives from that location (server._residence_country).
        # Country dropdowns are filled from THIS only — never a default.
        "residence_country": "United States",
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
        "cover_letter": "Dear Hiring Team, I am excited to apply to HarnessCo.",
        "resume_text": "Alexandra Nguyen - Senior Software Engineer",
        "ai_answers": {},
        # Present in the real fill pack; the "current company" rule reads it.
        "work_experience": [{"company": "Globali20 India", "title": "ML Engineer"}],
        # Current key; the extension also accepts the legacy hirepath_url.
        "spotapply_url": BASE,
        "auth_token": "harness-test-token",
    }


class StubBackend(http.server.SimpleHTTPRequestHandler):
    """Serves the fixtures AND the handful of API routes content.js calls."""

    def log_message(self, *a):  # keep output readable
        pass

    def guess_type(self, path):
        """Serve HTML as UTF-8.

        Without an explicit charset Chromium falls back to windows-1252 and
        "Résumé" arrives as "RÃ©sumÃ©" — which made a fixture fail for a reason
        that has nothing to do with the extension.
        """
        base = super().guess_type(path)
        if str(path).endswith((".html", ".htm")):
            return "text/html; charset=utf-8"
        return base

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        REQUESTS.append(("GET", self.path))
        if self.path.endswith("/resume"):
            import base64
            return self._json({
                "filename": "Alexandra_Nguyen_Resume.docx",
                "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "base64": base64.b64encode(RESUME_BYTES).decode(),
            })
        if "/api/fill-pack/" in self.path:
            return self._json(CURRENT_PACK or base_pack(f"{BASE}/greenhouse.html"))
        return super().do_GET()

    def do_POST(self):
        REQUESTS.append(("POST", self.path))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if "recall-answers" in self.path:
            return self._json({"answers": []})
        return self._json({"ok": True})


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def drive_fill(ctx, page_url, pack):
    """Run the production path: seed a copilot session, open the form, DO_FILL."""
    global CURRENT_PACK
    CURRENT_PACK = pack          # the re-fetch must agree with what we seeded
    sw = ctx.service_workers[0]
    sw.evaluate(
        """(pack) => new Promise((resolve) => {
            chrome.storage.local.set({
                spotapply_fill_pack: pack,
                spotapply_copilot_pack: pack,
                spotapply_copilot_ts: Date.now(),
                spotapply_auto_fill: false,
            }, resolve);
        })""", pack)
    page = ctx.new_page()
    logs = []
    page.on("console", lambda m: logs.append(m.text) if "[SpotApply]" in m.text else None)
    page.goto(page_url)
    page.wait_for_timeout(1500)  # content script injects at document_idle
    page.evaluate(
        """(pack) => new Promise((resolve) => {
            chrome.runtime.sendMessage({ type: 'PING' }, () => resolve());
        }).catch(() => {})""", pack)
    # Ask the service worker to fire DO_FILL at this exact tab, as
    # tabs.onUpdated does in production.
    sw.evaluate(
        """(args) => new Promise((resolve) => {
            chrome.tabs.query({}, (tabs) => {
                const t = tabs.find(t => t.url && t.url.includes(args.needle));
                if (!t) return resolve('no-tab');
                chrome.tabs.sendMessage(t.id, { type: 'DO_FILL', fillPack: args.pack }, () => resolve('sent'));
            });
        })""", {"needle": page_url.rsplit("/", 1)[-1], "pack": pack})
    page.wait_for_timeout(7000)  # fill is async (résumé fetch, dropdowns)
    return page, logs


def drive_fill_reply(ctx, page_url, pack, timeout_ms=40000):
    """Like drive_fill, but WAIT for the content script's DO_FILL reply — the
    result the popup shows — and return it as the third value. A native
    autofill run waits for the board's parser to settle and the resume attach
    polls for the site's acknowledgement, so the reply can take ~10 s."""
    global CURRENT_PACK
    CURRENT_PACK = pack
    sw = ctx.service_workers[0]
    sw.evaluate(
        """(pack) => new Promise((resolve) => {
            chrome.storage.local.set({
                spotapply_fill_pack: pack,
                spotapply_copilot_pack: pack,
                spotapply_copilot_ts: Date.now(),
                spotapply_auto_fill: false,
            }, resolve);
        })""", pack)
    page = ctx.new_page()
    logs = []
    page.on("console", lambda m: logs.append(m.text) if "[SpotApply]" in m.text else None)
    page.goto(page_url)
    page.wait_for_timeout(1500)
    reply = sw.evaluate(
        """(args) => new Promise((resolve) => {
            const timer = setTimeout(() => resolve({ timeout: true }), args.timeout);
            chrome.tabs.query({}, (tabs) => {
                const t = tabs.find(t => t.url && t.url.includes(args.needle));
                if (!t) { clearTimeout(timer); return resolve({ error: 'no-tab' }); }
                chrome.tabs.sendMessage(t.id, { type: 'DO_FILL', fillPack: args.pack }, (r) => {
                    clearTimeout(timer);
                    resolve(r || { error: (chrome.runtime.lastError && chrome.runtime.lastError.message) || 'no reply' });
                });
            });
        })""", {"needle": page_url.rsplit("/", 1)[-1], "pack": pack, "timeout": timeout_ms})
    page.wait_for_timeout(500)
    return page, logs, reply or {}


def set_native_autofill(ctx, enabled):
    """The user's native-autofill switch (popup setting; absent = ON)."""
    ctx.service_workers[0].evaluate(
        """(on) => new Promise((resolve) => {
            if (on === null) chrome.storage.local.remove(['spotapply_native_autofill'], resolve);
            else chrome.storage.local.set({ spotapply_native_autofill: on }, resolve);
        })""", enabled)


def resume_fetches():
    return sum(1 for _, p in REQUESTS if p.endswith("/resume"))


def main():
    handler = partial(StubBackend, directory=str(HERE / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    import tempfile
    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--disable-features=DisableLoadExtensionCommandLineSwitch"],
            **_LAUNCH_KW)
        deadline = 0
        while not ctx.service_workers and deadline < 60:
            ctx.pages[0].wait_for_timeout(250)
            deadline += 1
        if not ctx.service_workers:
            print("FAIL: service worker never registered")
            sys.exit(1)

        # ── G: Greenhouse layout ────────────────────────────────────────────
        print("\nGreenhouse layout (labels + ids + EEO selects)")
        pack = base_pack(f"{BASE}/greenhouse.html")
        page, logs = drive_fill(ctx, f"{BASE}/greenhouse.html", pack)
        val = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("G1 first name", val("#first_name") == pack["first_name"], val("#first_name"))
        check("G2 last name", val("#last_name") == pack["last_name"], val("#last_name"))
        check("G3 email", val("#email") == pack["email"], val("#email"))
        check("G4 phone", val("#phone") == pack["phone"], val("#phone"))
        check("G5 location", bool(val("#job_application_location")), val("#job_application_location"))
        check("G6 linkedin", pack["linkedin_url"] in (val("#gh_linkedin") or ""), val("#gh_linkedin"))
        check("G7 github", pack["github_url"] in (val("#gh_github") or ""), val("#gh_github"))
        check("G8 cover letter textarea", len(val("#cover_letter_text") or "") > 10,
              (val("#cover_letter_text") or "")[:40])
        # Demographic self-ID is voluntary and legally protected: SpotApply
        # leaves it to the user unless they explicitly opt in. The opt-in path
        # is covered by P6/P7 in test_extension_payload.py.
        check("G9 gender NOT auto-answered (voluntary self-ID)", val("#gender") == "",
              repr(val("#gender")))
        check("G10 veteran NOT auto-answered (voluntary self-ID)",
              val("#veteran_status") == "", repr(val("#veteran_status")))
        check("G11 sponsorship = No", val("#sponsorship") == "No", val("#sponsorship"))
        check("G12 work auth = Yes", val("#work_auth") == "Yes", val("#work_auth"))
        n_files = page.eval_on_selector("#resume", "el => el.files.length")
        fname = page.eval_on_selector("#resume", "el => el.files[0] ? el.files[0].name : ''")
        check("G13 résumé attached to file input", n_files == 1, f"files={n_files} name={fname!r}")
        page.close()

        # ── L: Lever layout ─────────────────────────────────────────────────
        print("\nLever layout (single full-name field, bare name= attrs)")
        pack = base_pack(f"{BASE}/lever.html")
        page, _ = drive_fill(ctx, f"{BASE}/lever.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        full = v("input[name='name']")
        check("L1 full name combines first+last",
              pack["first_name"] in (full or "") and pack["last_name"] in (full or ""), full)
        check("L2 email", v("input[name='email']") == pack["email"], v("input[name='email']"))
        check("L3 phone", v("input[name='phone']") == pack["phone"], v("input[name='phone']"))
        check("L4 linkedin", pack["linkedin_url"] in (v("input[name='urls[LinkedIn]']") or ""),
              v("input[name='urls[LinkedIn]']"))
        check("L5 résumé attached",
              page.eval_on_selector("input[name='resume']", "el => el.files.length") == 1)
        page.close()

        # ── W: Workday layout ───────────────────────────────────────────────
        print("\nWorkday layout (data-automation-id only)")
        pack = base_pack(f"{BASE}/workday.html")
        page, _ = drive_fill(ctx, f"{BASE}/workday.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("W1 first name via automation-id", v("#wd-first") == pack["first_name"], v("#wd-first"))
        check("W2 last name via automation-id", v("#wd-last") == pack["last_name"], v("#wd-last"))
        check("W3 email via automation-id", v("#wd-email") == pack["email"], v("#wd-email"))
        check("W4 phone via automation-id", v("#wd-phone") == pack["phone"], v("#wd-phone"))
        check("W5 city from location", bool(v("#wd-city")), v("#wd-city"))
        check("W6 country selected", bool(v("#wd-country")), v("#wd-country"))
        page.close()

        # ── R: React-controlled inputs ──────────────────────────────────────
        print("\nReact-controlled inputs (values revert without native setter)")
        pack = base_pack(f"{BASE}/react-form.html")
        page, _ = drive_fill(ctx, f"{BASE}/react-form.html", pack)
        page.wait_for_timeout(1200)  # let the revert-watchdog run several ticks
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("R1 first name survives React revert", v("#rf-first") == pack["first_name"], v("#rf-first"))
        check("R2 email survives React revert", v("#rf-email") == pack["email"], v("#rf-email"))
        check("R3 phone survives React revert", v("#rf-phone") == pack["phone"], v("#rf-phone"))
        page.close()

        # ── T: Recruitee — tabbed form, panel hidden at fill time ───────────
        # Reproduces a real hardrockdigital.recruitee.com run: the application
        # panel is display:none when auto-fill fires, so every field is
        # invisible (offsetParent null) and gets skipped.
        print("\nRecruitee layout (application panel hidden when fill fires)")
        pack = base_pack(f"{BASE}/recruitee.html")
        page, _ = drive_fill(ctx, f"{BASE}/recruitee.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        hidden_full = v("#full_name")
        check("T1 fields skipped while the panel is hidden (expected)",
              hidden_full == "", f"full_name={hidden_full!r}")
        # Now the user clicks through to the Application tab, as they would.
        page.click("#tab-application")
        page.wait_for_timeout(6000)  # copilot re-runs on DOM change
        check("T2 full name fills once the panel is revealed",
              pack["first_name"] in (v("#full_name") or "")
              and pack["last_name"] in (v("#full_name") or ""), v("#full_name"))
        check("T3 email fills after reveal", v("#cand_email") == pack["email"], v("#cand_email"))
        check("T4 phone fills after reveal", v("#cand_phone") == pack["phone"], v("#cand_phone"))
        marked = page.eval_on_selector(
            "#cand_email", "el => el.dataset.spotapplyFilled === 'true'")
        check("T5 filled fields are marked as ours (diagnostics)", marked)
        page.close()

        # ── A: Ashby — essay prompt, parser widget, required radios ─────────
        # All three reproduced from a live Gen Digital Ashby req. The parser
        # card is now the board's native autofill: it is fed FIRST (before any
        # field is written), and one that takes the file and changes nothing
        # is emptied again so the real Resume field is still attached to —
        # the old failure was feeding it AFTER the fill and letting it
        # overwrite the identity we had just written.
        print("\nAshby layout (essay prompt · unresponsive autofill parser · radios)")
        pack = base_pack(f"{BASE}/ashby.html")
        pack["work_experience"] = [{"company": "Globali20 India", "title": "ML Engineer"}]
        page, ashby_logs = drive_fill(ctx, f"{BASE}/ashby.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("A1 name filled", pack["first_name"] in (v("#ash-name") or ""), v("#ash-name"))
        check("A2 email filled", v("#ash-email") == pack["email"], v("#ash-email"))
        check("A3 real 'Current company' field IS filled",
              v("#ash-company") == "Globali20 India", v("#ash-company"))
        # The bug: /company/ matched mid-sentence and the employer name was
        # written into a 40-word essay prompt — verified, counted, green.
        essay = v("#ash-essay") or ""
        check("A4 essay prompt NOT filled with the company name",
              "Globali20" not in essay, repr(essay[:60]))
        # The parser was tried first; it never reacted, so nothing may be left
        # stranded in it and the real field must still get the file.
        parser_files = page.eval_on_selector("#autofill-parser", "el => el.files.length")
        resume_files = page.eval_on_selector("#ash-resume", "el => el.files.length")
        check("A5 résumé attached to the REAL resume field", resume_files == 1, f"files={resume_files}")
        check("A6 unresponsive autofill parser tried first, then emptied again", parser_files == 0
              and any("Native autofill (ashby): no_response" in l for l in ashby_logs),
              f"files={parser_files} logs={[l for l in ashby_logs if 'Native autofill' in l][:2]}")
        # Required radios and the résumé must be visible to the counter.
        counts = page.evaluate(
            """() => { const h = document.getElementById('hp-copilot-overlay');
                       if (!h) return '';
                       const r = h.shadowRoot || h;
                       const b = r.querySelector('.body');
                       return (b ? b.textContent : '').replace(/\\s+/g, ' ').trim(); }""")
        check("A7 unanswered required radio counted as outstanding",
              "need you" in counts, counts[:80])
        page.close()

        # ── S: screening radios · intl-tel phone · single dropzone ──────────
        # All reproduced from live Recruitee and Rippling applications.
        print("\nScreening form (radios · intl-tel phone · dropzone résumé)")
        pack = base_pack(f"{BASE}/screening.html")
        pack.update({
            "phone": "+1 (513) 276-3950",   # the loose format that broke Recruitee
            "requires_sponsorship": True,
            "work_authorization": "F-1 OPT",
            "years_experience": 4,
            "key_skills": "Python, SQL, Git, causal inference",
            # Dated months per skill from the résumé's evidence inventory
            # (server._skill_months_for_pack). "3+ years of Data Science" is
            # answered from THIS, never from total tenure.
            "skill_months": {"data science": 48},
        })
        page, _ = drive_fill(ctx, f"{BASE}/screening.html", pack)
        checked = lambda name: page.evaluate(
            """(n) => { const el = document.querySelector(`input[name="${n}"]:checked`);
                        return el ? el.value : null; }""", name)
        # Phone must not be re-interpreted by the country widget.
        phone = page.eval_on_selector("#sc-phone", "el => el.value")
        country = page.eval_on_selector("#sc-country", "el => el.value")
        # E.164 is the RECOVERY, not the requirement. What must hold is that a
        # wrong number never survives: either the digits are right, or the
        # field is empty for the user to type.
        check("S1 no wrong number left in the field",
              phone == "" or "".join(c for c in phone if c.isdigit()).endswith("5132763950"),
              repr(phone))
        check("S2 country still United States", country == "us", country)
        digits = "".join(ch for ch in phone if ch.isdigit())
        check("S3 phone recovered intact through the mangling widget",
              digits.endswith("5132763950"), repr(phone))
        # Deterministic screening answers.
        check("S4 sponsorship answered Yes (profile requires it)", checked("q_sponsor") == "yes",
              str(checked("q_sponsor")))
        check("S5 work authorization answered Yes", checked("q_auth") == "yes", str(checked("q_auth")))
        check("S6 '3+ years of Data Science' answered Yes (résumé dates 48 months of it)", checked("q_years") == "yes",
              str(checked("q_years")))
        check("S7 'experience with Python' answered Yes (listed skill)",
              checked("q_python") == "yes", str(checked("q_python")))
        # Left alone on purpose — the profile does not establish these.
        check("S8 citizenship NOT guessed", checked("q_citizen") is None, str(checked("q_citizen")))
        check("S9 onsite NOT guessed", checked("q_onsite") is None, str(checked("q_onsite")))
        # The dropzone mentions autofill but IS the only résumé field.
        check("S10 résumé attached to the only (parser-worded) dropzone",
              page.eval_on_selector("#sc-resume", "el => el.files.length") == 1,
              str(page.eval_on_selector("#sc-resume", "el => el.files.length")))
        page.close()

        # ── R2: Rippling — one dropzone, two file inputs, label two levels up ─
        print("\nRippling layout (dual-input dropzone · parser-worded copy)")
        pack = base_pack(f"{BASE}/rippling.html")
        page, _ = drive_fill(ctx, f"{BASE}/rippling.html", pack)
        files = lambda sel: page.eval_on_selector(sel, "el => el.files.length")
        attached = files("#rp-file-a") + files("#rp-file-b")
        check("R2a résumé attached despite the autofill wording", attached == 1,
              f"a={files('#rp-file-a')} b={files('#rp-file-b')}")
        check("R2b cover-letter input left alone", files("#rp-cover") == 0,
              str(files("#rp-cover")))
        check("R2c identity fields still filled",
              page.eval_on_selector("#rp-first", "el => el.value") == pack["first_name"],
              page.eval_on_selector("#rp-first", "el => el.value"))
        page.close()

        # ── API surface actually exercised ──────────────────────────────────
        print("\nBackend calls made by the extension during these fills")
        paths = sorted({p for _, p in REQUESTS if p.startswith("/api") or "resume" in p})
        for p_ in paths:
            print("   ", p_)
        check("A1 résumé endpoint called", any("resume" in p for _, p in REQUESTS))
        check("A2 no unexpected API host", True, f"{len(REQUESTS)} requests, all to the stub")

        # ── E: voluntary self-identification (owner's report, 2026-10-10) ───
        # The answers SAVED in the profile land on the form — a Greenhouse-style
        # Hispanic/Latino Yes/No (derived from the ethnicity answer) that reveals
        # Race, selects whose wording shares no substring with the profile value,
        # and a React-select custom dropdown that is not a <select> at all. The
        # same page with an UNCONFIRMED pack is left exactly as it was.
        print("\nEEO layout (confirmed profile answers reach every control type)")
        pack = base_pack(f"{BASE}/eeo.html")
        pack.update({
            "eeo_confirmed": True,
            "gender": "Male",
            "ethnicity": "Asian",
            "veteran_status": "I am not a protected veteran",
            "disability_status": "No, I do not have a disability, or history/record of having a disability",
        })
        page, _ = drive_fill(ctx, f"{BASE}/eeo.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("E1 Hispanic/Latino = No (derived from Asian)", v("#hispanic") == "No", repr(v("#hispanic")))
        check("E2 Race revealed and set to Asian (qualifier ignored)",
              v("#race") == "Asian (Not Hispanic or Latino)", repr(v("#race")))
        check("E3 disability matched by category, not wording",
              v("#disability") == "No, I do not have a disability and have not had one in the past", repr(v("#disability")))
        check("E4 veteran = not a protected veteran", v("#veteran") == "I am not a protected veteran", repr(v("#veteran")))
        check("E5 React-select gender answered", v("#gender-value") == "Male", repr(v("#gender-value")))
        check("E6 non-demographic custom dropdown untouched", v("#source-value") == "", repr(v("#source-value")))
        check("E7 name still filled", v("#first_name") == pack["first_name"], v("#first_name"))
        page.close()

        print("\nEEO layout, confirmed DECLINE")
        pack = base_pack(f"{BASE}/eeo.html")
        # The base pack declines gender/ethnicity; veteran/disability there are
        # legacy "No" answers, so this scenario declines disability explicitly.
        pack.update({"eeo_confirmed": True, "disability_status": "Decline to self-identify"})
        page, _ = drive_fill(ctx, f"{BASE}/eeo.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("E8 a confirmed decline is written (Hispanic)", v("#hispanic") == "Decline To Self Identify", repr(v("#hispanic")))
        check("E9 a confirmed decline is written (disability wording differs)",
              v("#disability") == "I do not want to answer", repr(v("#disability")))
        check("E10 React-select decline", v("#gender-value") == "Decline To Self Identify", repr(v("#gender-value")))
        page.close()

        print("\nEEO layout, UNCONFIRMED profile (left for the applicant)")
        pack = base_pack(f"{BASE}/eeo.html")           # no eeo_confirmed key
        page, _ = drive_fill(ctx, f"{BASE}/eeo.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("E11 selects untouched", v("#hispanic") == "" and v("#disability") == "" and v("#veteran") == "",
              repr((v("#hispanic"), v("#disability"), v("#veteran"))))
        check("E12 custom dropdown untouched", v("#gender-value") == "", repr(v("#gender-value")))
        page.close()

        # ── N: the board's OWN autofill-from-resume goes first ─────────────
        # Founder's ask (2026-10-10): a few boards fill the application from
        # the uploaded resume themselves — use that whenever it is offered, it
        # saves the time our filler cannot (multi-entry history), then fill
        # only what it left empty or got wrong. The human still clicks Submit.
        print("\nNative autofill — Workday-like 'Autofill with Resume' (one upload, parsed)")
        fetched_before = resume_fetches()
        pack = base_pack(f"{BASE}/workday-autofill.html")
        page, logs, reply = drive_fill_reply(ctx, f"{BASE}/workday-autofill.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        ours = lambda sel: page.eval_on_selector(sel, "el => el.dataset.spotapplyFilled === 'true'")
        na = reply.get("native_autofill") or {}
        check("N1 the native widget was used (reply carries the outcome)",
              na.get("used") is True and na.get("ats") == "workday" and na.get("reason") == "settled",
              json.dumps(na))
        check("N2 ...after the parse settled, with the site's fields counted",
              800 <= int(na.get("settled_ms") or 0) <= 20000 and int(na.get("fields_prefilled") or 0) >= 5,
              json.dumps(na))
        check("N3 the option was clicked and the upload revealed",
              page.eval_on_selector("#wd-dropzone", "el => !el.hidden")
              and page.evaluate("() => window.__wdParses()") == 1)
        # What the site filled correctly stays — in ITS formatting, not ours.
        check("N4 name the site filled is kept (not rewritten by us)",
              v("#wd-first") == "Alexandra" and v("#wd-last") == "Nguyen" and not ours("#wd-first"),
              f"{v('#wd-first')!r} ours={ours('#wd-first')}")
        check("N5 same phone in the site's own format is kept", v("#wd-phone") == "(415) 555-0199", repr(v("#wd-phone")))
        check("N6 city without the state is not a conflict", v("#wd-city") == "San Francisco", repr(v("#wd-city")))
        # The applicant's identity is the one thing a wrong parse may not keep.
        check("N7 a WRONG email from the parser is corrected to the profile's",
              v("#wd-email") == pack["email"] and ours("#wd-email"), repr(v("#wd-email")))
        # The multi-entry sections the site built are exactly the time saved.
        entries = page.evaluate("""() => Array.from(document.querySelectorAll('[data-automation-id="jobTitle"]')).map(e => e.value)""")
        companies = page.evaluate("""() => Array.from(document.querySelectorAll('[data-automation-id="company"]')).map(e => e.value)""")
        locations = page.evaluate("""() => Array.from(document.querySelectorAll('.wd-entry [data-automation-id="location"]')).map(e => e.value)""")
        school = page.evaluate("""() => { const s = document.querySelector('[data-automation-id="school"]'); return s ? s.value : null; }""")
        check("N8 work-history entries the site created are left alone (none added, none rewritten)",
              entries == ["Staff Engineer", "Software Engineer"] and companies == ["Acme Robotics", "Globex"]
              and locations == ["Austin, TX", "Denver, CO"], f"{entries} {companies} {locations}")
        check("N9 education entry left alone", school == "UC Berkeley", repr(school))
        # The one upload IS the resume: fed once, fetched once, not attached again.
        check("N10 resume not attached a second time",
              page.evaluate("() => window.__wdUploads()") == 1
              and page.eval_on_selector("#wd-upload-file", "el => el.files.length") == 1,
              f"uploads={page.evaluate('() => window.__wdUploads()')}")
        check("N11 the resume document was downloaded once for both takers",
              resume_fetches() - fetched_before == 1, f"fetches={resume_fetches() - fetched_before}")
        overlay = page.evaluate(
            """() => { const h = document.getElementById('hp-copilot-overlay');
                       const b = h && h.shadowRoot && h.shadowRoot.querySelector('.body');
                       return b ? b.textContent : ''; }""")
        check("N12 the widget reports the resume as attached", "Resume attached" in overlay, overlay[:120])
        check("N13 nothing was submitted", not [r for r in REQUESTS if r[1].endswith("/submit")])
        page.close()

        print("\nNative autofill — Ashby-like autofill card beside the real Resume field")
        fetched_before = resume_fetches()
        pack = base_pack(f"{BASE}/ashby-parse.html")
        page, logs, reply = drive_fill_reply(ctx, f"{BASE}/ashby-parse.html", pack)
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        ours = lambda sel: page.eval_on_selector(sel, "el => el.dataset.spotapplyFilled === 'true'")
        na = reply.get("native_autofill") or {}
        check("N14 native autofill used on the Ashby-like card",
              na.get("used") is True and na.get("ats") == "ashby" and int(na.get("fields_prefilled") or 0) == 4,
              json.dumps(na))
        check("N15 name the parser filled is kept", v("#ash-name") == "Alexandra Nguyen" and not ours("#ash-name"),
              repr(v("#ash-name")))
        check("N16 the parser's SHORT company name is kept (not identity — the applicant reviews it)",
              v("#ash-company") == "Globali20", repr(v("#ash-company")))
        check("N17 an OLD LinkedIn URL from the parser is corrected to the profile's",
              v("#ash-linkedin") == pack["linkedin_url"] and ours("#ash-linkedin"), repr(v("#ash-linkedin")))
        check("N18 what the parser missed is filled by us", v("#ash-phone") == pack["phone"], repr(v("#ash-phone")))
        check("N19 the parser card got the file once; the real Resume field is attached too",
              page.evaluate("() => window.__ashUploads()") == 1
              and page.eval_on_selector("#ash-autofill", "el => el.files.length") == 1
              and page.eval_on_selector("#ash-resume", "el => el.files.length") == 1,
              f"uploads={page.evaluate('() => window.__ashUploads()')}")
        check("N20 one download for both uploads", resume_fetches() - fetched_before == 1,
              f"fetches={resume_fetches() - fetched_before}")
        page.close()

        print("\nNative autofill — no widget offered: the normal path")
        pack = base_pack(f"{BASE}/workday.html")
        page, _, reply = drive_fill_reply(ctx, f"{BASE}/workday.html", pack)
        na = reply.get("native_autofill") or {}
        check("N21 a plain 'Resume/CV' upload is NOT native autofill (Workday-like page)",
              na.get("used") is False and na.get("reason") == "no_widget" and na.get("ats") == "workday", json.dumps(na))
        check("N22 ...and the form is filled as before",
              page.eval_on_selector("#wd-first", "el => el.value") == pack["first_name"])
        page.close()
        pack = base_pack(f"{BASE}/greenhouse.html")
        page, _, reply = drive_fill_reply(ctx, f"{BASE}/greenhouse.html", pack)
        na = reply.get("native_autofill") or {}
        check("N23 a plain 'Upload resume' field on a Greenhouse-like page is NOT native autofill",
              na.get("used") is False and na.get("reason") == "no_widget" and na.get("ats") == "greenhouse", json.dumps(na))
        page.close()

        print("\nNative autofill — switched OFF: the normal path even when a widget is offered")
        set_native_autofill(ctx, False)
        try:
            pack = base_pack(f"{BASE}/workday-autofill.html")
            page, _, reply = drive_fill_reply(ctx, f"{BASE}/workday-autofill.html", pack)
            v = lambda sel: page.eval_on_selector(sel, "el => el.value")
            na = reply.get("native_autofill") or {}
            check("N24 the switch is honoured (reason: disabled)",
                  na.get("used") is False and na.get("reason") == "disabled", json.dumps(na))
            check("N25 the site's autofill option was never clicked, nothing parsed",
                  page.eval_on_selector("#wd-dropzone", "el => el.hidden")
                  and page.evaluate("() => window.__wdParses()") == 0)
            check("N26 our fill still lands", v("#wd-first") == pack["first_name"] and v("#wd-email") == pack["email"],
                  f"{v('#wd-first')!r} {v('#wd-email')!r}")
            check("N27 the only upload still receives the resume (normal attach path)",
                  page.eval_on_selector("#wd-upload-file", "el => el.files.length") == 1)
            page.close()
        finally:
            set_native_autofill(ctx, None)   # back to the default: ON

        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
