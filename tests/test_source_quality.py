"""Source-quality weighting and link-liveness checks."""
from unittest.mock import patch

import httpx

from app.db.models import JobSource
from app.matching.filters.constants import (
    DEFAULT_SOURCE_QUALITY,
    FRESH_POSTING_BONUS,
    source_quality,
)
from app.discovery.verify import check_job_alive


def test_source_quality_ordering():
    assert source_quality(JobSource.GREENHOUSE) == 1.0
    assert source_quality("lever") == 1.0
    assert source_quality(JobSource.SERPAPI) > source_quality(JobSource.REMOTIVE)
    assert source_quality(JobSource.REMOTIVE) > source_quality(JobSource.JOOBLE)
    assert source_quality("something_new") == DEFAULT_SOURCE_QUALITY
    assert source_quality(None) == DEFAULT_SOURCE_QUALITY


def test_fresh_direct_ats_beats_stale_aggregator():
    # Equal cross-encoder score: a fresh greenhouse job must outrank a stale
    # jooble redirect for the LLM budget.
    ce = 0.5
    fresh_direct = ce * source_quality("greenhouse") * FRESH_POSTING_BONUS
    stale_redirect = ce * source_quality("jooble")
    assert fresh_direct > stale_redirect


def test_check_job_alive_dead_on_404():
    with patch("httpx.Client") as MockClient:
        client = MockClient.return_value.__enter__.return_value
        client.head.return_value = httpx.Response(
            404, request=httpx.Request("HEAD", "https://x.co/job/1"))
        alive, reason = check_job_alive("https://x.co/job/1")
    assert alive is False
    assert "404" in reason


def test_check_job_alive_dead_on_careers_redirect():
    with patch("httpx.Client") as MockClient:
        client = MockClient.return_value.__enter__.return_value
        resp = httpx.Response(200, request=httpx.Request("HEAD", "https://x.co/careers"))
        client.head.return_value = resp
        alive, reason = check_job_alive("https://x.co/job/123")
    assert alive is False
    assert "careers" in reason.lower()


def test_check_job_alive_ok_and_fail_open():
    # 200 with same-shape URL → alive
    with patch("httpx.Client") as MockClient:
        client = MockClient.return_value.__enter__.return_value
        client.head.return_value = httpx.Response(
            200, request=httpx.Request("HEAD", "https://x.co/job/123"))
        alive, _ = check_job_alive("https://x.co/job/123")
    assert alive is True

    # Network error → treated as alive (never over-close on a timeout)
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.head.side_effect = httpx.ConnectTimeout("boom")
        alive, _ = check_job_alive("https://x.co/job/123")
    assert alive is True


def test_disabled_feed_flags_are_honored():
    import asyncio
    from app.config import settings
    from app.discovery.sources.remoteok import RemoteOKSource
    from app.discovery.sources.themuse import TheMuseSource
    from app.discovery.sources.arbeitnow import ArbeitnowSource

    with patch.object(settings, "remoteok_enabled", False), \
         patch.object(settings, "themuse_enabled", False), \
         patch.object(settings, "arbeitnow_enabled", False):
        assert asyncio.run(RemoteOKSource().fetch_jobs()) == []
        assert asyncio.run(TheMuseSource().fetch_jobs()) == []
        assert asyncio.run(ArbeitnowSource().fetch_jobs()) == []
