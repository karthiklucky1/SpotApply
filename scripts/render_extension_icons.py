#!/usr/bin/env python3
"""Render the SpotApply extension icons (16/48/128 px) from the brand mark.

The three ``extension/icon*.png`` files are the ONLY copy of the mark that
Chrome shows — there is no source SVG for them and nothing in the extension
calls ``chrome.action.setIcon``, so the PNGs *are* the branding. They used to
be teal, baked in, while the app itself is blue (``--sage`` #0077C2), which is
why the browser tab and the extensions menu did not match the product.

This script regenerates them from the same geometry as ``app/static/favicon.svg``
(32-unit viewBox: rounded square, arrow, tail dot) in the brand blue, so the
favicon and the extension mark stay one mark in one colour.

Stdlib only, on purpose: PIL / cairosvg / ImageMagick are not installed here and
CI installs requirements MINUS the ML stack, so a rendering dependency would be
a new declared dep for a once-a-year asset job. Everything below is zlib+struct.

Usage:  python3 scripts/render_extension_icons.py [--check]

``--check`` re-decodes the committed PNGs and verifies size/format/colour
instead of writing, which is what a test or a reviewer wants.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# Brand blue, verbatim from the --sage-lite / --sage tokens in dashboard.html.
# Do NOT invent a shade here: these two are the same pair favicon.svg uses.
GRAD_FROM = (0x0E, 0xA5, 0xE9)   # --sage-lite #0EA5E9
GRAD_TO = (0x00, 0x77, 0xC2)     # --sage      #0077C2

# Geometry in the favicon's 32-unit viewBox, scaled per output size.
VIEWBOX = 32.0
CORNER_R = 8.0                   # rect rx="8"
ARROW_W = 2.5                    # stroke-width
DOT_C = (10.0, 16.0)             # circle cx/cy
DOT_R = 2.0
DOT_ALPHA = 0.85
# path d="M10 16h12M16 10l6 6-6 6" — the shaft plus the two chevron arms.
SEGMENTS = (
    ((10.0, 16.0), (22.0, 16.0)),
    ((16.0, 10.0), (22.0, 16.0)),
    ((22.0, 16.0), (16.0, 22.0)),
)

SIZES = (16, 48, 128)
SS = 4                           # supersampling factor per axis


def _dist_to_segment(px: float, py: float, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else ((px - ax) * dx + (py - ay) * dy) / den
    t = max(0.0, min(1.0, t))          # clamp = round line caps, for free
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _rounded_rect_inside(px: float, py: float, size: float, r: float) -> bool:
    """True when (px, py) is inside a size x size square with corner radius r."""
    if px < 0 or py < 0 or px > size or py > size:
        return False
    # Distance to the nearest corner circle centre, only in the corner boxes.
    cx = r if px < r else (size - r if px > size - r else px)
    cy = r if py < r else (size - r if py > size - r else py)
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5 <= r


def _render(size: int) -> bytes:
    """One RGBA buffer, supersampled SSxSS then box-filtered."""
    scale = size / VIEWBOX
    r = CORNER_R * scale
    half_w = (ARROW_W * scale) / 2.0
    dot_cx, dot_cy = DOT_C[0] * scale, DOT_C[1] * scale
    dot_r = DOT_R * scale
    segs = [((a[0] * scale, a[1] * scale), (b[0] * scale, b[1] * scale))
            for a, b in SEGMENTS]

    n = SS * SS
    out = bytearray()
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(SS):
                py = y + (sy + 0.5) / SS
                for sx in range(SS):
                    px = x + (sx + 0.5) / SS
                    if not _rounded_rect_inside(px, py, float(size), r):
                        continue
                    # 135deg gradient: top-left -> bottom-right.
                    t = (px + py) / (2.0 * size)
                    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    cr = GRAD_FROM[0] + (GRAD_TO[0] - GRAD_FROM[0]) * t
                    cg = GRAD_FROM[1] + (GRAD_TO[1] - GRAD_FROM[1]) * t
                    cb = GRAD_FROM[2] + (GRAD_TO[2] - GRAD_FROM[2]) * t
                    # Tail dot (white at 85%), then the arrow (solid white)
                    # painted over it, matching the SVG's paint order.
                    if ((px - dot_cx) ** 2 + (py - dot_cy) ** 2) ** 0.5 <= dot_r:
                        cr += (255 - cr) * DOT_ALPHA
                        cg += (255 - cg) * DOT_ALPHA
                        cb += (255 - cb) * DOT_ALPHA
                    if any(_dist_to_segment(px, py, a, b) <= half_w for a, b in segs):
                        cr = cg = cb = 255.0
                    acc_r += cr
                    acc_g += cg
                    acc_b += cb
                    acc_a += 255.0
            if acc_a == 0.0:
                row += b"\x00\x00\x00\x00"
                continue
            # Un-premultiply: colour is the mean over COVERED samples only, so
            # an edge pixel keeps its colour and only loses alpha. Averaging
            # over all n samples instead would darken every rounded corner.
            cov = acc_a / 255.0
            row += bytes((
                int(round(acc_r / cov)),
                int(round(acc_g / cov)),
                int(round(acc_b / cov)),
                int(round(acc_a / n)),
            ))
        out += b"\x00" + row          # filter byte 0 (None) per scanline
    return bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(size: int, raw: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def _decode(path: Path):
    """Minimal reader: returns (width, height, colortype, rows as RGBA bytes)."""
    blob = path.read_bytes()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{path}: not a PNG"
    pos, idat, hdr = 8, bytearray(), None
    while pos < len(blob):
        (ln,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        elif tag == b"IEND":
            break
        pos += 12 + ln
    w, h, depth, ctype = hdr[0], hdr[1], hdr[2], hdr[3]
    assert depth == 8 and ctype == 6, f"{path}: expected 8-bit RGBA, got {depth}/{ctype}"
    stride = w * 4
    data = zlib.decompress(bytes(idat))
    out, prev = bytearray(), bytearray(stride)
    p = 0
    for _ in range(h):
        f = data[p]
        line = bytearray(data[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif f == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out += line
        prev = line
    return w, h, ctype, bytes(out)


def check(root: Path) -> int:
    bad = 0
    for size in SIZES:
        path = root / "extension" / f"icon{size}.png"
        w, h, ctype, px = _decode(path)
        problems = []
        if (w, h) != (size, size):
            problems.append(f"size {w}x{h}, expected {size}x{size}")
        if px[3] != 0:
            problems.append("top-left corner is opaque (rounding lost)")
        mid = ((h // 2) * w + w // 2) * 4
        centre = tuple(px[mid:mid + 3])
        # Dead centre of the mark is the white arrow shaft. At 16px the shaft
        # is 1.25px wide, so the centre sample is a blend rather than pure
        # white — assert "clearly the arrow, not the background" instead.
        if min(centre) < 150:
            problems.append(f"centre pixel {centre} is too dark to be the arrow")
        # The gradient should read blue: blue channel dominates red at the
        # top-left inset, which is the whole point of this change.
        inset = ((h // 10) * w + w // 10) * 4
        rr, gg, bb = px[inset], px[inset + 1], px[inset + 2]
        if not (bb > rr and bb > 120):
            problems.append(f"inset pixel ({rr},{gg},{bb}) is not blue")
        print(f"{path.name}: {w}x{h} RGBA corner_alpha={px[3]} "
              f"inset=({rr},{gg},{bb}) " + ("OK" if not problems else "FAIL"))
        for p in problems:
            print(f"    - {p}")
            bad += 1
    return bad


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    if "--check" in sys.argv:
        return 1 if check(root) else 0
    for size in SIZES:
        path = root / "extension" / f"icon{size}.png"
        path.write_bytes(_png(size, _render(size)))
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 1 if check(root) else 0


if __name__ == "__main__":
    raise SystemExit(main())
