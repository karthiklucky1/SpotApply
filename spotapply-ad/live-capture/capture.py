"""Screenshot the REAL SpotApply UI (local demo server, fictional seed data).

    . live/env.sh && live/venv/bin/python live/capture.py [step ...]

Steps (default: all, in order): board match tailor submitted outreach settings
landing oneclick. Each step runs at the PHONE viewport (430x932 @2.5x, mobile)
and the DESKTOP viewport (1280x800 @2x). The script only drives the page the
way a user would (clicks, scrolls) plus three capture-only interventions, all
logged:
  * network: only 127.0.0.1:$DEMO_PORT and Google Fonts (through the session's
    egress proxy) are allowed; everything else is aborted (jsdelivr CDN, the
    /api/favicon logo proxy, and POST /api/jobs/{id}/verify, which would make
    the server fetch the employer's ATS page);
  * localStorage: hp_ext_installed=1 (what the page itself writes once the
    Chrome extension answers its ping; the story's candidate has it) and
    hp_shortlist_sort=fresh (the default);
  * the Tailoring Studio is opened on an already-tailored application by
    calling the page's own openTailorStudio() + _tsRenderResult() with the real
    /details payload, because reaching it by clicking needs an LLM run.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright

LIVE = os.environ["LIVE_DIR"]
PORT = int(os.environ.get("DEMO_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = os.path.join(LIVE, "shots")
PY = os.path.join(LIVE, "venv", "bin", "python")
STATE = json.load(open(os.path.join(LIVE, "seed_state.json")))
HERO = STATE["app_ids"]["lumen"]
SUBMITTED = STATE["app_ids"]["orchard"]

VIEWPORTS = {
    "phone": dict(viewport={"width": 430, "height": 932}, device_scale_factor=2.5,
                  is_mobile=True, has_touch=True,
                  user_agent=("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36")),
    "desktop": dict(viewport={"width": 1280, "height": 800}, device_scale_factor=2),
}

ALLOWED_EXTERNAL = {"fonts.googleapis.com", "fonts.gstatic.com"}
LOG = {"blocked": [], "aborted_local": [], "console_errors": [], "page_errors": [], "shots": []}

INIT = """
try {
  localStorage.setItem('hp_ext_installed', '1');
  localStorage.setItem('hp_shortlist_sort', 'fresh');
} catch (e) {}
"""


def phase(*args):
    out = subprocess.run([PY, os.path.join(LIVE, "phase.py"), *args], capture_output=True, text=True,
                         env=os.environ.copy())
    print(out.stdout.strip() or out.stderr.strip()[-400:])


async def router(route):
    req = route.request
    u = urlparse(req.url)
    if u.hostname in ("127.0.0.1", "localhost"):
        if u.path.startswith("/api/favicon") or re.fullmatch(r"/api/jobs/\d+/verify", u.path):
            LOG["aborted_local"].append(f"{req.method} {u.path}")
            return await route.abort()
        return await route.continue_()
    if u.hostname in ALLOWED_EXTERNAL:
        return await fulfill_font(route)
    LOG["blocked"].append(req.url[:160])
    return await route.abort()


_FONT_CACHE: dict = {}
_HTTP = None


async def fulfill_font(route):
    """Google Fonts (CSS + woff2), fetched by THIS script through the session's
    egress proxy and handed to the page; the browser itself never leaves 127.0.0.1."""
    global _HTTP
    import httpx
    req = route.request
    key = (req.url, req.headers.get("user-agent", ""))
    if key not in _FONT_CACHE:
        if _HTTP is None:
            _HTTP = httpx.AsyncClient(verify=os.environ.get("SSL_CERT_FILE") or "/root/.ccr/ca-bundle.crt",
                                      timeout=30, follow_redirects=True)
        r = await _HTTP.get(req.url, headers={"user-agent": req.headers.get("user-agent", "Mozilla/5.0")})
        _FONT_CACHE[key] = (r.status_code, r.headers.get("content-type", ""), r.content)
        LOG.setdefault("fonts", []).append(f"{r.status_code} {req.url[:120]}")
    status, ctype, body = _FONT_CACHE[key]
    return await route.fulfill(status=status, body=body,
                               headers={"content-type": ctype, "access-control-allow-origin": "*"})


async def settle(page, ms=1200):
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    try:
        await page.evaluate("document.fonts.ready.then(() => true)")
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def shot(page, vp, name, full=False, what=""):
    path = os.path.join(SHOTS, vp, f"{name}.png")
    await page.screenshot(path=path, full_page=full)
    LOG["shots"].append({"path": path, "viewport": vp, "full_page": full, "what": what})
    print("saved", path)
    return path


async def scroll_frames(page, vp, name, selector, what, max_frames=8, overlap=0.15):
    """Viewport screenshots while scrolling a scrollable container (modals and
    drawers scroll inside themselves, so a full-page screenshot cannot show them)."""
    paths = []
    info = await page.evaluate("""(sel) => { const el = document.querySelector(sel);
        if (!el) return null; el.scrollTop = 0;
        return {h: el.clientHeight, sh: el.scrollHeight}; }""", selector)
    if not info:
        print("no scroll container", selector)
        return paths
    step = max(1, int(info["h"] * (1 - overlap)))
    pos, i = 0, 1
    while i <= max_frames:
        await page.evaluate("([sel, y]) => { document.querySelector(sel).scrollTop = y; }", [selector, pos])
        await page.wait_for_timeout(450)
        paths.append(await shot(page, vp, f"{name}_scroll{i:02d}", what=f"{what} (scroll frame {i})"))
        if pos + info["h"] >= info["sh"] - 2:
            break
        pos += step
        i += 1
    await page.evaluate("(sel) => { document.querySelector(sel).scrollTop = 0; }", selector)
    return paths


async def open_dashboard(page):
    await page.goto(f"{BASE}/dashboard", wait_until="domcontentloaded")
    await settle(page, 1800)


async def step_board(page, vp):
    await open_dashboard(page)
    await shot(page, vp, "01_board_top", what="Dashboard on load (top of page)")
    # Bring the shortlist (stage chips + early-window divider) to the top.
    await page.evaluate("""() => { const t = document.getElementById('pipe-tabs');
        if (t) window.scrollTo(0, t.getBoundingClientRect().top + window.scrollY - (innerWidth < 769 ? 96 : 12)); }""")
    await page.wait_for_timeout(500)
    await shot(page, vp, "01_board_shortlist", what="Shortlisted pane: Apply today divider + Lumen Data card first")
    await page.evaluate("""() => { const d = document.getElementById('early-window-divider');
        if (d) window.scrollTo(0, d.getBoundingClientRect().top + window.scrollY - (innerWidth < 769 ? 96 : 8)); }""")
    await page.wait_for_timeout(500)
    await shot(page, vp, "01_board_divider", what="Scrolled so the Apply today divider is at the top")
    await page.reload(wait_until="domcontentloaded")
    await settle(page, 1500)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)
    await shot(page, vp, "01_board_full", full=True, what="Dashboard, full page")


async def step_match(page, vp):
    await open_dashboard(page)
    await page.click(f"#shortlisted-container .jobrow[data-app-id='{HERO}']", position={"x": 60, "y": 60})
    await page.wait_for_selector("#jobModalOverlay.open")
    await page.wait_for_function("""() => { const p = document.querySelector('#jobDrawerBody .match-panel-placeholder');
        return p && /fit for your resume/.test(p.textContent); }""", timeout=15000)
    await settle(page, 1500)
    await shot(page, vp, "02_match_report", what="Lumen Data job open: AI match report (94)")
    # The match report proper: "Why this matched you" at the top.
    await page.evaluate("""() => { const body = document.getElementById('jobDrawerBody');
        const r = body.querySelector('.jd-report'); if (r) body.scrollTop = r.offsetTop - 12; }""")
    await page.wait_for_timeout(500)
    await shot(page, vp, "02_match_report_why", what="Match report scrolled to its header (score + why it matches)")
    await scroll_frames(page, vp, "02_match_report", "#jobDrawerBody", "Lumen Data job drawer", max_frames=10)


async def step_tailor(page, vp):
    await open_dashboard(page)
    # (a) Tailoring Studio, finished state, from the real /details payload.
    await page.evaluate(f"""async () => {{
        openTailorStudio({HERO}, 'Senior Backend Engineer', 'Lumen Data');
        const d = await _tsFetchDetails();
        _tsRenderResult(d);
    }}""")
    await page.wait_for_timeout(4500)          # the typewriter reveal (55 ms a line)
    await shot(page, vp, "03_tailoring_studio_end", what="Tailoring Studio as the reveal leaves it (auto-scrolled to the end)")
    await page.evaluate("document.getElementById('ts-scroll').scrollTop = 0")
    await page.wait_for_timeout(300)
    await shot(page, vp, "03_tailoring_studio", what="Tailoring Studio: tailored resume (top), grounded + quality pills")
    await scroll_frames(page, vp, "03_tailoring_studio", "#ts-scroll", "Tailoring Studio resume", max_frames=6)
    await page.click("#ts-tab-cover")
    await page.wait_for_timeout(4500)          # the letter types in too, and the pane follows it
    await shot(page, vp, "03_tailoring_studio_cover_end", what="Cover letter tab as the reveal leaves it")
    await page.evaluate("document.getElementById('ts-scroll').scrollTop = 0")
    await page.wait_for_timeout(300)
    await shot(page, vp, "03_tailoring_studio_cover", what="Tailoring Studio: cover letter tab (top)")
    await page.evaluate("closeTailorStudio()")
    await page.wait_for_timeout(400)
    # (b) View Documents (the card's real button) -> resume + pre-download review.
    await page.click(f"#shortlisted-container .jobrow[data-app-id='{HERO}'] [data-role='view-docs']")
    await page.wait_for_selector("#predownload-review:not(.hidden)", timeout=15000)
    await settle(page, 800)
    await shot(page, vp, "03_documents_modal", what="View Documents modal (tailored resume)")
    sel = "#modal .overflow-y-auto"
    await scroll_frames(page, vp, "03_documents_modal", sel, "View Documents modal incl. pre-download review",
                        max_frames=6)
    await page.evaluate("""() => { const box = document.getElementById('predownload-review');
        const sc = box.closest('.overflow-y-auto'); sc.scrollTop = box.offsetTop - 16; }""")
    await page.wait_for_timeout(400)
    await shot(page, vp, "03_predownload_review", what="Pre-download review (GET /application/{id}/review)")
    await page.click("#tab-cover")
    await page.wait_for_timeout(500)
    await page.evaluate("(sel) => { document.querySelector(sel).scrollTop = 0; }", sel)
    await shot(page, vp, "03_documents_cover", what="View Documents modal: cover letter tab")


async def step_submitted(page, vp):
    await open_dashboard(page)
    await page.click("#pipe-tabs .pipe-tab:has-text('Submitted')")
    await page.wait_for_timeout(600)
    await page.evaluate("""() => { const t = document.getElementById('pipe-tabs');
        if (t) window.scrollTo(0, t.getBoundingClientRect().top + window.scrollY - (innerWidth < 769 ? 96 : 12)); }""")
    await page.wait_for_timeout(500)
    await shot(page, vp, "04_submitted_receipt", what="Submitted tab: earliness receipt (Applied 3h 12m after posting)")


async def step_outreach(page, vp):
    await open_dashboard(page)
    # Submitted application: drafts + what was sent + the reply.
    await page.click("#pipe-tabs .pipe-tab:has-text('Submitted')")
    await page.wait_for_timeout(500)
    await page.click(f"#submitted-container .jobrow[data-app-id='{SUBMITTED}']", position={"x": 60, "y": 40})
    await page.wait_for_selector("#jobModalOverlay.open")
    await settle(page, 800)
    btn = page.locator("#jobDrawerBody button:has-text('Draft Outreach Message')")
    await btn.scroll_into_view_if_needed()
    await btn.click()
    await page.wait_for_selector("#referralOverlay.open")
    await page.wait_for_function("() => !!document.querySelector('#referralBody #outreach-draft-0')", timeout=15000)
    await settle(page, 800)
    await shot(page, vp, "05_outreach_kit", what="Outreach kit (Orchard Systems, submitted application)")
    await scroll_frames(page, vp, "05_outreach_kit", "#referralBody", "Outreach kit, Orchard Systems", max_frames=10)
    # The sent list + reply at the bottom.
    await page.evaluate("""() => { const b = document.getElementById('referralBody');
        const s = document.getElementById('outreach-sent-list'); if (s) b.scrollTop = s.offsetTop - 200; }""")
    await page.wait_for_timeout(400)
    await shot(page, vp, "05_outreach_sent", what="Outreach kit: Sent list with a reply received")
    # Same kit for the hero application (drafts only).
    await page.evaluate("closeReferral()")
    await page.evaluate(f"openReferral({HERO})")
    await page.wait_for_function("() => !!document.querySelector('#referralBody #outreach-draft-0')", timeout=15000)
    await settle(page, 600)
    await shot(page, vp, "05_outreach_kit_lumen", what="Outreach kit for Lumen Data (drafts, no sends yet)")


async def step_settings(page, vp, suffix=""):
    await open_dashboard(page)
    await page.evaluate("openProfileModal()")
    await page.wait_for_selector("#profile-form:not(.hidden)", timeout=15000)
    await settle(page, 800)
    await page.evaluate("""() => { const s = document.querySelector('select[name=one_click_submit]');
        const box = s.closest('div'); box.scrollIntoView({block: 'center'}); }""")
    await page.wait_for_timeout(500)
    await shot(page, vp, f"06_settings_one_click{suffix}",
               what="My Profile (settings): One-click submit select + explanation")
    await page.evaluate("document.getElementById('profile-shell').scrollTop = 0")
    await page.wait_for_timeout(300)
    await shot(page, vp, f"06_profile_top{suffix}", what="My Profile modal, top")


async def step_landing(page, vp):
    await page.goto(f"{BASE}/", wait_until="domcontentloaded")
    await settle(page, 2500)
    await shot(page, vp, "07_landing_hero", what="Landing page hero (/)")
    await shot(page, vp, "07_landing_full", full=True, what="Landing page, full page")


async def step_oneclick(page, vp):
    await step_settings(page, vp, suffix="_on")
    await open_dashboard(page)
    await page.evaluate("""() => { const d = document.getElementById('early-window-divider');
        if (d) window.scrollTo(0, d.getBoundingClientRect().top + window.scrollY - (innerWidth < 769 ? 96 : 8)); }""")
    await page.wait_for_timeout(500)
    await shot(page, vp, "08_board_one_click_on", what="Board with one-click submit ON (Apply & Submit)")


STEPS = {
    "board": ("untailored", step_board),
    "match": ("untailored", step_match),
    "tailor": ("tailored", step_tailor),
    "submitted": (None, step_submitted),
    "outreach": (None, step_outreach),
    "settings": (None, step_settings),
    "landing": (None, step_landing),
    "oneclick": ("oneclick_on", step_oneclick),
}


async def main(steps):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for name in steps:
            ph, fn = STEPS[name]
            if ph == "oneclick_on":
                phase("untailored"); phase("oneclick", "on")
            elif ph:
                phase(ph)
            for vp, opts in VIEWPORTS.items():
                os.makedirs(os.path.join(SHOTS, vp), exist_ok=True)
                ctx = await browser.new_context(**opts, service_workers="block", locale="en-US",
                                                timezone_id="America/Chicago", color_scheme="light")
                await ctx.add_init_script(INIT)
                await ctx.route("**/*", router)
                page = await ctx.new_page()
                page.on("console", lambda m, vp=vp, n=name: LOG["console_errors"].append(
                    f"[{n}/{vp}] {m.text[:200]}") if m.type == "error" else None)
                page.on("pageerror", lambda e, vp=vp, n=name: LOG["page_errors"].append(f"[{n}/{vp}] {str(e)[:300]}"))
                try:
                    await fn(page, vp)
                except Exception as e:
                    print(f"STEP {name}/{vp} FAILED: {e}")
                    LOG.setdefault("failures", []).append(f"{name}/{vp}: {str(e)[:300]}")
                    try:
                        await shot(page, vp, f"zz_failed_{name}", what=f"state when {name} failed")
                    except Exception:
                        pass
                await ctx.close()
            if ph == "oneclick_on":
                phase("oneclick", "off")
        await browser.close()
    phase("untailored")
    with open(os.path.join(LIVE, "logs", "capture_log.json"), "w") as f:
        json.dump(LOG, f, indent=1)
    print(json.dumps({k: (v if k != "shots" else len(v)) for k, v in LOG.items()}, indent=1)[:4000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(STEPS)))
