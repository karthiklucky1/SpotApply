"""SpotApply browser service — headless Chromium behind a small HTTP API.

WHY THIS EXISTS
---------------
The main app is one process holding FastAPI + torch + sentence-transformers +
FAISS + five background lanes. Every headless Chromium it launched was a
~300-500MB child process charged to that same container's memory limit, and
launching them unbounded is what OOM-killed production. `app/common/browser.py`
capped the damage by serializing launches; this service removes the cost from
the web container entirely and lets browser capacity scale on its own.

WHAT IT REPLACES
----------------
The three STATELESS render/search operations that run on the background lanes,
unattended, for every tenant:

  * app/discovery/extractor.py       scrape_job_page(url) -> page text
  * app/discovery/google_search.py   _search_google_playwright(q) -> links
  * app/discovery/sources/search_engine.py  _query_playwright(q) -> links

It deliberately does NOT take over autofill or form preview. Those are stateful,
long-lived, interactive sessions (CAPTCHA hand-off, pending questions, a human
reviewing before Submit), and server-side autofill is founder-only today
(`autofill_multi_user_enabled=False`) while every other tenant autofills through
the MV3 Chrome extension in their own browser. See README.md.

DESIGN NOTES
------------
* ONE long-lived browser, a fresh BrowserContext per request. Launch-per-request
  costs ~1-2s of startup and churns hundreds of MB every time; a persistent
  browser with disposable contexts gives the same isolation for a fraction of
  the cost.
* Concurrency is bounded by a semaphore (BROWSER_CONCURRENCY). This is a memory
  budget, not a throughput knob — each concurrent context is real RAM.
* The browser is recycled every RECYCLE_AFTER requests. Long-lived Chromium
  leaks; recycling is cheaper than being killed.
* Auth + SSRF defence are NOT optional. An open "fetch any URL and tell me what
  it says" endpoint is a proxy into whatever private network it can reach.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("browser-service")

# ── configuration ────────────────────────────────────────────────────────────

#: Shared secret. Callers send it as `Authorization: Bearer <token>`. Empty
#: DISABLES auth, which is only ever appropriate on a private network — the
#: service logs a loud warning at startup in that case.
AUTH_TOKEN = os.environ.get("BROWSER_SERVICE_TOKEN", "").strip()

#: Concurrent browser contexts. Each is real memory; size it to the container.
CONCURRENCY = max(1, int(os.environ.get("BROWSER_CONCURRENCY", "2")))

#: Recycle the browser after AT LEAST this many requests (leak defence). The
#: check runs when a request arrives and only fires while nothing is in flight,
#: so under sustained concurrency `since_recycle` in /health legitimately drifts
#: past this number until the first quiet moment. 0 = never recycle.
RECYCLE_AFTER = int(os.environ.get("BROWSER_RECYCLE_AFTER", "200"))

#: Hard ceiling on any single navigation, regardless of what the caller asks for.
MAX_TIMEOUT_MS = int(os.environ.get("BROWSER_MAX_TIMEOUT_MS", "60000"))

#: Cap on returned page text. Callers feed this to an LLM; unbounded text is
#: both a memory risk here and a cost risk there.
MAX_TEXT_CHARS = int(os.environ.get("BROWSER_MAX_TEXT_CHARS", "200000"))

#: SSRF guard. When on (default), refuse URLs resolving to private/loopback/
#: link-local addresses — the service must not become a tunnel into the VPC.
BLOCK_PRIVATE = os.environ.get("BROWSER_BLOCK_PRIVATE_IPS", "1") not in ("0", "false", "False")

#: Explicit Chromium binary. Empty = whatever `playwright install` put in place
#: (the normal path in the Docker image). Set this when the host already has a
#: Chromium whose build revision differs from the Playwright library's expected
#: one, which otherwise fails the launch with "please run playwright install".
EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LAUNCH_ARGS = [
    "--no-sandbox",
    # Chromium defaults /dev/shm to 64MB in containers and crashes on big pages;
    # this makes it use /tmp instead. Without it, renders fail semi-randomly.
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--mute-audio",
]


# ── memory telemetry (self-contained — this service must not import the app) ──

def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as fh:
            raw = fh.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def memory_snapshot() -> dict:
    rss = None
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) / 1024.0
                    break
    except (OSError, IndexError, ValueError):
        pass
    usage = _read_int("/sys/fs/cgroup/memory.current")
    limit = _read_int("/sys/fs/cgroup/memory.max")
    usage_mb = usage / 1048576.0 if usage is not None else None
    limit_mb = limit / 1048576.0 if limit and limit < 2 ** 62 else None
    pct = round(usage_mb / limit_mb * 100, 1) if (usage_mb and limit_mb) else None
    return {
        "rss_mb": round(rss, 1) if rss is not None else None,
        "container_mb": round(usage_mb, 1) if usage_mb is not None else None,
        "limit_mb": round(limit_mb, 1) if limit_mb is not None else None,
        "used_pct": pct,
    }


# ── SSRF guard ───────────────────────────────────────────────────────────────

def validate_url(raw: str) -> str:
    """Accept only http(s) URLs that do not resolve into private address space.

    This service will fetch whatever it is told to fetch and hand back the
    response body. Without this check, anyone who can reach it can read internal
    metadata endpoints, admin panels, and databases through it.
    """
    try:
        parsed = urlparse(raw)
    except Exception:
        raise HTTPException(400, "Malformed URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, f"Unsupported scheme {parsed.scheme!r} — http/https only")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "URL has no host")
    if not BLOCK_PRIVATE:
        return raw
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"Cannot resolve host {host!r}")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(
                403, f"Refusing to fetch {host!r}: resolves to non-public address {addr}")
    return raw


# ── browser pool ─────────────────────────────────────────────────────────────

class BrowserPool:
    """One Chromium, disposable contexts, bounded concurrency, periodic recycle."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(CONCURRENCY)
        self._served = 0
        self._since_recycle = 0
        self._in_flight = 0
        self._launched_at: float | None = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        await self._launch()

    async def _launch(self) -> None:
        kwargs = {"headless": True, "args": LAUNCH_ARGS}
        if EXECUTABLE_PATH:
            kwargs["executable_path"] = EXECUTABLE_PATH
        self._browser = await self._pw.chromium.launch(**kwargs)
        self._since_recycle = 0
        self._launched_at = time.time()
        log.info("Chromium launched (version=%s, concurrency=%d, recycle_after=%d) %s",
                 self._browser.version, CONCURRENCY, RECYCLE_AFTER, memory_snapshot())

    async def stop(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _healthy_browser(self):
        """Return a live browser, relaunching if it died or is due for recycling.

        A crashed Chromium must not turn into a permanently failing service:
        `is_connected()` is false after a crash and we simply start a new one.
        """
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                log.warning("Chromium not connected — relaunching")
                await self._launch()
            elif RECYCLE_AFTER and self._since_recycle >= RECYCLE_AFTER and self._in_flight == 0:
                log.info("Recycling Chromium after %d requests %s",
                         self._since_recycle, memory_snapshot())
                try:
                    await self._browser.close()
                except Exception:
                    log.debug("close during recycle failed (ignored)", exc_info=True)
                await self._launch()
            return self._browser

    @asynccontextmanager
    async def page(self, user_agent: str, timeout_ms: int):
        """A fresh isolated context+page, torn down on exit no matter what."""
        async with self._sem:
            browser = await self._healthy_browser()
            self._in_flight += 1
            context = None
            try:
                context = await browser.new_context(
                    user_agent=user_agent or DEFAULT_UA,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                    java_script_enabled=True,
                )
                context.set_default_timeout(timeout_ms)
                page = await context.new_page()
                yield page
            finally:
                self._in_flight -= 1
                self._served += 1
                self._since_recycle += 1
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        log.debug("context close failed (ignored)", exc_info=True)

    def stats(self) -> dict:
        return {
            "served": self._served,
            "since_recycle": self._since_recycle,
            "in_flight": self._in_flight,
            "concurrency": CONCURRENCY,
            "connected": bool(self._browser and self._browser.is_connected()),
            "uptime_s": round(time.time() - self._launched_at, 1) if self._launched_at else None,
        }


pool = BrowserPool()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not AUTH_TOKEN:
        log.warning("BROWSER_SERVICE_TOKEN is empty — the service is UNAUTHENTICATED. "
                    "Only acceptable on a private network; anyone who can reach this "
                    "can render arbitrary URLs through it.")
    await pool.start()
    yield
    await pool.stop()


app = FastAPI(title="SpotApply Browser Service", lifespan=lifespan)


def require_auth(authorization: Optional[str]) -> None:
    if not AUTH_TOKEN:
        return
    expected = f"Bearer {AUTH_TOKEN}"
    # Constant-time compare — this is a bearer secret on a public endpoint.
    import hmac
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Invalid or missing bearer token")


# ── API ──────────────────────────────────────────────────────────────────────

class RenderRequest(BaseModel):
    url: str
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "domcontentloaded"
    timeout_ms: int = Field(30000, ge=1000)
    settle_ms: int = Field(2000, ge=0, le=15000, description="Extra wait for JS hydration after navigation")
    user_agent: str = ""
    extract: Literal["text", "links", "both"] = "text"


class RenderResponse(BaseModel):
    ok: bool
    status: Optional[int] = None
    final_url: str = ""
    title: str = ""
    text: str = ""
    links: list[str] = []
    error: str = ""
    elapsed_ms: int = 0


@app.post("/render", response_model=RenderResponse)
async def render(req: RenderRequest, authorization: str = Header(default=None)) -> RenderResponse:
    """Render a URL and return its visible text and/or its outbound links."""
    require_auth(authorization)
    url = validate_url(req.url)
    timeout = min(req.timeout_ms, MAX_TIMEOUT_MS)
    started = time.monotonic()

    try:
        async with pool.page(req.user_agent, timeout) as page:
            resp = await page.goto(url, wait_until=req.wait_until, timeout=timeout)
            if req.settle_ms:
                await page.wait_for_timeout(req.settle_ms)

            text = ""
            links: list[str] = []
            if req.extract in ("text", "both"):
                text = (await page.evaluate("() => document.body ? document.body.innerText : ''")) or ""
                if len(text) > MAX_TEXT_CHARS:
                    text = text[:MAX_TEXT_CHARS]
            if req.extract in ("links", "both"):
                links = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('a'))"
                    ".map(a => a.href).filter(h => h && h.startsWith('http'))"
                ) or []

            return RenderResponse(
                ok=True,
                status=resp.status if resp else None,
                final_url=page.url,
                title=(await page.title()) or "",
                text=text,
                links=links,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
    except HTTPException:
        raise
    except Exception as exc:
        # A render failure is normal operation (dead link, bot wall, timeout) —
        # report it as data so the caller can fall back, not as a 500.
        log.warning("render failed for %s: %s: %s", url, type(exc).__name__, exc)
        return RenderResponse(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class SearchRequest(BaseModel):
    query: str
    engine: Literal["google", "bing", "duckduckgo"] = "google"
    timeout_ms: int = Field(30000, ge=1000)
    settle_ms: int = Field(2000, ge=0, le=15000)
    user_agent: str = ""


_ENGINES = {
    "google": "https://www.google.com/search?q=",
    "bing": "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
}


@app.post("/search", response_model=RenderResponse)
async def search(req: SearchRequest, authorization: str = Header(default=None)) -> RenderResponse:
    """Run a search-engine query and return the result links."""
    require_auth(authorization)
    import urllib.parse
    url = _ENGINES[req.engine] + urllib.parse.quote_plus(req.query)
    return await render(
        RenderRequest(
            url=url,
            wait_until="domcontentloaded",
            timeout_ms=req.timeout_ms,
            settle_ms=req.settle_ms,
            user_agent=req.user_agent,
            extract="links",
        ),
        authorization=authorization,
    )


@app.get("/health")
async def health() -> dict:
    """Liveness + the numbers that matter for sizing this container."""
    return {"ok": True, "browser": pool.stats(), "memory": memory_snapshot()}
