"""Regenerate app/static/favicon.ico FROM app/static/favicon.svg.

`/favicon.ico` used to return the SVG file with an SVG content type — SVG bytes
at a `.ico` URL. Modern Chrome often renders it anyway; the clients that trust
the extension over the content type (older Chrome, several crawlers, bookmark
and tab-restore surfaces) get an image they cannot decode and show a blank or
generic icon. A developer whose browser already cached the SVG from a page visit
does not reproduce it, which is what a "no logo in Chrome" report looks like.

The .ico is DERIVED from the SVG so the two cannot drift into different marks.
Run this whenever favicon.svg changes:

    python -m scripts.build_favicon_ico

Rendering uses the Chromium that Playwright already ships for the autofill path —
no new dependency, and the raster is what a browser actually draws rather than an
approximation of the brand. `tests/test_brand_assets.py` fails if the committed
.ico stops being a valid multi-size ICO, or stops matching the SVG's dimensions.
"""
from __future__ import annotations

import pathlib
import struct
import sys

SIZES = (16, 32, 48)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

ROOT = pathlib.Path(__file__).resolve().parents[1]
SVG = ROOT / "app" / "static" / "favicon.svg"
ICO = ROOT / "app" / "static" / "favicon.ico"


def _render(svg_text: str) -> dict[int, bytes]:
    """One transparent PNG per size, drawn by Chromium."""
    from playwright.sync_api import sync_playwright

    out: dict[int, bytes] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            for size in SIZES:
                page = browser.new_page(viewport={"width": size, "height": size},
                                        device_scale_factor=1)
                page.set_content(
                    '<body style="margin:0;background:transparent">'
                    '<div style="width:%dpx;height:%dpx;line-height:0">%s</div>'
                    "</body>" % (size, size, svg_text))
                page.wait_for_timeout(150)
                out[size] = page.screenshot(omit_background=True, type="png")
                page.close()
        finally:
            browser.close()
    return out


def build_ico(frames: dict[int, bytes]) -> bytes:
    """PNG-in-ICO. Chrome, Edge, Firefox and Safari all accept PNG inside ICO,
    and it keeps the file small enough to serve uncompressed."""
    sizes = sorted(frames)
    blob = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, payloads = b"", b""
    for s in sizes:
        data = frames[s]
        if data[:8] != PNG_MAGIC:
            raise ValueError(f"frame {s} is not a PNG")
        # width/height are single bytes; 0 means 256. Nothing here is 256.
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset)
        payloads += data
        offset += len(data)
    return blob + entries + payloads


def main() -> int:
    if not SVG.exists():
        print(f"missing {SVG}", file=sys.stderr)
        return 1
    frames = _render(SVG.read_text())
    ico = build_ico(frames)
    ICO.write_bytes(ico)
    print(f"wrote {ICO.relative_to(ROOT)}: {len(ico)} bytes, sizes={sorted(frames)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
