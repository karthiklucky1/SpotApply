"""Which boards are worth their cost (2026-10-10).

The founder saw boards showing "0" and asked which sources are actually good.
Two things are pinned here: the admin per-source quality route counts from
the LIFECYCLE columns (never `rerank_score IS NOT NULL`, which also matches
expiry/ghost stamps) and buckets by the real producer; and a zero in the
Discover overlay is never bare — it is "off", "⚠ <why>" or "none".

Rows are prefixed `sq-` and cleaned up by that prefix.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import delete, select

from app.api import server
from app.common import ttl_cache
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER

_P = "sq-"


@pytest.fixture(autouse=True)
def _clean():
    ttl_cache.invalidate("admin:source-quality:")
    yield
    ttl_cache.invalidate("admin:source-quality:")
    with get_session() as s:
        ids = list(s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all())
        if ids:
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
            s.exec(delete(Job).where(Job.id.in_(ids)))
            s.commit()


def _job(user_id, ext, **kw) -> int:
    now = datetime.utcnow()
    with get_session() as s:
        j = Job(user_id=user_id, source=kw.pop("source", JobSource.ASHBY), external_id=ext,
                company="SQ Co", title="Engineer", url="https://x/" + ext, description="d",
                discovered_at=now, first_seen=now, **kw)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def test_source_quality_counts_by_producer_from_lifecycle_columns(monkeypatch):
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin")
    now = datetime.utcnow()
    # Shared postings: two Ashby, one of them an old-row uppercase origin.
    _job(SHARED_POOL_USER, f"{_P}a1", origin="ashby")
    _job(SHARED_POOL_USER, f"{_P}a2", origin="ASHBY")
    _job(SHARED_POOL_USER, f"{_P}h1", source=JobSource.INDEED, origin="hn_whoishiring")
    # Copies: a1 Claude-scored and shortlisted; a2 expired unscored; the HN one Tier-1 only.
    c1 = _job("sq-u1", f"{_P}a1", origin="ashby", scored_at=now, rerank_score=81.0)
    _job("sq-u1", f"{_P}a2", origin="ashby", expired_at=now, rerank_score=8.0)
    _job("sq-u1", f"{_P}h1", source=JobSource.INDEED, origin="hn_whoishiring", prescored_at=now, rerank_score=40.0)
    with get_session() as s:
        s.add(Application(user_id="sq-u1", job_id=c1, status=ApplicationStatus.SUBMITTED))
        s.commit()
    out = server.admin_source_quality(request=None, days=7)
    rows = {r["source"]: r for r in out["sources"]}
    ashby = rows["ashby"]
    assert ashby["ingested"] == 2 and ashby["copies"] == 2       # WORKDAY/workday-style casing is ONE bucket
    assert ashby["claude_scored"] == 1 and ashby["expired_unscored"] == 1
    assert ashby["shortlisted"] == 1 and ashby["applied"] == 1
    assert ashby["shortlist_rate"] is None                       # below the 30-copy denominator: null, never a number
    hn = rows["hn_whoishiring"]                                   # the real producer, not the "indeed" bucket
    assert hn["ingested"] == 1 and hn["tier1_only"] == 1 and hn["claude_scored"] == 0
    assert out["degraded"] is False


def test_source_quality_is_admin_only():
    anon = SimpleNamespace(headers={}, cookies={})
    with pytest.raises(HTTPException) as e:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(type(settings), "use_supabase", property(lambda self: True))
            server.admin_source_quality(request=anon, days=7)
    assert e.value.status_code in (401, 403)


def test_a_zero_in_the_discover_overlay_is_never_bare():
    html = (Path(__file__).resolve().parents[1] / "app/templates/dashboard.html").read_text()
    m = re.search(r"function _renderSources\(run\) \{(.*?)\n        \}", html, re.S)
    assert m, "_renderSources missing"
    body = m.group(1)
    assert "s.note" in body and "'none'" in body and "'off'" in body and "'⚠'" in body


def test_the_shared_pass_tells_a_failed_source_from_an_empty_one():
    src = (Path(__file__).resolve().parents[1] / "app/discovery/pipeline.py").read_text()
    i = src.index("async def _fetch_one(name, src_cls):")
    body = src[i: src.index("all_raw_jobs = []", i)]
    assert 'getattr(src, "last_error", None)' in body
    assert 'stat["error"]' in body and 'stat["note"]' in body
