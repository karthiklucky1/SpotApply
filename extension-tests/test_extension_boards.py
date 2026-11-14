"""The shapes real job boards use, as found on live pages (2026-11-14).

A run of the extension over 22 real postings (nothing submitted, no account
made, no CAPTCHA touched) showed what the synthetic forms had not:

  Greenhouse (new boards)  every dropdown is a pick-one widget whose visible
      input is only a search box. Answers were TYPED into it — "Yes", the job
      title, "California" — and thrown away by the form; three questions that
      merely contained the word "position" got the applicant's job title.
  Lever        the work-authorization questions are radio cards whose question
      text sits two levels above the radios; all were left blank. "Commence
      ('sponsor') an immigration case" is a sponsorship question.
  Teamtailor   the job page's only email box is the employer's STAFF sign-in;
      the applicant's email went there and the form never opened.
  SmartRecruiters / JOIN   "I'm interested" and "Jetzt bewerben" were not
      recognised as the apply control.
  The submit check counted optional empty boxes and read a picked dropdown as
      empty, so no real form would ever have been submitted.
  Saved answers (the profile's auto-fill data) reached text boxes and
      <select>s, never a pick-one dropdown or a radio question.

  Ashby        the site reads the resume and EMPTIES the upload box; the
      attached resume was read as a required field still missing.

The fixtures in www/ mirror those shapes (greenhouse-react.html,
lever-radios.html, ashby-upload.html, teamtailor-job.html,
smartrecruiters-job.html, join-job.html). The helpers and the stub API are test_extension_forms.py's.
"""
import importlib.util
import json
import sys
import tempfile
import threading
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("forms_harness", HERE / "test_extension_forms.py")
fh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fh)

BASE = fh.BASE
check = fh.check
results = fh.results


def diagnostic(ctx, needle):
    """Popup → "Copy diagnostic report", asked of the tab showing `needle`."""
    return ctx.service_workers[0].evaluate(
        """(needle) => new Promise((resolve) => {
            chrome.tabs.query({}, (tabs) => {
                const t = tabs.find(t => t.url && t.url.includes(needle));
                if (!t) return resolve({ error: 'no-tab' });
                chrome.tabs.sendMessage(t.id, { type: 'DIAGNOSTIC_REPORT' }, (r) =>
                    resolve(r || { error: (chrome.runtime.lastError && chrome.runtime.lastError.message) || 'no reply' }));
            });
        })""", needle) or {}


PICKED = """(slot) => {
    const v = document.querySelector('#' + slot + ' .select__single-value');
    return v ? v.textContent.trim() : '';
}"""
RADIO = """(name) => {
    const r = document.querySelector('input[type="radio"][name="' + name + '"]:checked');
    return r ? r.value : '';
}"""


def main():
    handler = partial(fh.StubBackend, directory=str(HERE / "www"))
    import http.server
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", fh.PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    events = lambda since: [p for m, p in fh.REQUESTS[since:] if m == "POST" and "telemetry" in p]
    submits = lambda since: [p for m, p in fh.REQUESTS[since:] if m == "POST" and p.endswith("/submit")]

    with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=True,
            args=[f"--disable-extensions-except={fh.EXT}", f"--load-extension={fh.EXT}",
                  "--disable-features=DisableLoadExtensionCommandLineSwitch"],
            **fh._LAUNCH_KW)
        waited = 0
        while not ctx.service_workers and waited < 60:
            ctx.pages[0].wait_for_timeout(250)
            waited += 1
        if not ctx.service_workers:
            print("FAIL: service worker never registered")
            sys.exit(1)

        # ── GR: Greenhouse's new boards — pick-one dropdowns ────────────────
        print("\nGreenhouse new boards (every dropdown is a pick-one widget)")
        url = f"{BASE}/greenhouse-react.html"
        pack = fh.base_pack(url)
        page, logs, reply = fh.drive_fill_reply(ctx, url, pack)
        picked = lambda slot: page.evaluate(PICKED, slot)
        typed = page.evaluate("() => window.__typed")
        v = lambda sel: page.eval_on_selector(sel, "el => el.value")
        check("GR1 the phone's country is PICKED from the list (\"United States +1\")",
              picked("slot-country") == "United States +1", repr(picked("slot-country")))
        check("GR2 work authorization: Yes is picked", picked("slot-auth") == "Yes", repr(picked("slot-auth")))
        check("GR3 sponsorship: No is picked", picked("slot-sponsor") == "No", repr(picked("slot-sponsor")))
        check("GR4 the employer's own question (a referral) is left for the person",
              picked("slot-refer") == "", repr(picked("slot-refer")))
        check("GR5 nothing was ever TYPED into a Yes/No or country dropdown",
              not any(typed.get(k) for k in ("country", "question_auth", "question_sponsor", "question_refer")),
              json.dumps(typed))
        check("GR6 the city is searched and picked from the suggestions",
              picked("slot-location") == "San Francisco, California, United States", repr(picked("slot-location")))
        check("GR7 a question that merely says \"position\" does not get the job title",
              v("#question_why") == "", repr(v("#question_why")))
        check("GR7b ...while \"Current title\" still does",
              v("#question_title") == pack["current_title"], repr(v("#question_title")))
        check("GR8 the resume is attached and acknowledged (the input was replaced by its name)",
              page.evaluate("() => !document.getElementById('resume') && !!window.__resumeName"),
              str(page.evaluate("() => window.__resumeName || ''")))
        gate = (diagnostic(ctx, "greenhouse-react.html").get("submitReadiness") or {}).get("gate") or {}
        check("GR9 the submit check reads the form as ready: nothing REQUIRED is open",
              gate.get("verdict") == "ready" and gate.get("requiredOpen") == 0, json.dumps(gate))
        check("GR9b ...though optional boxes are still empty (they are the person's to leave)",
              (gate.get("optionalOpen") or 0) >= 2, json.dumps(gate))
        check("GR9c ...and nothing was pressed by asking", page.url.endswith("/greenhouse-react.html"), page.url)
        page.close()

        # The same form, approved for one-click: optional empties do not hold it.
        oc_from = len(fh.REQUESTS)
        approved = dict(pack, auto_submit=True, auto_submit_reason="ok", auto_submit_hosts=["127.0.0.1"])
        page, logs, reply = fh.drive_fill_reply(ctx, url, approved, timeout_ms=60000)
        auto = reply.get("auto_submit") or {}
        check("GR10 a complete form with empty OPTIONAL boxes is submitted",
              auto.get("attempted") is True and auto.get("reason") == "clicked", json.dumps(auto))
        try:
            page.wait_for_url("**/thanks.html**", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        check("GR10b ...and recorded only on the employer's confirmation page",
              "thanks.html" in page.url and len(submits(oc_from)) == 1, page.url + " " + str(submits(oc_from)))
        page.close()

        # A required dropdown no profile can answer: held, and never guessed.
        oc_from = len(fh.REQUESTS)
        held_url = f"{BASE}/greenhouse-react.html?clearance=1"
        page, logs, reply = fh.drive_fill_reply(ctx, held_url, dict(approved, apply_url=held_url), timeout_ms=60000)
        auto = reply.get("auto_submit") or {}
        typed = page.evaluate("() => window.__typed")
        check("GR11 an unanswered REQUIRED dropdown holds the submit back",
              auto.get("attempted") is False and auto.get("reason") == "fields_need_you", json.dumps(auto))
        check("GR11b ...it was neither picked nor typed into",
              page.evaluate(PICKED, "slot-clearance") == "" and not typed.get("question_clearance"),
              json.dumps(typed.get("question_clearance")))
        check("GR11c ...and nothing was submitted", not submits(oc_from) and "greenhouse-react.html" in page.url,
              page.url + " " + str(submits(oc_from)))
        page.close()

        # A student on OPT: "now or in the future" is Yes.
        opt = dict(pack, work_authorization="F-1 OPT", work_auth_status="OPT",
                   requires_sponsorship=True, authorized_now=None)
        page, logs, reply = fh.drive_fill_reply(ctx, url, opt)
        check("GR12 on OPT the sponsorship dropdown says Yes",
              page.evaluate(PICKED, "slot-sponsor") == "Yes", repr(page.evaluate(PICKED, "slot-sponsor")))
        check("GR12b ...and authorization without an end date stays the applicant's",
              page.evaluate(PICKED, "slot-auth") == "", repr(page.evaluate(PICKED, "slot-auth")))
        page.close()

        # ── SA: answers the person SAVED reach dropdowns and radios ─────────
        # The profile's "Auto-fill data" (and answers given on earlier forms)
        # come back from /api/recall-answers. Text boxes and <select>s took
        # them already; a pick-one dropdown and a radio group did not, and the
        # answer was written into the dropdown's hidden bookkeeping input.
        print("\nSaved answers (profile auto-fill data) on dropdowns and radios")
        shadow = "() => document.querySelector('#slot-clearance .req-shadow').value"
        held_pack = dict(fh.base_pack(held_url), auto_submit=True, auto_submit_reason="ok",
                         auto_submit_hosts=["127.0.0.1"])
        try:
            fh.RECALL.clear()
            fh.RECALL.update({"current clearance level": "None"})
            oc_from = len(fh.REQUESTS)
            page, logs, reply = fh.drive_fill_reply(ctx, held_url, held_pack, timeout_ms=60000)
            auto = reply.get("auto_submit") or {}
            check("SA1 the saved answer is PICKED in the dropdown (the one option that says it)",
                  any("Saved answer picked: None" in l for l in logs), str([l for l in logs if "Saved" in l]))
            try:
                page.wait_for_url("**/thanks.html**", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            check("SA2 ...which completes the form: the approved application is submitted",
                  auto.get("attempted") is True and "thanks.html" in page.url and len(submits(oc_from)) == 1,
                  json.dumps(auto) + " " + page.url)
            page.close()

            # A saved answer that is none of the options: nothing is picked,
            # nothing typed, and the hidden bookkeeping input stays empty.
            fh.RECALL.clear()
            fh.RECALL.update({"current clearance level": "Confidential"})
            oc_from = len(fh.REQUESTS)
            page, logs, reply = fh.drive_fill_reply(ctx, held_url, held_pack, timeout_ms=60000)
            auto = reply.get("auto_submit") or {}
            typed = page.evaluate("() => window.__typed")
            check("SA3 a saved answer that is not one of the options is not guessed",
                  page.evaluate(PICKED, "slot-clearance") == "" and not typed.get("question_clearance"),
                  repr(page.evaluate(PICKED, "slot-clearance")) + " " + json.dumps(typed.get("question_clearance")))
            check("SA3b ...nor written into the dropdown's hidden bookkeeping input",
                  page.evaluate(shadow) == "", repr(page.evaluate(shadow)))
            check("SA3c ...so the form is still held for the person",
                  auto.get("attempted") is False and auto.get("reason") == "fields_need_you" and not submits(oc_from),
                  json.dumps(auto))
            page.close()

            # Work authorization keeps its ONE interpreter: a saved "Yes" never
            # answers it for a status the profile cannot prove.
            fh.RECALL.clear()
            fh.RECALL.update({"legally authorized": "Yes", "sponsorship": "No"})
            opt_pack = dict(fh.base_pack(url), work_authorization="F-1 OPT", work_auth_status="OPT",
                            requires_sponsorship=True, authorized_now=None)
            page, logs, reply = fh.drive_fill_reply(ctx, url, opt_pack)
            check("SA4 a saved answer never answers a work-authorization question",
                  page.evaluate(PICKED, "slot-auth") == "" and page.evaluate(PICKED, "slot-sponsor") == "Yes",
                  repr((page.evaluate(PICKED, "slot-auth"), page.evaluate(PICKED, "slot-sponsor"))))
            page.close()

            # Radio questions: the employer's own question, answered only
            # because the person saved the answer to exactly that question.
            fh.RECALL.clear()
            fh.RECALL.update({"are you local to austin": "Yes"})
            lever_local = f"{BASE}/lever-radios.html?local=1"
            page, logs, reply = fh.drive_fill_reply(ctx, lever_local, fh.base_pack(lever_local))
            check("SA5 a saved Yes checks the \"Yes.\" radio of that question",
                  page.evaluate(RADIO, "cards[a1][field3]") == "Yes.", repr(page.evaluate(RADIO, "cards[a1][field3]")))
            check("SA5b ...and the profile's own answers are unchanged",
                  page.evaluate(RADIO, "cards[a1][field0]") == "Yes." and page.evaluate(RADIO, "cards[a1][field1]") == "No.",
                  repr((page.evaluate(RADIO, "cards[a1][field0]"), page.evaluate(RADIO, "cards[a1][field1]"))))
            page.close()

            fh.RECALL.clear()
            fh.RECALL.update({"are you local to austin": "Sometimes"})
            page, logs, reply = fh.drive_fill_reply(ctx, lever_local, fh.base_pack(lever_local))
            check("SA6 a saved answer that matches no radio leaves the question open",
                  page.evaluate(RADIO, "cards[a1][field3]") == "", repr(page.evaluate(RADIO, "cards[a1][field3]")))
            page.close()
        finally:
            fh.RECALL.clear()

        # ── LV: Lever radio cards ───────────────────────────────────────────
        print("\nLever radio cards (the question sits two levels above the radios)")
        url = f"{BASE}/lever-radios.html"
        pack = fh.base_pack(url)
        page, logs, reply = fh.drive_fill_reply(ctx, url, pack)
        radio = lambda name: page.evaluate(RADIO, name)
        check("LV1 authorized to work: Yes", radio("cards[a1][field0]") == "Yes.", repr(radio("cards[a1][field0]")))
        check("LV2 sponsorship now or in the future: No", radio("cards[a1][field1]") == "No.",
              repr(radio("cards[a1][field1]")))
        check("LV3 \"commence ('sponsor') an immigration case\" is the same question: No",
              radio("cards[a1][field2]") == "No.", repr(radio("cards[a1][field2]")))
        page.close()

        oc_from = len(fh.REQUESTS)
        local_url = f"{BASE}/lever-radios.html?local=1"
        approved = dict(pack, apply_url=local_url, auto_submit=True, auto_submit_reason="ok",
                        auto_submit_hosts=["127.0.0.1"])
        page, logs, reply = fh.drive_fill_reply(ctx, local_url, approved, timeout_ms=60000)
        auto = reply.get("auto_submit") or {}
        check("LV4 the employer's own question (\"Are you local to Austin, TX?\") is not guessed",
              page.evaluate(RADIO, "cards[a1][field3]") == "", repr(page.evaluate(RADIO, "cards[a1][field3]")))
        check("LV4b ...and, being required, it holds the submit back",
              auto.get("attempted") is False and auto.get("reason") == "fields_need_you" and not submits(oc_from),
              json.dumps(auto))
        page.close()

        opt = dict(pack, work_authorization="F-1 OPT", work_auth_status="OPT",
                   requires_sponsorship=True, authorized_now=None)
        page, logs, reply = fh.drive_fill_reply(ctx, url, opt)
        check("LV5 on OPT both sponsorship wordings say Yes",
              page.evaluate(RADIO, "cards[a1][field1]") == "Yes." and page.evaluate(RADIO, "cards[a1][field2]") == "Yes.",
              repr((page.evaluate(RADIO, "cards[a1][field1]"), page.evaluate(RADIO, "cards[a1][field2]"))))
        page.close()

        # ── AU: an upload box the site empties after taking the file ────────
        print("\nAshby upload box (the site reads the file, then empties the input)")
        url = f"{BASE}/ashby-upload.html"
        pack = fh.base_pack(url)
        page, logs, reply = fh.drive_fill_reply(ctx, url, pack)
        gate = (diagnostic(ctx, "ashby-upload.html").get("submitReadiness") or {}).get("gate") or {}
        check("AU1 the site emptied the input and shows the file's name",
              page.eval_on_selector("#_systemfield_resume", "el => el.files.length") == 0 and
              "Alexandra_Nguyen_Resume" in page.inner_text("#resume-shown"), page.inner_text("#resume-shown"))
        check("AU2 ...and the resume counts as attached: nothing required is open",
              gate.get("verdict") == "ready" and gate.get("requiredOpen") == 0, json.dumps(gate))
        # The person removes the file: the field is required and empty again.
        page.click("#resume-clear")
        page.wait_for_timeout(300)
        gate = (diagnostic(ctx, "ashby-upload.html").get("submitReadiness") or {}).get("gate") or {}
        check("AU3 once the person removes the file the field is open again",
              gate.get("verdict") == "fields_need_you" and gate.get("requiredOpen") == 1 and
              any("file" in f for f in gate.get("requiredOpenFields") or []), json.dumps(gate))
        page.close()

        oc_from = len(fh.REQUESTS)
        approved = dict(pack, auto_submit=True, auto_submit_reason="ok", auto_submit_hosts=["127.0.0.1"])
        page, logs, reply = fh.drive_fill_reply(ctx, url, approved, timeout_ms=60000)
        auto = reply.get("auto_submit") or {}
        try:
            page.wait_for_url("**/thanks.html**", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        check("AU4 the approved form is submitted and confirmed",
              auto.get("attempted") is True and "thanks.html" in page.url and len(submits(oc_from)) == 1,
              json.dumps(auto) + " " + page.url)
        page.close()

        # Ashby's own resume autofill puts the file in the Resume field: the
        # field's input never holds it, and it is still attached.
        parsed_url = f"{BASE}/ashby-upload.html?parsed=1"
        page, logs, reply = fh.drive_fill_reply(ctx, parsed_url, dict(pack, apply_url=parsed_url), timeout_ms=60000)
        gate = (diagnostic(ctx, "ashby-upload.html").get("submitReadiness") or {}).get("gate") or {}
        check("AU5 a resume the board's own autofill placed in the field counts as attached",
              gate.get("verdict") == "ready" and gate.get("requiredOpen") == 0 and
              "Alexandra_Nguyen_Resume" in page.inner_text("#resume-shown"), json.dumps(gate))
        page.close()

        # ── TT / SR / JN: the control that opens the application ────────────
        print("\nOpening the application (Teamtailor, SmartRecruiters, JOIN)")
        url = f"{BASE}/teamtailor-job.html"
        page, logs = fh.drive_fill(ctx, url, fh.base_pack(url))
        check("TT1 \"Apply for this job\" is pressed: the application form opens",
              "apply.html" in page.url, page.url)
        check("TT2 ...and is filled",
              "apply.html" in page.url and page.eval_on_selector("#first_name", "el => el.value") == "Alexandra",
              page.url)
        page.close()
        # The staff sign-in box itself: on the job page, before anything is pressed.
        page = ctx.new_page()
        page.goto(url)
        page.wait_for_timeout(1500)
        report = diagnostic(ctx, "teamtailor-job.html").get("submitReadiness") or {}
        check("TT3 the employer's staff sign-in box is not an application form",
              report.get("applicationForm") is False and report.get("applyButtonOnly") is True, json.dumps(report))
        page.close()

        url = f"{BASE}/smartrecruiters-job.html"
        page, logs = fh.drive_fill(ctx, url, fh.base_pack(url))
        check("SR1 \"I'm interested\" opens the application", "apply.html?from=smartrecruiters" in page.url, page.url)
        # The shortcut records its press in sessionStorage, which the form page
        # (same origin, same tab) can still read after the navigation.
        pressed_linkedin = page.evaluate("() => sessionStorage.getItem('linkedin_pressed')")
        check("SR2 the \"with LinkedIn\" shortcut is never the one pressed", not pressed_linkedin,
              repr(pressed_linkedin))
        page.close()

        url = f"{BASE}/join-job.html"
        page, logs = fh.drive_fill(ctx, url, fh.base_pack(url))
        check("JN1 \"Jetzt bewerben\" is pressed once and the form opens",
              page.evaluate("() => window.__applyPressed") == 1 and
              page.evaluate("() => !!document.getElementById('join-form')"),
              str(page.evaluate("() => window.__applyPressed")))
        check("JN2 ...and the applicant's email lands in it",
              page.eval_on_selector("#email", "el => el.value") == "alexandra.nguyen@example.com",
              repr(page.eval_on_selector("#email", "el => el.value")))
        page.close()

        ctx.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    for name, ok, detail in results:
        if not ok:
            print("  FAILED:", name, "—", detail)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
