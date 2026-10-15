"""Screen recordings of the REAL app for the vertical reel (fictional demo data).

    . live/env.sh && live/venv/bin/python live/record_reel.py [take ...]

Takes (default: all):
  phone_match  phone 430x932 @2.5: the board, the "Apply today" Lumen Data card,
               a tap, the match report scrolling through why it fits, the fit
               breakdown and the keyword match (6/6, nothing missing)
  phone_gap    phone: the Meridian Freight card, whose posting asks for Redis,
               which Maya's resume does not have: the keyword match names it
  desk_tailor  desktop 1000x780 @2: the Tailoring Studio while it works, then
               the tailored resume typing in, the top of the page, and the cover
               letter tab

Same capture rules as capture.py (fictional seed, no outbound network). One
capture-time intervention, the same one capture.py makes and for the same
reason: the studio is opened with the page's own openTailorStudio() and filled
with _tsRenderResult() on the real /details payload, because a real run needs
an LLM. The stage view it shows first is the one a real run shows while the
model works; the reel labels the jump between them "sped up". Desktop takes
also switch off CSS backdrop blur (see NO_BLUR_CSS): a recording-speed
change to the dimmed area around a modal, not to anything the app says.

Output: shots/reel/<take>.mp4 + <take>.json (marks in seconds), copied to
../public/reel/ by publish_reel.sh.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture as C  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from screencast import CURSOR_JS, TOUCH_JS, Screencast, smooth_scroll  # noqa: E402

OUT = os.path.join(C.SHOTS, "reel")
MERIDIAN = C.STATE["app_ids"]["meridian"]
PHONE = C.VIEWPORTS["phone"]
DESK = dict(viewport={"width": 1000, "height": 780}, device_scale_factor=2)

# scrollTop that puts the drawer section whose title matches `re` near the top.
SECTION_TOP_JS = """([re, pad]) => {
  const body = document.getElementById('jobDrawerBody');
  const rx = new RegExp(re, 'i');
  const t = [...body.querySelectorAll('.jd-sec-title, h3, h4, div, span')]
    .find(n => n.children.length <= 2 && rx.test((n.textContent || '').trim().slice(0, 60)));
  if (!t) return null;
  const top = t.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
  return Math.max(0, Math.round(top - pad));
}"""


async def drawer_scroll_to(page, rx, pad=14, ms=1100):
    top = await page.evaluate(SECTION_TOP_JS, [rx, pad])
    if top is None:
        raise RuntimeError(f"no drawer section /{rx}/")
    await smooth_scroll(page, "#jobDrawerBody", top, ms)


async def open_card(page, app_id):
    await page.click(f"#shortlisted-container .jobrow[data-app-id='{app_id}']", position={"x": 70, "y": 70})
    await page.wait_for_selector("#jobModalOverlay.open")
    await page.wait_for_function("""() => { const p = document.querySelector('#jobDrawerBody .match-panel-placeholder');
        return p && /fit for your resume/.test(p.textContent); }""", timeout=15000)


async def take_phone_match(page):
    await C.open_dashboard(page)
    await page.evaluate(TOUCH_JS)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)
    rec = Screencast(page, "phone_match")
    await rec.start()
    await page.wait_for_timeout(1000)
    top = await page.evaluate("""() => { const t = document.getElementById('pipe-tabs');
        return t.getBoundingClientRect().top + window.scrollY - 96; }""")
    await smooth_scroll(page, None, top, 1300)
    rec.mark("shortlist")
    await page.wait_for_timeout(2000)
    rec.mark("tap_lumen")
    await open_card(page, C.HERO)
    rec.mark("drawer")
    await page.wait_for_timeout(1500)
    # The report's own title, so the 94 and "Strong fit" sit under the header
    # (.jd-report's offsetTop is measured from another parent and overshoots).
    await drawer_scroll_to(page, "AI MATCH REPORT", pad=10)
    rec.mark("report")
    await page.wait_for_timeout(2800)
    await drawer_scroll_to(page, "^fit breakdown")
    rec.mark("fit")
    await page.wait_for_timeout(2800)
    await drawer_scroll_to(page, "^resume keyword match")
    rec.mark("keywords")
    await page.wait_for_timeout(2400)
    await rec.stop()
    return rec.save(OUT)


async def take_phone_gap(page):
    await C.open_dashboard(page)
    await page.evaluate(TOUCH_JS)
    await page.evaluate(f"""() => document.querySelector("#shortlisted-container .jobrow[data-app-id='{MERIDIAN}']")
        .scrollIntoView({{block: 'center'}})""")
    await page.wait_for_timeout(500)
    rec = Screencast(page, "phone_gap")
    await rec.start()
    await page.wait_for_timeout(900)
    rec.mark("tap_meridian")
    await open_card(page, MERIDIAN)
    rec.mark("drawer")
    await page.wait_for_timeout(1200)
    await drawer_scroll_to(page, "^resume keyword match")
    rec.mark("keywords")
    missing = await page.evaluate("""() => (document.getElementById('jobDrawerBody').innerText.match(
        /Missing from your resume[\\s\\S]{0,80}/i) || [''])[0]""")
    print("  gap shown:", " ".join(missing.split())[:100])
    await page.wait_for_timeout(3000)
    await rec.stop()
    return rec.save(OUT)


# Desktop takes only: the software compositor redraws the studio overlay's
# backdrop blur at 2x for every frame and the recording falls to ~6 fps (29 fps
# without it, measured). Only the dimmed board AROUND the modal changes; the
# reel shows the studio in close-up.
NO_BLUR_CSS = "*{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}"


async def take_desk_tailor(page):
    await C.open_dashboard(page)
    await page.add_style_tag(content=NO_BLUR_CSS)
    await page.evaluate(CURSOR_JS)
    await page.mouse.move(760, 600)
    rec = Screencast(page, "desk_tailor")
    await rec.start()
    # The studio as a run shows it while the model works.
    await page.evaluate(f"openTailorStudio({C.HERO}, 'Senior Backend Engineer', 'Lumen Data')")
    rec.mark("stages")
    await page.wait_for_timeout(2600)
    await page.evaluate("""async () => { const d = await _tsFetchDetails(); _tsRenderResult(d); }""")
    rec.mark("reveal")
    await page.wait_for_timeout(4800)          # the resume types in (55 ms a line) and the pane follows
    rec.mark("revealed")
    await page.wait_for_timeout(600)
    await smooth_scroll(page, "#ts-scroll", 0, 1300)
    rec.mark("top")
    await page.wait_for_timeout(2600)
    box = await page.locator("#ts-tab-cover").bounding_box()
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=28)
    await page.wait_for_timeout(250)
    rec.mark("cover_click")
    await page.mouse.down()
    await page.mouse.up()
    await page.wait_for_timeout(4800)          # the letter types in too
    rec.mark("cover_done")
    await smooth_scroll(page, "#ts-scroll", 0, 900)
    await page.mouse.move(850, 660, steps=20)
    await page.wait_for_timeout(2400)
    await rec.stop()
    return rec.save(OUT)


TAKES = {
    "phone_match": ("untailored", PHONE, take_phone_match),
    "phone_gap": ("untailored", PHONE, take_phone_gap),
    "desk_tailor": ("tailored", DESK, take_desk_tailor),
}


async def main(takes):
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as pw:
        # Without --force-device-scale-factor the screencast arrives at CSS size
        # (430x932) whatever the emulated scale: the compositor itself has to
        # draw at device resolution, so each scale gets its own browser.
        browsers = {}
        for name in takes:
            ph, opts, fn = TAKES[name]
            dsf = opts["device_scale_factor"]
            if dsf not in browsers:
                browsers[dsf] = await pw.chromium.launch(
                    headless=True, args=[f"--force-device-scale-factor={dsf}"])
            C.phase(ph)
            ctx = await browsers[dsf].new_context(**opts, service_workers="block", locale="en-US",
                                                  timezone_id="America/Chicago", color_scheme="light")
            await ctx.add_init_script(C.INIT)
            await ctx.route("**/*", C.router)
            page = await ctx.new_page()
            page.on("pageerror", lambda e, n=name: print(f"  [{n}] page error: {str(e)[:200]}"))
            await fn(page)
            await ctx.close()
        for b in browsers.values():
            await b.close()
    C.phase("untailored")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(TAKES)))
