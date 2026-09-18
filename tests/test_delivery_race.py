"""The delivery path after the 2026-09-17 review: one placement per job, copies
that are as old as the posting they copy, adoption that keeps up, a record of
every slate decision, and a prewarm that obeys the breaker.

What production measured:

* one job delivered TWICE, 1.7 s apart — the matching lane released its scoring
  claim the moment the score came back and stored it minutes later, so for that
  window the job still read ``rerank_score IS NULL`` to the 90 s scoring lane;
* 35 per-user copies YOUNGER than the shared posting they copied (one held 56
  days) — the pulse lane's per-user route stamped ``first_seen=now``;
* adoption p95 of ~110 minutes for a DB copy that takes 4.4 s through the pulse
  route, because adoption ran only on the ~6 h scheduler;
* a job that scored 78, missed placement, and waited a pass — with nothing in
  the record saying why.

Rows are prefixed ``drc_`` and cleaned by that prefix only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import (Application, ApplicationStatus, FunnelEvent, Job,
                           JobSource, UserProfile)
from app.discovery.pipeline import SHARED_POOL_USER

_P = "drc_"
UID = "drc_user"
DESC = ("We are hiring a Machine Learning Engineer to build and ship models for "
        "our ranking platform. You will work in Python with PyTorch, own feature "
        "pipelines end to end, and partner with product engineers. Requirements: "
        "3+ years building ML systems in production, strong SQL, and experience "
        "with distributed training. Remote within the United States.")


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated():
    _clean()
    yield
    _clean()


@pytest.fixture
def small_slate(monkeypatch):
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 5)


def _clean():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
        try:
            from app.db.models import JobHiringContext
            s.exec(delete(JobHiringContext).where(
                JobHiringContext.external_id.like(f"{_P}%")))
        except Exception:
            pass
        s.commit()


def _job(s, ext: str, *, user_id, score=None, first_seen=None, closed=False,
         title="Machine Learning Engineer", source=JobSource.GREENHOUSE,
         description=DESC) -> Job:
    when = first_seen or datetime.utcnow()
    j = Job(user_id=user_id, source=source, external_id=_P + ext,
            company=f"Co-{ext}", title=title, location="Remote", remote=True,
            url=f"https://x/{ext}", description=description, rerank_score=score,
            first_seen=when, discovered_at=when, is_closed=closed)
    s.add(j)
    s.commit()
    s.refresh(j)
    # SQLite recycles ids: a job another file deleted may have left placement
    # events and applications behind under this id. This test reads by job id,
    # so it must only ever see its own rows.
    s.exec(delete(FunnelEvent).where(FunnelEvent.job_id == j.id))
    s.exec(delete(Application).where(Application.job_id == j.id))
    s.commit()
    return j


def _raw(ext: str, title="Machine Learning Engineer"):
    from app.discovery.base import RawJob
    return RawJob(source=JobSource.GREENHOUSE.value, external_id=_P + ext,
                  company=f"Co-{ext}", title=title, location="Remote",
                  remote=True, url=f"https://x/{ext}", description=DESC)


def _copy_of(ext: str, user_id: str) -> Job | None:
    with get_session() as s:
        return s.exec(select(Job).where(Job.user_id == user_id,
                                        Job.external_id == _P + ext)).first()


def _placements(jid: int):
    from app.strategy import slate
    with get_session() as s:
        return [(e.passed, e.reason, e.metadata_json) for e in s.exec(
            select(FunnelEvent).where(FunnelEvent.job_id == jid,
                                      FunnelEvent.stage == slate.PLACEMENT_STAGE)
            .order_by(FunnelEvent.id)).all()]


# ── 1. one application per job, decided inside the one writer ────────────────

def test_the_slate_places_a_job_once_and_records_both_decisions(small_slate):
    """Every caller checked "is there an application?" before calling place(),
    and production still delivered one job twice. The check now lives INSIDE
    the writer: the second placement finds the first and says so."""
    from app.strategy import slate
    with get_session() as s:
        j = _job(s, "dup", user_id=UID, score=80)
        first = slate.place(s, j, 80, user_id=UID)
        s.commit()
        second = slate.place(s, s.get(Job, j.id), 80, user_id=UID)
        s.commit()
        jid = j.id

    assert first.created and first.outcome == "placed"
    assert not second.created and second.outcome == "exists"
    with get_session() as s:
        apps = s.exec(select(Application).where(Application.job_id == jid)).all()
    assert len(apps) == 1 and apps[0].status == ApplicationStatus.SHORTLISTED

    recorded = _placements(jid)
    assert [(p, r) for p, r, _ in recorded] == [(True, "placed"), (False, "exists")]
    meta = json.loads(recorded[0][2])
    assert meta["user_id"] == UID and meta["score"] == 80.0


def test_a_refused_placement_leaves_a_record_with_the_cutoff(small_slate):
    """The 78 that missed placement left no trace. Now a refusal is a row with
    the outcome and the bar it failed to clear."""
    from app.strategy import slate
    with get_session() as s:
        for i in range(5):                       # a full slate, every entry opened
            j = _job(s, f"full{i}", user_id=UID, score=80)
            s.add(Application(job_id=j.id, user_id=UID,
                              status=ApplicationStatus.SHORTLISTED,
                              apply_url=j.url, apply_track="autofill",
                              viewed_at=datetime.utcnow()))
        s.commit()
        late = _job(s, "late", user_id=UID, score=70)
        res = slate.place(s, late, 70, user_id=UID)
        s.commit()
        # The re-shortlist backstop offers the same job again every pass: the
        # same refusal is on record once, not once per pass.
        again = slate.place(s, s.get(Job, late.id), 70, user_id=UID)
        s.commit()
        jid = late.id

    assert not res.created and res.outcome == "below_cutoff"
    assert not again.created and again.outcome == "below_cutoff"
    recorded = _placements(jid)
    assert len(recorded) == 1, "a repeated identical refusal must not be re-recorded"
    passed, reason, meta = recorded[0]
    assert (passed, reason) == (False, "below_cutoff")
    assert json.loads(meta)["cutoff"] == 80.0
    with get_session() as s:
        assert s.exec(select(Application).where(Application.job_id == jid)).first() is None


# ── 2. the matching lane holds its claim until the slate has written ─────────

def test_the_matching_lane_holds_its_claim_until_the_slate_has_written():
    """`with claim(jid)` released the job when its score came back; Phase 3
    stored that score minutes later. The claim must still be held at the
    moment the slate writes, and be gone once the pass is over."""
    from app.common import inflight
    from app.matching.pipeline import run_matching
    from app.strategy import slate

    with get_session() as s:
        if not s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first():
            s.add(UserProfile(user_id="local", first_name="T", remote_ok=True,
                              location="Cincinnati, OH"))
            s.commit()
        # The short JD keeps the seniority door-match rule out of the picture
        # (a "3+ years" line against the empty local profile is a rule drop).
        fresh = _job(s, "claim", user_id=None, source=JobSource.MANUAL,
                     title="AI Engineer", description="Remote. Python, ML, LLM role.")
        fresh_id = fresh.id

    held_at_place: dict[int, bool] = {}
    real_place = slate.place

    def _spy(session, job, score, **kw):
        held_at_place[job.id] = job.id in inflight._inflight
        return real_place(session, job, score, **kw)

    with patch("app.matching.matcher.Matcher.__init__", return_value=None), \
         patch("app.matching.matcher.Matcher.rebuild", return_value=1), \
         patch("app.matching.matcher.Matcher.search_for_resume",
               return_value=[(fresh_id, 0.8)]), \
         patch("app.matching.pipeline._load_resume", return_value="Python ML resume"), \
         patch("app.matching.filters.EmbeddingFilter.filter",
               return_value=(True, 0.7, "ok")), \
         patch("app.matching.reranker.Reranker.score",
               return_value=(70.0, "good fit", [], {})), \
         patch("app.discovery.verify.check_job_alive", return_value=(True, None)), \
         patch("app.strategy.slate.place", side_effect=_spy), \
         patch("app.matching.pipeline.score_ghost") as mock_ghost:
        mock_ghost.return_value.is_ghost = False
        mock_ghost.return_value.ghost_score = 0.0
        mock_ghost.return_value.flags_json = "[]"
        mock_ghost.return_value.flags = []
        shortlisted = run_matching(user_id=None)

    assert fresh_id in shortlisted
    assert held_at_place.get(fresh_id) is True, (
        "the claim was released before the slate wrote — another lane could "
        "score and place the same job in that window")
    assert fresh_id not in inflight._inflight, "a claim must not outlive the pass"


# ── 3. a per-user copy is exactly as old as the posting it copies ────────────

def test_a_direct_per_user_copy_inherits_the_shared_first_sighting():
    """The pulse route hands the scraper's RawJob (no first_seen) straight to
    the user's pool. The copy must take the shared pool's sighting."""
    from app.discovery.pipeline import _upsert
    held = datetime.utcnow() - timedelta(days=20)
    with get_session() as s:
        _job(s, "inh1", user_id=SHARED_POOL_USER, first_seen=held)

    raw = _raw("inh1")
    assert _upsert([raw], user_id=UID) == 1
    copy = _copy_of("inh1", UID)
    assert copy is not None
    assert abs((copy.first_seen - held).total_seconds()) < 5, (
        f"copy stamped {copy.first_seen}, shared posting first seen {held}")


def test_the_shared_upsert_stamps_the_raw_job_for_the_routes_that_follow():
    """The lanes upsert the SAME RawJob objects to the shared pool first and
    then to each matching user. A re-seen posting gets its sighting stamped on
    the object; a brand-new one gets the row's, so the copies never re-date it."""
    from app.discovery.pipeline import _upsert
    held = datetime.utcnow() - timedelta(days=10)
    with get_session() as s:
        _job(s, "inh2", user_id=SHARED_POOL_USER, first_seen=held)

    reseen, brand_new = _raw("inh2"), _raw("inh3")
    assert reseen.first_seen is None and brand_new.first_seen is None
    inserted = _upsert([reseen, brand_new], user_id=SHARED_POOL_USER)
    assert inserted == 1, "only the brand-new posting is a new shared row"
    assert reseen.first_seen is not None and abs((reseen.first_seen - held).total_seconds()) < 5
    assert brand_new.first_seen is not None
    assert (datetime.utcnow() - brand_new.first_seen).total_seconds() < 60

    assert _upsert([reseen, brand_new], user_id=UID) == 2
    assert abs((_copy_of("inh2", UID).first_seen - held).total_seconds()) < 5
    assert _copy_of("inh3", UID).first_seen == brand_new.first_seen


def test_repair_re_dates_only_open_copies_that_are_materially_younger():
    """The daily sweep for copies written before the fix: an open copy stamped
    a month after its posting is re-dated; a closed copy is history; a gap
    inside an hour is two lanes stamping seconds apart, not a defect."""
    from app.strategy.adoption import repair_copied_first_seen
    now = datetime.utcnow()
    held = now - timedelta(days=30)
    with get_session() as s:
        _job(s, "rep", user_id=SHARED_POOL_USER, first_seen=held)
        young = _job(s, "rep", user_id=UID, first_seen=now)
        near = _job(s, "rep", user_id=UID + "2", first_seen=held + timedelta(minutes=10))
        closed = _job(s, "rep", user_id=UID + "3", first_seen=now, closed=True)
        ids = (young.id, near.id, closed.id)

    assert repair_copied_first_seen(limit=2000) >= 1

    with get_session() as s:
        y, n, c = (s.get(Job, i) for i in ids)
    assert abs((y.first_seen - held).total_seconds()) < 1, "the young open copy is re-dated"
    assert abs((n.first_seen - (held + timedelta(minutes=10))).total_seconds()) < 1, \
        "a sub-hour gap is left alone"
    assert abs((c.first_seen - now).total_seconds()) < 1, "a closed copy is left alone"


# ── 4. adoption keeps up: bounded, watermarked, replay-safe ──────────────────

def test_incremental_adoption_picks_up_what_the_pool_gained_since_last_step():
    from app.strategy import adoption
    uid = _P + "inc"
    now = datetime.utcnow()
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Machine Learning Engineer"))
        s.commit()
        _job(s, "inc_a", user_id=SHARED_POOL_USER, title="Senior ML Engineer",
             first_seen=now - timedelta(days=1))

    adoption.adopt_incremental(uid)
    assert _copy_of("inc_a", uid) is not None
    assert uid in adoption._ADOPT_WATERMARK, "a complete step advances the watermark"
    mark = adoption._ADOPT_WATERMARK[uid]

    # A posting that lands AFTER the step is inside the next window.
    with get_session() as s:
        _job(s, "inc_b", user_id=SHARED_POOL_USER, first_seen=now)
    adoption.adopt_incremental(uid)
    assert _copy_of("inc_b", uid) is not None
    assert adoption._ADOPT_WATERMARK[uid] >= mark

    # Replaying the window is free: nothing new, nothing duplicated.
    adoption.adopt_incremental(uid)
    with get_session() as s:
        mine = s.exec(select(Job.external_id).where(
            Job.user_id == uid, Job.external_id.like(f"{_P}%"))).all()
    assert sorted(mine) == [_P + "inc_a", _P + "inc_b"]


def test_a_capped_step_keeps_its_watermark_so_the_next_step_reaches_the_rest():
    """A step that selected as many as it was allowed may have left eligible
    postings behind. It must not advance the watermark past them."""
    from app.strategy import adoption
    uid = _P + "cap"
    now = datetime.utcnow()
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Machine Learning Engineer"))
        s.commit()
        for i in range(3):
            _job(s, f"cap{i}", user_id=SHARED_POOL_USER,
                 first_seen=now - timedelta(minutes=i))

    adoption.adopt_incremental(uid, limit=2)
    assert uid not in adoption._ADOPT_WATERMARK, (
        "a capped step advanced the watermark — the third posting would never "
        "have been adopted until the 6-hour pass")

    adoption.adopt_incremental(uid, limit=2)
    with get_session() as s:
        mine = s.exec(select(Job.external_id).where(
            Job.user_id == uid, Job.external_id.like(f"{_P}cap%"))).all()
    assert sorted(mine) == [_P + "cap0", _P + "cap1", _P + "cap2"]
    assert uid in adoption._ADOPT_WATERMARK


def test_the_incremental_window_is_a_range_on_first_seen():
    """`adopt_shared_jobs(since=...)` must not walk the whole 21-day window —
    that is the 6-hour pass's job. A posting older than `since` is not touched."""
    from app.strategy import adoption
    uid = _P + "win"
    now = datetime.utcnow()
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Machine Learning Engineer"))
        s.commit()
        _job(s, "win_old", user_id=SHARED_POOL_USER, first_seen=now - timedelta(days=3))
        _job(s, "win_new", user_id=SHARED_POOL_USER, first_seen=now - timedelta(hours=1))

    adoption.adopt_shared_jobs(uid, since=now - timedelta(hours=6))
    assert _copy_of("win_new", uid) is not None
    assert _copy_of("win_old", uid) is None, "outside the incremental window"


# ── 5. the prewarm obeys the breaker and the budget ──────────────────────────

def _bare_reranker():
    from app.matching.reranker import Reranker
    rr = Reranker.__new__(Reranker)
    rr._anthropic_client = Mock()
    rr._profile = None
    rr._feedback = None
    rr._user_id = _P + "pw"
    return rr


def test_prewarm_does_not_call_a_provider_the_breaker_knows_is_down(monkeypatch):
    """09-14..16: Anthropic rejected every call; the prewarm had no gate, so
    each scoring cycle still opened with two doomed requests per user — the
    only calls still asking once the breaker had tripped."""
    from app.matching import reranker
    rr = _bare_reranker()
    reranker._mark_provider_down("anthropic", "credit balance is too low")
    assert rr.prewarm_cache("resume") is False
    rr._anthropic_client.messages.create.assert_not_called()


def test_prewarm_does_not_spend_when_the_platform_budget_is_gone(monkeypatch):
    from app.matching import reranker
    rr = _bare_reranker()
    monkeypatch.setattr(reranker, "llm_budget_exhausted", lambda: True)
    assert rr.prewarm_cache("resume") is False
    rr._anthropic_client.messages.create.assert_not_called()


def test_prewarm_is_the_recovery_probe_once_the_cooldown_has_passed(monkeypatch):
    """Gated, not disabled: with the breaker closed and budget left, the
    prewarm runs and a success is noted as the outage's end."""
    from app.matching import reranker
    rr = _bare_reranker()
    monkeypatch.setattr(reranker, "llm_budget_exhausted", lambda: False)
    monkeypatch.setattr(reranker, "_get_system_prompt", lambda profile: "sys")
    monkeypatch.setattr(reranker, "_resume_context_block", lambda text, fb: "resume")
    monkeypatch.setattr(reranker, "_track_anthropic_usage", lambda resp: None)
    monkeypatch.setattr(reranker, "_usage_from_anthropic", lambda resp: {})
    monkeypatch.setattr(reranker, "_buffer_spend", lambda *a, **k: None)
    noted = []
    monkeypatch.setattr(reranker, "_note_provider_ok", lambda name: noted.append(name))
    assert reranker.provider_available("anthropic")
    assert rr.prewarm_cache("resume") is True
    rr._anthropic_client.messages.create.assert_called_once()
    assert noted == ["anthropic"]
