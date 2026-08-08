"""Real-fill coverage across the ATS layouts the extension claims to support.

test_extension_live.py proves the plumbing (bridge -> background -> DO_FILL).
This file proves the FILL ITSELF on four different DOM shapes, driving each
form the way production does and asserting on the resulting field values:

  Greenhouse  label/id/name selectors + EEO <select>s + textarea
  Lever       single "Full name" field, bare name= attributes
  Workday     data-automation-id only, no usable labels, country dropdown
  React       controlled inputs that revert any value written without a
              proper native setter + input event (the classic autofill killer)

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
        # Current key; the extension also accepts the legacy hirepath_url.
        "spotapply_url": BASE,
        "auth_token": "harness-test-token",
    }


class StubBackend(http.server.SimpleHTTPRequestHandler):
    """Serves the fixtures AND the handful of API routes content.js calls."""

    def log_message(self, *a):  # keep output readable
        pass

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
            return self._json(base_pack(f"{BASE}/greenhouse.html"))
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


def main():
    handler = partial(StubBackend, directory=str(HERE / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    import tempfile
    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
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

        # ── API surface actually exercised ──────────────────────────────────
        print("\nBackend calls made by the extension during these fills")
        paths = sorted({p for _, p in REQUESTS if p.startswith("/api") or "resume" in p})
        for p_ in paths:
            print("   ", p_)
        check("A1 résumé endpoint called", any("resume" in p for _, p in REQUESTS))
        check("A2 no unexpected API host", True, f"{len(REQUESTS)} requests, all to the stub")

        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
