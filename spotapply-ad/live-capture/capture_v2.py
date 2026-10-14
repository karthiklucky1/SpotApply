"""Ad v2 captures on top of capture.py: the HERO role (Lumen Data) as the
submitted application, and its outreach kit's email draft. Phone viewport only.

    . live/env.sh && live/venv/bin/python live/capture_v2.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture as C  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


async def step_submitted_lumen(page, vp):
    await C.open_dashboard(page)
    await page.click("#pipe-tabs .pipe-tab:has-text('Submitted')")
    await page.wait_for_timeout(700)
    await page.evaluate("""() => { const t = document.getElementById('pipe-tabs');
        if (t) window.scrollTo(0, t.getBoundingClientRect().top + window.scrollY - 96); }""")
    await page.wait_for_timeout(500)
    await C.shot(page, vp, "v2_submitted_lumen", what="Submitted tab, Lumen Data on top with its receipt")
    await C.shot(page, vp, "v2_submitted_lumen_full", full=True, what="same, full page")


async def step_outreach_lumen(page, vp):
    await C.open_dashboard(page)
    await page.evaluate(f"openReferral({C.HERO})")
    await page.wait_for_function("() => !!document.querySelector('#referralBody #outreach-draft-0')", timeout=15000)
    await C.settle(page, 700)
    await C.shot(page, vp, "v2_outreach_lumen_top", what="Outreach kit, Lumen Data, top")
    found = await page.evaluate("""() => {
        const b = document.getElementById('referralBody');
        const el = [...b.querySelectorAll('*')].find(n => n.children.length === 0 && /Hiring-manager note/.test(n.textContent));
        if (!el) return false;
        const card = el.closest('[id^="outreach-draft-"]') || el.parentElement.parentElement;
        b.scrollTop += card.getBoundingClientRect().top - b.getBoundingClientRect().top - 12;
        return true; }""")
    await page.wait_for_timeout(500)
    await C.shot(page, vp, "v2_outreach_lumen_email", what=f"Outreach kit, Lumen email draft (found={found})")


async def main():
    C.phase("submitted")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        opts = C.VIEWPORTS["phone"]
        for fn in (step_submitted_lumen, step_outreach_lumen):
            ctx = await browser.new_context(**opts, service_workers="block", locale="en-US",
                                            timezone_id="America/Chicago", color_scheme="light")
            await ctx.add_init_script(C.INIT)
            await ctx.route("**/*", C.router)
            page = await ctx.new_page()
            page.on("pageerror", lambda e: print("PAGEERROR", str(e)[:300]))
            await fn(page, "phone")
            await ctx.close()
        await browser.close()
    C.phase("untailored")


if __name__ == "__main__":
    asyncio.run(main())
