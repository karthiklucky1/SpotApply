"""One SSRF guard for every server-side fetch of a user-influenced URL.

The rule this module exists to enforce: **the server must never be talked into
requesting an address the caller could not reach themselves.** Cloud metadata
(169.254.169.254), localhost, private ranges and link-local addresses are all
reachable from inside the container and from nowhere else, which is exactly
what makes them worth attacking.

It lived only in `app/intelligence/job_check.py`, guarding the anonymous
job-check endpoint — while the AUTHENTICATED path (`/api/jobs/submit` stores an
arbitrary url, `/api/jobs/{id}/verify` then HEADs it with
`follow_redirects=True`) had no check at all. Being logged in is not a reason to
trust a URL: every friend invited to the app would have had that reach.

Two things are load-bearing:

* Resolve the host and check **every** address it returns, not just the first.
  A DNS name can answer with one public and one private address.
* Re-check **every redirect hop**. A public URL that 302s to
  `http://169.254.169.254/latest/meta-data/` defeats a check that only looked
  at the URL it was handed, which is why redirects are followed by hand here.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

MAX_REDIRECTS = 4


def is_public_host(host: str) -> bool:
    """True only when `host` resolves exclusively to public IP addresses.

    Fails CLOSED: an unresolvable name, an unparseable address or an empty host
    is not public. A name that resolves to several addresses is public only if
    every one of them is.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def is_fetchable_url(url: str) -> bool:
    """http(s) scheme AND a host that resolves only to public addresses."""
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    return is_public_host(p.hostname or "")


def guarded_request(client: httpx.Client, method: str, url: str,
                    max_redirects: int = MAX_REDIRECTS,
                    **kwargs) -> Tuple[Optional[httpx.Response], Optional[str]]:
    """Issue `method url`, following redirects BY HAND so each hop is checked.

    Returns (response, None) or (None, reason). `client` must be built with
    ``follow_redirects=False`` — passing a following client would hand the
    redirect chain back to httpx and skip the per-hop check entirely.
    """
    current = url
    history = []
    for _ in range(max_redirects):
        if not is_fetchable_url(current):
            return None, "blocked_host"
        try:
            r = client.request(method, current, follow_redirects=False, **kwargs)
        except Exception as e:
            return None, f"fetch_failed: {e}"
        location = r.headers.get("location")
        if r.status_code in (301, 302, 303, 307, 308) and location:
            history.append(r)
            current = str(httpx.URL(current).join(location))
            continue
        try:
            r.history = history
        except Exception:
            pass
        return r, None
    return None, "too_many_redirects"
