"""Client for the browser service, with a local-Playwright fallback.

Two implementations behind one API:

  * REMOTE — when ``BROWSER_SERVICE_URL`` is set, page rendering happens in the
    separate browser service (``browser-service/``). Chromium's ~400MB never
    enters this container, and browser capacity scales independently of the API.
  * LOCAL  — otherwise, launch Chromium in-process behind the
    ``app.common.browser.browser_slot`` gate, exactly as before.

Callers do not branch on which one is active. That is the point: this can be
switched on in production by setting one env var, and switched off again by
unsetting it, with no code change and no redeploy of application logic.

FALLBACK POLICY
---------------
When the remote call fails (service down, deploying, network blip), the default
is to fall back to a local render (``BROWSER_SERVICE_FALLBACK_LOCAL=1``). A
browser-service outage should degrade discovery to "slower and heavier", not
"broken". Set it to 0 on a container sized WITHOUT room for a local Chromium —
there, falling back is the OOM we were trying to avoid, and a failed render is
strictly better.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def remote_enabled() -> bool:
    return bool((getattr(settings, "browser_service_url", "") or "").strip())


def _endpoint(path: str) -> str:
    return (settings.browser_service_url or "").rstrip("/") + path


def _headers() -> dict:
    token = (getattr(settings, "browser_service_token", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _remote(path: str, payload: dict, timeout_ms: int) -> dict | None:
    """POST to the browser service. Returns the payload, or None on failure.

    None means "the service did not answer" — the caller decides whether to fall
    back. A rendered-but-failed page (dead link, bot wall) comes back as a real
    payload with ok=False, which is NOT a fallback case: retrying that locally
    would just fail again, slower and with a browser in this container.
    """
    import httpx
    # Client budget exceeds the render budget so we surface the service's own
    # structured error rather than a client-side timeout that hides it.
    budget = timeout_ms / 1000.0 + 15.0
    try:
        async with httpx.AsyncClient(timeout=budget) as client:
            resp = await client.post(_endpoint(path), json=payload, headers=_headers())
            if resp.status_code == 401:
                log.error("browser service rejected our token (401) — check "
                          "BROWSER_SERVICE_TOKEN matches on both services")
                return None
            if resp.status_code >= 400:
                log.warning("browser service %s returned %d: %s",
                            path, resp.status_code, resp.text[:300])
                return None
            return resp.json()
    except Exception as exc:
        log.warning("browser service %s unreachable (%s: %s)", path, type(exc).__name__, exc)
        return None


def _should_fall_back() -> bool:
    return bool(getattr(settings, "browser_service_fallback_local", True))


# ── local implementation (unchanged behaviour, memory-gated) ─────────────────

async def _local_render(url: str, *, wait_until: str, timeout_ms: int,
                        settle_ms: int, user_agent: str,
                        extract: str) -> Tuple[str, List[str]]:
    """Render in-process. Holds a browser_slot for the whole render."""
    from playwright.async_api import async_playwright
    from app.common.browser import browser_slot

    text, links = "", []
    async with browser_slot(f"render:{extract}"), async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(user_agent=user_agent or DEFAULT_UA)
            page = await context.new_page()
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            if extract in ("text", "both"):
                text = (await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")) or ""
            if extract in ("links", "both"):
                links = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('a'))"
                    ".map(a => a.href).filter(h => h && h.startsWith('http'))"
                ) or []
        finally:
            await browser.close()
    return text, links


# ── public API ───────────────────────────────────────────────────────────────

async def render_page(url: str, *, wait_until: str = "domcontentloaded",
                      timeout_ms: int = 30000, settle_ms: int = 2000,
                      user_agent: str = "", extract: str = "text") -> Tuple[str, List[str]]:
    """Render ``url``; return ``(visible_text, links)``.

    Raises whatever the underlying renderer raises when no route succeeds, so
    existing callers keep their current error handling.
    """
    if remote_enabled():
        payload = await _remote("/render", {
            "url": url,
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "settle_ms": settle_ms,
            "user_agent": user_agent,
            "extract": extract,
        }, timeout_ms)

        if payload is not None:
            if payload.get("ok"):
                return payload.get("text", "") or "", payload.get("links", []) or []
            # The service rendered and the PAGE failed — authoritative, not a
            # transport problem. Retrying locally cannot do better.
            raise RuntimeError(
                f"browser service could not render {url}: {payload.get('error') or 'unknown error'}")

        if not _should_fall_back():
            raise RuntimeError(
                f"browser service unavailable and local fallback is disabled "
                f"(BROWSER_SERVICE_FALLBACK_LOCAL=0); could not render {url}")
        log.warning("browser service unavailable — falling back to a LOCAL browser "
                    "for %s (this costs ~400MB in the web container)", url)

    return await _local_render(url, wait_until=wait_until, timeout_ms=timeout_ms,
                               settle_ms=settle_ms, user_agent=user_agent, extract=extract)


async def render_text(url: str, **kw) -> str:
    """Visible text of ``url``."""
    text, _ = await render_page(url, extract="text", **kw)
    return text


async def render_links(url: str, **kw) -> List[str]:
    """Every outbound http(s) link on ``url``."""
    _, links = await render_page(url, extract="links", **kw)
    return links


async def search_links(query: str, *, engine: str = "google",
                       timeout_ms: int = 30000, settle_ms: int = 2000,
                       user_agent: str = "") -> List[str]:
    """Run a search-engine query; return the result links.

    Returns ``[]`` rather than raising: every caller treats an empty result set
    as "this discovery source found nothing this pass", which is the correct
    behaviour for a bot-walled or unreachable search engine too.
    """
    if remote_enabled():
        payload = await _remote("/search", {
            "query": query,
            "engine": engine,
            "timeout_ms": timeout_ms,
            "settle_ms": settle_ms,
            "user_agent": user_agent,
        }, timeout_ms)
        if payload is not None:
            if payload.get("ok"):
                return payload.get("links", []) or []
            log.warning("browser service search failed for %r: %s",
                        query, payload.get("error"))
            return []
        if not _should_fall_back():
            log.warning("browser service unavailable, local fallback disabled — "
                        "skipping search for %r", query)
            return []
        log.warning("browser service unavailable — falling back to a LOCAL browser "
                    "for search %r", query)

    import urllib.parse
    engines = {
        "google": "https://www.google.com/search?q=",
        "bing": "https://www.bing.com/search?q=",
        "duckduckgo": "https://duckduckgo.com/?q=",
    }
    url = engines.get(engine, engines["google"]) + urllib.parse.quote_plus(query)
    try:
        _, links = await _local_render(url, wait_until="domcontentloaded",
                                       timeout_ms=timeout_ms, settle_ms=settle_ms,
                                       user_agent=user_agent, extract="links")
        return links
    except Exception as exc:
        log.warning("local search render failed for %r: %s", query, exc)
        return []
