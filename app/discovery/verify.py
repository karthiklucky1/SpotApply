"""Link-liveness check for job postings.

A cheap HEAD request that catches the two most common "dead posting" shapes:
the link 404/410s, or it 200s only after being redirected to the company's
general /careers page. Network failures are treated as ALIVE — a timeout is
not evidence the role is gone, and over-closing loses real opportunities.

Used by POST /api/jobs/{id}/verify (click-time check from the dashboard) and,
when ``settings.verify_links_on_shortlist`` is on, by the matching pipeline
before a non-direct-ATS job is shortlisted (direct ATS boards don't need it —
mark_ghost_jobs closes their vanished postings at scrape time).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from app.common.ssrf import guarded_request

log = logging.getLogger(__name__)


def check_job_alive(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    """Return (alive, reason). ``reason`` is set only when dead.

    SSRF-guarded. The URL comes from a job row, and a job row can come from
    `POST /api/jobs/submit` — i.e. from any logged-in user. This used to HEAD it
    with `follow_redirects=True` and no host check, so an authenticated caller
    could point the server at cloud metadata or any internal service and read
    alive/dead back out of the response. Being signed in is not a reason to
    trust a URL. Redirects are followed by hand so every hop is re-checked
    (`app/common/ssrf.py`); a blocked host reports ALIVE, because "we refused to
    fetch it" is not evidence the posting is gone.
    """
    if not url:
        return True, ""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            r, err = guarded_request(client, "HEAD", url)
            if r is None:
                log.debug("check_job_alive skipped for %s: %s", url, err)
                return True, ""
            if r.status_code in (404, 410):
                return False, f"Link returned HTTP {r.status_code}"
            if r.status_code == 200:
                final_path = urlparse(str(r.url)).path
                orig_path = urlparse(url).path
                if "/careers" in final_path and "/careers" not in orig_path:
                    return False, "Redirected to general careers page"
    except Exception as e:
        log.debug("check_job_alive failed for %s: %s", url, e)
    return True, ""
