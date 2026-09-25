"""Re-encode the landing screenshots to WebP beside their PNGs.

MEASURED on this checkout (7 cold-context runs, Chromium, loopback — see
docs/VERIFICATION_PHASE_10_11.md for the method): the hero PNG was 305KB of a
362KB landing page — **86% of everything the page transfers on a first visit**.
The same pixels at WebP q=0.86 cost 174KB, and the board's card text is still
legible at full resolution. Page total went 362KB -> 227KB, a 37% reduction.

The PNGs stay. The template lists WebP first and the PNG as the fallback
`<source>`, so a browser that cannot decode WebP still gets the original — this
script adds a format, it does not replace one.

Rendering uses the Chromium Playwright already ships for the autofill path, so
there is no new dependency and no imagemagick step to keep in sync.

    python -m scripts.build_webp_shots
"""
from __future__ import annotations

import base64
import pathlib
import sys

QUALITY = 0.86
SHOTS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "shots"


def main() -> int:
    from playwright.sync_api import sync_playwright

    pngs = sorted(SHOTS.glob("*.png"))
    if not pngs:
        print(f"no PNGs in {SHOTS}", file=sys.stderr)
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            for src in pngs:
                raw = base64.b64encode(src.read_bytes()).decode()
                result = page.evaluate(
                    """async ({dataUrl, quality}) => {
                        const img = new Image();
                        await new Promise((ok, no) => {
                            img.onload = ok; img.onerror = no; img.src = dataUrl;
                        });
                        const c = document.createElement('canvas');
                        c.width = img.naturalWidth;
                        c.height = img.naturalHeight;
                        c.getContext('2d').drawImage(img, 0, 0);
                        return {w: img.naturalWidth, h: img.naturalHeight,
                                webp: c.toDataURL('image/webp', quality)};
                    }""",
                    {"dataUrl": f"data:image/png;base64,{raw}", "quality": QUALITY})
                if not result["webp"].startswith("data:image/webp"):
                    print(f"  {src.name}: browser declined to encode WebP", file=sys.stderr)
                    return 1
                data = base64.b64decode(result["webp"].split(",", 1)[1])
                dst = src.with_suffix(".webp")
                dst.write_bytes(data)
                before = src.stat().st_size
                print("  %-30s %6.0fKB -> %5.0fKB  (%d%% smaller)  %dx%d" % (
                    src.name, before / 1024, len(data) / 1024,
                    round(100 * (1 - len(data) / before)), result["w"], result["h"]))
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
