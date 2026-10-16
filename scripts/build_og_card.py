"""Regenerate app/static/og-card.png — the 1200x630 image Reddit, WhatsApp,
LinkedIn, X and Slack show when someone shares a SpotApply link.

The committed card was made by hand before the brand moved to blue and was
never redrawn: it still showed the old GREEN palette, spelled "résumé" (the
founder's rule is "resume" in everything a user sees) and claimed "1,000+
company boards" against 47,618 active on 2026-10-15. Reddit's link preview put
it in front of the launch post. It is now DRAWN from the same values the site
uses — the favicon mark, the landing palette, the vendored Outfit + Instrument
Serif fonts — so it cannot drift into a second brand again. Run this whenever
the brand, the headline or a claim on it changes:

    python -m scripts.build_og_card

Rendering uses the Chromium Playwright already ships (as build_favicon_ico.py
does). Every claim on the card must be one the site makes and can back up;
tests/test_brand_assets.py checks the size, the spelling and the palette.
Templates reference the file with a ``?v=`` suffix — bump it (OG_CARD_VERSION
in the test) when the picture changes, or link previews keep the cached one.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
FONTS = ROOT / "app" / "static" / "vendor" / "fonts"
SVG = ROOT / "app" / "static" / "favicon.svg"
OUT = ROOT / "app" / "static" / "og-card.png"
WIDTH, HEIGHT = 1200, 630

# The landing palette (templates/landing.html :root).
INK, INK_SOFT, MUTED = "#0C2A3E", "#21455C", "#5B7186"
ACCENT, ACCENT_700, ACCENT_TINT, ACCENT_LINE = "#0077C2", "#0369A1", "#E0F2FE", "#BAE0F8"

HEADLINE = 'Apply only where you <span class="serif">actually fit.</span>'
SUBLINE = ("Watches 45,000+ company career pages, matches new jobs to your resume, "
           "tailors it for each one and fills the application.")
PILLS = ("Free to use", "Jobs matched to your resume", "You always click Submit")
HOST = "app.spotapply.ai"


def card_html() -> str:
    def face(family: str, weight: int, style: str, file: str) -> str:
        return (f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:{style};"
                f"src:url('{(FONTS / file).as_uri()}') format('woff2');}}")
    fonts = "".join([
        face("Outfit", 400, "normal", "outfit-latin-400-normal.woff2"),
        face("Outfit", 700, "normal", "outfit-latin-700-normal.woff2"),
        face("Outfit", 800, "normal", "outfit-latin-800-normal.woff2"),
        face("Instrument Serif", 400, "italic", "instrument-serif-latin-400-italic.woff2"),
    ])
    mark = SVG.read_text().replace("<svg ", '<svg width="76" height="76" ', 1)
    pills = "".join(f'<span class="pill">{p}</span>' for p in PILLS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{fonts}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:{WIDTH}px;height:{HEIGHT}px;font-family:'Outfit',sans-serif;color:{INK};
  background:radial-gradient(900px 520px at 88% 8%, {ACCENT_TINT} 0%, rgba(224,242,254,0) 70%),
             linear-gradient(180deg,#FFFFFF 0%,#F5FAFF 100%);
  padding:64px 72px;position:relative;overflow:hidden}}
.brand{{display:flex;align-items:center;gap:18px}}
.brand span{{font-weight:800;font-size:46px;letter-spacing:-0.02em;color:{ACCENT}}}
h1{{margin-top:44px;font-weight:800;font-size:78px;line-height:1.02;letter-spacing:-0.035em;max-width:15ch}}
.serif{{font-family:'Instrument Serif',Georgia,serif;font-style:italic;font-weight:400;
  letter-spacing:-0.012em;color:{ACCENT}}}
p{{margin-top:26px;font-size:29px;line-height:1.38;color:{INK_SOFT};max-width:30ch}}
.pills{{position:absolute;left:72px;bottom:58px;display:flex;gap:14px}}
.pill{{font-weight:700;font-size:22px;color:{ACCENT_700};background:{ACCENT_TINT};
  border:2px solid {ACCENT_LINE};border-radius:999px;padding:10px 22px}}
.host{{position:absolute;right:72px;top:80px;font-size:24px;font-weight:700;color:{MUTED}}}
</style></head><body>
<div class="brand">{mark}<span>SpotApply</span></div>
<div class="host">{HOST}</div>
<h1>{HEADLINE}</h1>
<p>{SUBLINE}</p>
<div class="pills">{pills}</div>
</body></html>"""


def render() -> bytes:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(args=["--no-sandbox"])
        except Exception:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # A file:// page, so the file:// font URLs load (about:blank may not).
            html = pathlib.Path(tmp) / "card.html"
            html.write_text(card_html(), encoding="utf-8")
            try:
                page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT},
                                        device_scale_factor=1)
                page.goto(html.as_uri())
                page.evaluate("document.fonts.ready")
                page.wait_for_timeout(300)
                missing = page.evaluate(
                    "['800 40px Outfit','400 40px Outfit','italic 400 40px \"Instrument Serif\"']"
                    ".filter(f => !document.fonts.check(f))")
                if missing:
                    raise RuntimeError(f"fonts did not load: {missing}")
                return page.screenshot(type="png",
                                       clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
            finally:
                browser.close()


def main() -> None:
    png = render()
    try:  # lossless re-encode keeps the file small for slow mobile previews
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(png)).save(buf, format="PNG", optimize=True)
        png = buf.getvalue() if len(buf.getvalue()) < len(png) else png
    except Exception:
        pass
    OUT.write_bytes(png)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(png) // 1024} KB, {WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    main()
