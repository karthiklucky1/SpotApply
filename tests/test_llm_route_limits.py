"""Every route that spends LLM tokens on demand carries a rate limit.

Most spend in this app is bounded by design: the lanes check
`llm_budget_exhausted()` before Tier-1, and finals are metered per user per plan
(`PLAN_LIMITS["finals_daily"]`). Interactive routes are the exception — a signed-in
user (or a loop in a browser tab) can call them as fast as HTTP allows, and each
call is a real Anthropic invoice. Nine of them had no limit at all.

The limits are slowapi burst guards keyed by _rate_key — the authenticated USER
(hashed bearer) when one is present, the client IP only as the anonymous
fallback (server.py). A burst guard is the right shape for the immediate risk
(accidental or deliberate hammering); a genuine per-user token budget for
interactive features is still open (see docs/AUDIT_2026_07_30.md §"Still open").
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "app" / "api" / "server.py"

# Route decorator → why it costs money. A route added to server.py that spends
# tokens belongs in this list; that is the point of the list existing.
LLM_ROUTES = {
    '@app.get("/api/resume/analysis")': "full-résumé LLM analysis",
    '@app.get("/api/resume/recruiter-read")': "LLM recruiter read of the whole résumé",
    '@app.get("/api/resume/metric-gaps")': "LLM pass over every bullet",
    '@app.post("/api/resume/metric-answers")': "LLM rewrite per answered gap",
    '@app.post("/api/resume/synthesize")': "builds a whole résumé from the profile",
    '@app.post("/api/resume/extract-profile")': "LLM profile extraction",
    '@app.get("/api/skill-gap")': "LLM JD-vs-résumé advice",
    '@app.post("/api/answer-question")': "~$0.002 per cache miss, called per textarea",
    '@app.post("/api/recruiter/search")': "LLM-ranks the candidate pool",
    '@app.post("/application/{application_id}/ask")': "one Claude call per question",
    '@app.get("/application/{application_id}/referral")': "LLM outreach drafts",
    '@app.post("/run/extract-link")': "LLM extraction from a pasted link",
    '@app.post("/api/sync-emails")': "LLM classification per email",
    '@app.post("/run/tailor/{application_id}")': "résumé + cover letter, ~$0.045-0.09",
    '@app.post("/api/public/job-check")': "unauthenticated — LLM fit check",
    '@app.post("/api/public/demo-match")': "unauthenticated — LLM demo match",
    # The three routes the Aug 2026 review found spending with no limit at all:
    '@app.get("/api/fill-pack/{application_id}/resume")': "auto-tailors on a GET the extension retries",
    '@app.get("/api/fill-pack/{application_id}")': "background auto-tailor per fill-pack open",
    '@app.get("/application/{application_id}/company")': "one Claude call per company-cache miss",
}


def _source() -> str:
    return SERVER.read_text(encoding="utf-8")


def _limit_after(src: str, decorator: str) -> str | None:
    """The rate limit declared immediately below `decorator`, if any."""
    i = src.find(decorator)
    if i < 0:
        return None
    window = src[i + len(decorator): i + len(decorator) + 200]
    m = re.search(r'@_rate_limit\(\s*["\']([^"\']+)["\']', window.split("def ")[0])
    return m.group(1) if m else None


@pytest.mark.parametrize("decorator,why", sorted(LLM_ROUTES.items()))
def test_llm_spending_routes_are_rate_limited(decorator, why):
    src = _source()
    assert decorator in src, (
        f"{decorator} is no longer in server.py — remove it from LLM_ROUTES or fix "
        f"the path")
    limit = _limit_after(src, decorator)
    assert limit, (
        f"{decorator} ({why}) has no @_rate_limit. Each call is a real invoice and "
        f"nothing else bounds interactive spend — the lanes' budget guards and the "
        f"per-plan finals cap do not cover on-demand routes."
    )


@pytest.mark.parametrize("decorator,why", sorted(LLM_ROUTES.items()))
def test_the_limits_are_actually_restrictive(decorator, why):
    """A limit of 1000/minute is decoration. Cap the per-minute allowance at 60."""
    limit = _limit_after(_source(), decorator)
    if not limit:
        pytest.skip("covered by the test above")
    n, _, per = limit.partition("/")
    assert per.strip() in ("second", "minute", "hour", "day"), limit
    if per.strip() == "minute":
        assert int(n) <= 60, f"{decorator}: {limit} is too loose to bound spend"


def test_the_unauthenticated_routes_are_the_strictest():
    """The two public demos can be hit by anyone with no account at all, so their
    limits must be at least as tight as the signed-in ones."""
    src = _source()
    for dec in ('@app.post("/api/public/job-check")',
                '@app.post("/api/public/demo-match")'):
        limit = _limit_after(src, dec)
        assert limit, f"{dec} is unauthenticated and unlimited"
        n, _, per = limit.partition("/")
        assert per.strip() == "minute" and int(n) <= 15, (
            f"{dec} is reachable without a session; {limit} is too generous")


def test_the_rate_limit_decorator_is_not_a_silent_no_op():
    """_rate_limit degrades to a no-op when slowapi is absent, which is correct for
    a dev box but must be visible. slowapi is a declared dependency, so in any real
    deployment the limiter is live — assert the dependency stays declared."""
    reqs = (SERVER.parent.parent.parent / "requirements.txt").read_text()
    assert re.search(r"^slowapi", reqs, re.M), (
        "slowapi is no longer declared, so every @_rate_limit above silently "
        "becomes a no-op and all the limits in this file stop existing")
