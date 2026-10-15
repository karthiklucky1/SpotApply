"""Screen recording of the REAL Chrome extension filling an application form.

    . live/env.sh && live/venv/bin/python live/record_extension.py

The demo server must be running (./restart.sh). This launches Chromium with
the repo's MV3 extension (extension/, unmodified), opens the REAL dashboard at
desktop size and clicks the Lumen Data card's real "Auto-Fill" button. The
dashboard fetches the real fill pack from the demo server and hands it to the
extension, which opens the job's apply URL and fills it.

The employer page is the one thing that is not real: there is no Lumen Data.
boards.greenhouse.io is answered by this script (a local HTTPS server and
host-resolver rules, the way extension-tests/test_extension_audit.py serves
its synthetic forms) with a
plain application form for the fictional role, so the extension meets an
ordinary Greenhouse-hosted form. Nothing is sent anywhere: the "submit" lands
on a thank-you page this script serves.

Capture-time interventions: the dashboard holds a placeholder session token
(see main()), because the extension attaches the resume only for a signed-in
dashboard and local demo mode has no sign-in; and backdrop blur is off
(record_reel.NO_BLUR_CSS).

The person's part is driven by the script and shown with a drawn pointer: they
read the filled fields, type the one answer the extension leaves to them
("How did you hear about this job?" is never guessed: how_heard_answer is
empty in the demo profile), and click Submit themselves.

Output: shots/reel/desk_dashboard_fill.mp4 (the click) and
shots/reel/desk_extension.mp4 (the form), each with a .json of marks.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import ssl
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture as C  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from record_reel import NO_BLUR_CSS, OUT  # noqa: E402
from screencast import CURSOR_JS, Screencast, smooth_scroll  # noqa: E402

EXT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "extension"))
CHROME = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
JOB_PATH = "/lumendata/jobs/4821093"
# Headless Chromium with an extension draws each tab into a 641 px-tall content
# area whatever viewport is emulated (measured: innerHeight 780, frames 1282
# px tall at 2x), which cut the extension's status card off at the bottom. The
# page is given the height it really gets.
VIEW = {"width": 1000, "height": 640}
HTTPS_PORT = 8943

FORM = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Job Application for Senior Backend Engineer at Lumen Data</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: Inter, system-ui, sans-serif; color: #1f2933; background: #fff; }
  .top { border-bottom: 1px solid #e4e7eb; padding: 18px 0; }
  .wrap { width: 640px; margin: 0 auto; }
  .co { display: flex; align-items: center; gap: 12px; font-weight: 700; font-size: 18px; }
  .logo { width: 36px; height: 36px; border-radius: 8px; background: #1d4ed8; color: #fff;
          display: grid; place-items: center; font-weight: 800; }
  h1 { font-size: 28px; margin: 28px 0 4px; letter-spacing: -0.01em; }
  .loc { color: #616e7c; font-size: 15px; margin-bottom: 26px; }
  h2 { font-size: 18px; margin: 0 0 18px; padding-top: 22px; border-top: 1px solid #e4e7eb; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .f { margin-bottom: 18px; }
  label { display: block; font-size: 14px; font-weight: 600; margin-bottom: 6px; }
  label .req { color: #c81e1e; }
  input[type=text], input[type=email], input[type=tel], select {
    width: 100%; height: 42px; border: 1px solid #cbd2d9; border-radius: 6px; padding: 0 12px;
    font: 15px Inter, system-ui, sans-serif; color: #1f2933; background: #fff; }
  input:focus, select:focus { outline: 2px solid #1d4ed8; outline-offset: 0; border-color: #1d4ed8; }
  .drop { border: 1px dashed #9aa5b1; border-radius: 6px; padding: 14px; display: flex;
          align-items: center; gap: 12px; font-size: 14px; color: #52606d; }
  .drop input { font-size: 13px; }
  .chip { display: none; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px;
          background: #eef2ff; color: #1e3a8a; font-weight: 600; font-size: 14px; }
  .chip.on { display: inline-flex; }
  .loading { color: #7b8794; font-size: 15px; padding: 30px 0; }
  button[type=submit] { margin: 10px 0 60px; height: 46px; padding: 0 26px; border: 0; border-radius: 6px;
          background: #1d4ed8; color: #fff; font: 600 16px Inter, system-ui, sans-serif; cursor: pointer; }
</style></head><body>
<div class="top"><div class="wrap co"><span class="logo">L</span> Lumen Data</div></div>
<div class="wrap">
  <h1>Senior Backend Engineer</h1>
  <div class="loc">Remote (US) · Platform Engineering</div>
  <div id="app-slot"><p class="loading">Loading application…</p></div>
</div>
<template id="app-form">
  <form id="application" action="/lumendata/jobs/4821093/confirmation" method="get">
    <h2>Apply for this job</h2>
    <div class="row">
      <div class="f"><label for="first_name">First Name <span class="req">*</span></label>
        <input type="text" id="first_name" name="first_name" autocomplete="off" required></div>
      <div class="f"><label for="last_name">Last Name <span class="req">*</span></label>
        <input type="text" id="last_name" name="last_name" autocomplete="off" required></div>
    </div>
    <div class="row">
      <div class="f"><label for="email">Email <span class="req">*</span></label>
        <input type="email" id="email" name="email" autocomplete="off" required></div>
      <div class="f"><label for="phone">Phone <span class="req">*</span></label>
        <input type="tel" id="phone" name="phone" autocomplete="off" required></div>
    </div>
    <div class="f"><label for="location">Location (City) <span class="req">*</span></label>
      <input type="text" id="location" name="location" autocomplete="off" required></div>
    <div class="f"><label for="resume">Resume/CV <span class="req">*</span></label>
      <div class="drop"><input type="file" id="resume" name="resume" accept=".pdf,.doc,.docx" required>
        <span class="chip" id="resume-chip"></span></div></div>
    <div class="row">
      <div class="f"><label for="company">Current Company</label>
        <input type="text" id="company" name="current_company" autocomplete="off"></div>
      <div class="f"><label for="title">Current Title</label>
        <input type="text" id="title" name="current_title" autocomplete="off"></div>
    </div>
    <div class="f"><label for="auth">Are you legally authorized to work in the United States? <span class="req">*</span></label>
      <select id="auth" name="work_authorized" required><option value="">Select...</option>
        <option>Yes</option><option>No</option></select></div>
    <div class="f"><label for="sponsor">Will you now or in the future require sponsorship for employment visa status? <span class="req">*</span></label>
      <select id="sponsor" name="requires_sponsorship" required><option value="">Select...</option>
        <option>Yes</option><option>No</option></select></div>
    <div class="f"><label for="heard">How did you hear about this job? <span class="req">*</span></label>
      <input type="text" id="heard" name="how_heard" autocomplete="off" required></div>
    <button type="submit" id="submit_app">Submit Application</button>
  </form>
</template>
<script>
  // Like many hosted application pages, the form arrives a moment after the
  // page itself, so the recording sees it empty before the extension fills it.
  setTimeout(() => {
    const slot = document.getElementById('app-slot');
    slot.replaceChildren(document.getElementById('app-form').content.cloneNode(true));
    document.getElementById('resume').addEventListener('change', onResume);
  }, 1600);
  function onResume(e) {
    const f = e.target.files && e.target.files[0]; const c = document.getElementById('resume-chip');
    if (f) { c.textContent = 'Attached: ' + f.name; c.classList.add('on'); }
  }
</script></body></html>"""

THANKS = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Application submitted</title>
<style>body{margin:0;font-family:Inter,system-ui,sans-serif;color:#1f2933}
.top{border-bottom:1px solid #e4e7eb;padding:18px 0}.wrap{width:640px;margin:0 auto}
.co{display:flex;align-items:center;gap:12px;font-weight:700;font-size:18px}
.logo{width:36px;height:36px;border-radius:8px;background:#1d4ed8;color:#fff;display:grid;place-items:center;font-weight:800}
h1{font-size:30px;margin:60px 0 10px}p{font-size:17px;color:#52606d;line-height:1.5}</style></head><body>
<div class="top"><div class="wrap co"><span class="logo">L</span> Lumen Data</div></div>
<div class="wrap"><h1>Thank you for applying!</h1>
<p>We have received your application for Senior Backend Engineer. Our team will review it and be in touch.</p></div>
</body></html>"""


class EmployerPages(http.server.BaseHTTPRequestHandler):
    """boards.greenhouse.io, answered locally (the extension's own tabs bypass
    Playwright routing, so they reach this through --host-resolver-rules)."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        body = FORM if path == JOB_PATH else THANKS if path == JOB_PATH + "/confirmation" else None
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "").encode())


def start_employer_pages(tmpdir) -> list[str]:
    key, cert = os.path.join(tmpdir, "k.pem"), os.path.join(tmpdir, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-keyout", key, "-out", cert, "-subj", "/CN=boards.greenhouse.io"],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HTTPS_PORT), EmployerPages)
    srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return [f"--host-resolver-rules=MAP boards.greenhouse.io 127.0.0.1:{HTTPS_PORT}",
            "--ignore-certificate-errors"]


async def router(route):
    if urlparse(route.request.url).hostname == "boards.greenhouse.io":
        return await route.continue_()
    return await C.router(route)


async def glide(page, selector, steps=16, pause=420):
    box = await page.locator(selector).bounding_box()
    await page.mouse.move(box["x"] + min(box["width"] - 30, 160), box["y"] + box["height"] / 2, steps=steps)
    await page.wait_for_timeout(pause)


async def main():
    C.phase("tailored")
    os.makedirs(OUT, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="reel_ext_")
    page_args = start_employer_pages(tempfile.mkdtemp(prefix="reel_tls_"))
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            prof, headless=True, viewport=VIEW, device_scale_factor=2, locale="en-US",
            timezone_id="America/Chicago", color_scheme="light",
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--disable-features=DisableLoadExtensionCommandLineSwitch",
                  "--force-device-scale-factor=2", "--no-proxy-server", *page_args],
            ignore_https_errors=True,
            **({"executable_path": CHROME} if CHROME else {}))
        for _ in range(60):
            if ctx.service_workers:
                break
            await asyncio.sleep(0.25)
        if not ctx.service_workers:
            raise SystemExit("extension service worker did not start")
        await ctx.add_init_script(C.INIT)
        # A signed-in dashboard sends its session token, and the fill pack hands
        # it to the extension, which attaches the tailored resume only when it
        # has one (content.js attachResume). Local demo mode has no sign-in, so
        # the dashboard is given a placeholder; the local server ignores it.
        await ctx.add_init_script(
            f"if (location.origin === '{C.BASE}') localStorage.setItem('sb_token', 'local-demo-session');")
        await ctx.add_init_script(f"document.addEventListener('DOMContentLoaded', () => ({CURSOR_JS})());")
        await ctx.route("**/*", router)

        # ── the dashboard: the real Auto-Fill button ──────────────────────────
        # A NEW tab: the window's first tab gets an even shorter content area.
        page = await ctx.new_page()
        for extra in [p for p in ctx.pages if p is not page]:
            await extra.close()
        await C.open_dashboard(page)
        await page.add_style_tag(content=NO_BLUR_CSS)
        row = f"#shortlisted-container .jobrow[data-app-id='{C.HERO}']"
        await page.evaluate(f"""() => document.querySelector("{row}").scrollIntoView({{block: 'center'}})""")
        await page.mouse.move(820, 300)
        await page.wait_for_timeout(1500)                 # the dashboard pings the extension
        btn = f"{row} button:has-text('Auto-Fill')"
        rec = Screencast(page, "desk_dashboard_fill")
        await rec.start()
        await page.wait_for_timeout(700)
        box = await page.locator(btn).bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=22)
        await page.wait_for_timeout(300)
        rec.mark("click")
        async with ctx.expect_page(timeout=20000) as opened:
            await page.mouse.down()
            await page.mouse.up()
            await page.wait_for_timeout(900)
        form = await opened.value
        # Attach to the new tab at once: the extension starts filling as soon as
        # the form exists, so anything slower than this misses the empty form.
        frec = Screencast(form, "desk_extension")
        await frec.start()
        frec.mark("opened")
        await rec.stop(tail_ms=100)
        logs = []
        form.on("console", lambda m: logs.append(m.text[:200]))
        if os.environ.get("REEL_DEBUG"):
            await form.wait_for_timeout(int(os.environ.get("REEL_DEBUG_WAIT", "12000")))
            print("  url:", form.url)
            print("  values:", await form.evaluate("""() => Object.fromEntries([...document.querySelectorAll('input:not([type=file]),select')].map(e => [e.id, e.value]))"""))
            print("  console:", *logs[-25:], sep="\n    ")
            raise SystemExit(0)

        # ── the employer's form, filled by the extension ──────────────────────
        await form.wait_for_load_state()
        await form.wait_for_function("() => document.getElementById('email') && document.getElementById('email').value", timeout=30000)
        frec.mark("filling")
        await form.wait_for_function("() => document.getElementById('resume-chip').classList.contains('on')", timeout=30000)
        await form.wait_for_timeout(2500)                 # the extension finishes and reports
        frec.mark("filled")
        state = await form.evaluate("""() => Object.fromEntries([...document.querySelectorAll('input:not([type=file]),select')]
            .map(e => [e.id, e.value]))""")
        print("  filled:", state)
        overlay = await form.evaluate("""() => { const h = document.getElementById('hp-copilot-overlay');
            const b = h && h.shadowRoot && h.shadowRoot.querySelector('.body'); return b ? b.innerText : ''; }""")
        print("  extension says:", " ".join(overlay.split())[:240])
        # The person reads it over.
        for sel in ["#first_name", "#email", "#location", "#resume"]:
            await glide(form, sel)
        frec.mark("review_scroll")
        await smooth_scroll(form, None, 420, 1200)
        for sel in ["#company", "#auth", "#sponsor"]:
            await glide(form, sel)
        # ...and answers the one question the extension left to them.
        await glide(form, "#heard", pause=200)
        frec.mark("type_heard")
        await form.mouse.down()
        await form.mouse.up()
        await form.keyboard.type("Company website", delay=85)
        await form.wait_for_timeout(700)
        frec.mark("typed")
        await glide(form, "#submit_app", pause=500)
        frec.mark("submit")
        await form.mouse.down()
        await form.mouse.up()
        await form.wait_for_url("**/confirmation**", timeout=15000)
        frec.mark("thanks")
        await form.wait_for_timeout(3200)
        await frec.stop()
        rec.save(OUT)
        frec.save(OUT)
        await ctx.close()
    C.phase("untailored")


if __name__ == "__main__":
    asyncio.run(main())
