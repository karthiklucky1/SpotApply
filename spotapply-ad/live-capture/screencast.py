"""Record a page as a real screen recording, at device resolution.

Playwright's own recordVideo is a ~1 Mbit/s VP8 stream that blurs UI text.
This asks Chromium for its compositor frames instead (CDP Page.startScreencast,
JPEG q92 at the page's full device-pixel size), keeps each frame's own
timestamp, and rebuilds a constant-frame-rate H.264 file from them: a frame
the page did not change is held, exactly as a screen recorder would.

    rec = Screencast(page, "board")
    await rec.start(); ...; rec.mark("drawer"); ...; await rec.stop()
    rec.save(out_dir)   # -> board.mp4 + board.json (marks, seconds from frame 0)

Overlays for the viewer (a touch ripple on phone, a cursor on desktop) are
plain DOM injected by `TOUCH_JS` / `CURSOR_JS`; they draw only what the
script's own pointer is doing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time

FPS = 30

# A finger tap, as phone screen recordings show it.
TOUCH_JS = """() => {
  if (window.__touchViz) return; window.__touchViz = true;
  const css = document.createElement('style');
  css.textContent = `.__tap{position:fixed;z-index:2147483647;pointer-events:none;width:64px;height:64px;
    margin:-32px 0 0 -32px;border-radius:50%;background:rgba(255,255,255,.55);
    box-shadow:0 0 0 2px rgba(12,42,62,.35);animation:__tap .55s ease-out forwards}
    @keyframes __tap{0%{transform:scale(.4);opacity:1}100%{transform:scale(1.25);opacity:0}}`;
  document.documentElement.appendChild(css);
  addEventListener('pointerdown', e => {
    const d = document.createElement('div'); d.className = '__tap';
    d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px';
    document.documentElement.appendChild(d); setTimeout(() => d.remove(), 700);
  }, true);
}"""

# A desktop pointer that follows the script's mouse (a screencast has none).
CURSOR_JS = """() => {
  if (window.__cursorViz) return; window.__cursorViz = true;
  const c = document.createElement('div');
  c.innerHTML = `<svg width="26" height="34" viewBox="0 0 26 34"><path d="M2 2 L2 27 L8.5 21 L13 31.5 L17.5 29.5 L13 19.5 L22 19.5 Z"
    fill="#0C2A3E" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></svg>`;
  Object.assign(c.style, {position:'fixed', left:'-40px', top:'-40px', zIndex:'2147483647',
    pointerEvents:'none', transition:'transform .08s'});
  document.documentElement.appendChild(c);
  addEventListener('mousemove', e => { c.style.left = (e.clientX - 2) + 'px'; c.style.top = (e.clientY - 2) + 'px'; }, true);
  addEventListener('mousedown', () => { c.style.transform = 'scale(.86)'; }, true);
  addEventListener('mouseup', () => { c.style.transform = ''; }, true);
}"""

# Scroll an element (or the window) with an eased, human-paced motion.
SMOOTH_SCROLL_JS = """async ([sel, top, ms]) => {
  const el = sel ? document.querySelector(sel) : document.scrollingElement;
  const from = el.scrollTop, t0 = performance.now();
  const ease = t => t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2;
  await new Promise(done => { const step = now => {
    const t = Math.min(1, (now - t0) / ms); el.scrollTop = from + (top - from) * ease(t);
    t < 1 ? requestAnimationFrame(step) : done(); }; requestAnimationFrame(step); });
  return el.scrollTop;
}"""


async def smooth_scroll(page, selector, top, ms=1200):
    return await page.evaluate(SMOOTH_SCROLL_JS, [selector, top, ms])


class Screencast:
    def __init__(self, page, name: str):
        self.page, self.name = page, name
        self.frames: list[tuple[float, bytes]] = []
        self.marks: list[tuple[str, float]] = []
        self.cdp = None
        self.t_stop = 0.0

    async def start(self, quality: int = 92):
        vp = self.page.viewport_size
        dpr = await self.page.evaluate("devicePixelRatio")
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": quality, "everyNthFrame": 1,
            "maxWidth": int(vp["width"] * dpr), "maxHeight": int(vp["height"] * dpr)})
        # Nudge one compositor frame so the recording starts on a picture.
        await self.page.evaluate("document.documentElement.style.outline = '0px solid transparent'")
        await self.page.wait_for_timeout(250)

    def _on_frame(self, p):
        self.frames.append((float(p["metadata"]["timestamp"]), base64.b64decode(p["data"])))
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}))

    def mark(self, label: str):
        self.marks.append((label, time.time()))

    async def stop(self, tail_ms: int = 300):
        await self.page.wait_for_timeout(tail_ms)
        # A page that stops changing stops producing frames: the recording runs
        # to the moment it was stopped, holding the last picture, as a screen
        # recorder would.
        self.t_stop = time.time()
        await self.cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.2)

    def save(self, out_dir: str) -> dict:
        """Constant-rate H.264 + a JSON of marks (seconds from the first frame)."""
        if not self.frames:
            raise RuntimeError(f"{self.name}: no frames captured")
        os.makedirs(out_dir, exist_ok=True)
        frames = sorted(self.frames, key=lambda f: f[0])
        t0, t_end = frames[0][0], max(frames[-1][0], self.t_stop)
        n = int((t_end - t0) * FPS) + 1
        tmp = tempfile.mkdtemp(prefix=f"sc_{self.name}_")
        try:
            src = []
            for i, (_, data) in enumerate(frames):
                path = os.path.join(tmp, f"src{i:05d}.jpg")
                with open(path, "wb") as fh:
                    fh.write(data)
                src.append(path)
            j = 0
            for k in range(n):
                t = t0 + k / FPS
                while j + 1 < len(frames) and frames[j + 1][0] <= t:
                    j += 1
                os.symlink(src[j], os.path.join(tmp, f"out{k:05d}.jpg"))
            mp4 = os.path.join(out_dir, f"{self.name}.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i",
                            os.path.join(tmp, "out%05d.jpg"),
                            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2:color=white,format=yuv420p",
                            "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-g", str(FPS),
                            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                            "-movflags", "+faststart", mp4], check=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        meta = {"name": self.name, "fps": FPS, "frames": n, "seconds": round(n / FPS, 3),
                "source_frames": len(frames),
                "marks": {label: round(ts - t0, 3) for label, ts in self.marks}}
        with open(os.path.join(out_dir, f"{self.name}.json"), "w") as fh:
            json.dump(meta, fh, indent=1)
        print(f"  {self.name}: {meta['seconds']}s, {len(frames)} source frames, marks {meta['marks']}")
        return meta
