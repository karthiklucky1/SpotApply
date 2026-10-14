"""Captures for the 60-second widescreen explainer (DESKTOP 1280x800 @2x):
the sponsorship card as a candidate who needs sponsorship sees it, and the
hero role as SUBMITTED (receipt) with its outreach email draft.

    . live/env.sh && live/venv/bin/python live/capture_v3.py
(run capture.py board match tailor settings first for the other screens)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture as C  # noqa: E402
import capture_v2 as V2  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


async def step_sponsor(page, vp):
    await C.open_dashboard(page)
    await page.click(f"#shortlisted-container .jobrow[data-app-id='{C.HERO}']", position={"x": 60, "y": 60})
    await page.wait_for_selector("#jobModalOverlay.open")
    await page.wait_for_function("""() => { const p = document.querySelector('#jobDrawerBody .match-panel-placeholder');
        return p && /fit for your resume/.test(p.textContent); }""", timeout=15000)
    await C.settle(page, 1200)
    found = await page.evaluate("""() => {
        const body = document.getElementById('jobDrawerBody');
        const el = [...body.querySelectorAll('*')].find(n => n.children.length === 0 && /^Sponsorship/.test(n.textContent.trim()));
        if (!el) return null;
        const card = el.closest('div[class*="rounded"]') || el.parentElement;
        body.scrollTop += card.getBoundingClientRect().top - body.getBoundingClientRect().top - 420;
        return el.textContent.trim(); }""")
    await page.wait_for_timeout(500)
    await C.shot(page, vp, "v3_sponsor_needs", what=f"Sponsorship card for a candidate who needs sponsorship ({found})")


async def run(steps):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        opts = C.VIEWPORTS["desktop"]
        for phase, fn in steps:
            for ph in phase:
                C.phase(*ph)
            ctx = await browser.new_context(**opts, service_workers="block", locale="en-US",
                                            timezone_id="America/Chicago", color_scheme="light")
            await ctx.add_init_script(C.INIT)
            await ctx.route("**/*", C.router)
            page = await ctx.new_page()
            page.on("pageerror", lambda e: print("PAGEERROR", str(e)[:300]))
            await fn(page, "desktop")
            await ctx.close()
        await browser.close()
    C.phase("citizen")
    C.phase("untailored")


if __name__ == "__main__":
    asyncio.run(run([
        ((("untailored",), ("needs_sponsorship",)), step_sponsor),
        ((("citizen",), ("submitted",)), V2.step_submitted_lumen),
        ((), V2.step_outreach_lumen),
    ]))
