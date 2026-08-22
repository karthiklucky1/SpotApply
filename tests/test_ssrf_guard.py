"""Every server-side fetch of a user-influenced URL goes through one guard.

The anonymous job-check endpoint was well defended — resolve the host, reject
private/loopback/link-local, re-check each redirect hop. The AUTHENTICATED path
was not: `/api/jobs/submit` stores any url a logged-in user sends, and
`/api/jobs/{id}/verify` then HEADed it with `follow_redirects=True` and no host
check at all. Cloud metadata (169.254.169.254) and every internal service were
one signed-in request away, with alive/dead readable from the response.

Being logged in is not a reason to trust a URL — with ten invited users it is
barely a filter. These tests pin the guard on both paths and, importantly, on
the REDIRECT hop, which is the half that a naive check misses.
"""
from __future__ import annotations

import httpx
import pytest

from app.common.ssrf import guarded_request, is_fetchable_url, is_public_host


# ── the predicate ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254",     # metadata
    "10.0.0.5", "192.168.1.1", "172.16.0.1",                    # private
    "", "no-such-host.invalid",                                 # unresolvable
])
def test_private_and_unresolvable_hosts_are_refused(host):
    assert is_public_host(host) is False


def test_a_public_host_resolves(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert is_public_host("example.com") is True


def test_a_name_answering_with_one_private_address_is_refused(monkeypatch):
    """Split-horizon DNS: public and private in the same answer. Checking only
    the first address is how this gets through."""
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("169.254.169.254", 0)),
    ])
    assert is_public_host("split.example.com") is False


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://evil/", "ftp://example.com/x",
    "http://127.0.0.1:8000/admin", "", "not a url",
])
def test_only_public_http_urls_are_fetchable(url):
    assert is_fetchable_url(url) is False


# ── the redirect hop ─────────────────────────────────────────────────────────

def test_a_redirect_into_the_private_network_is_blocked(monkeypatch):
    """The half a naive guard misses: the URL handed in is public, the hop is
    not. Redirects are followed by hand so every hop is re-checked."""
    monkeypatch.setattr("socket.getaddrinfo", lambda host, *a, **k: (
        [(2, 1, 6, "", ("93.184.216.34", 0))] if host == "public.example.com"
        else [(2, 1, 6, "", ("169.254.169.254", 0))]))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://metadata.internal/latest/"})
        raise AssertionError("the guard let the request through to the private host")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        r, err = guarded_request(c, "GET", "http://public.example.com/job/1")
    assert r is None and err == "blocked_host"


def test_a_normal_public_fetch_still_works(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        r, err = guarded_request(c, "GET", "http://public.example.com/job/1")
    assert err is None and r.status_code == 200


def test_a_redirect_loop_terminates(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://public.example.com/again"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        r, err = guarded_request(c, "GET", "http://public.example.com/start")
    assert r is None and err == "too_many_redirects"


# ── the two call sites ───────────────────────────────────────────────────────

def test_liveness_check_never_fetches_a_private_address(monkeypatch):
    """`check_job_alive` is reached from /api/jobs/{id}/verify, which any signed-in
    user can call on a job whose url they chose. A blocked host must report ALIVE:
    refusing to look is not evidence the posting is gone."""
    from app.discovery import verify

    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k): called["n"] += 1
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **k):
            raise AssertionError("a request was issued to a blocked host")

    monkeypatch.setattr(verify.httpx, "Client", _Boom)
    alive, reason = verify.check_job_alive("http://169.254.169.254/latest/meta-data/")
    assert alive is True and reason == ""


def test_submit_rejects_a_non_public_url():
    from fastapi.testclient import TestClient
    from app.api.server import app

    res = TestClient(app).post("/api/jobs/submit", json={
        "company": "Acme", "title": "Engineer",
        "url": "http://169.254.169.254/latest/meta-data/",
        "description": "x",
    })
    assert res.status_code == 422, "an internal address must never reach the job table"
