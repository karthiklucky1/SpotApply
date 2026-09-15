"""A paid job search does not stop because nobody opened the dashboard.

On 2026-09-10 at ~15:00 UTC a user crossed dormant_user_grace_days and left
every lane — adoption, matching, scoring and alerts — with no notification of
any kind, while shortlist hygiene carried on pruning their board. There is no
email or push channel anywhere in the product, so there was no way for them to
find out except by coming back and noticing the board had gone quiet.

Two changes: a live paid subscription overrides the gate entirely (they are
paying for a continuously-running search, and their spend is bounded by their
own plan), and a free user who crosses the line is told once.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import PlanTier, UserNotification, UserProfile

_P = "dm_"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            s.exec(delete(UserNotification).where(
                UserNotification.user_id.like(f"{_P}%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


def _profile(uid: str, days_idle: float) -> UserProfile:
    with get_session() as s:
        p = UserProfile(user_id=_P + uid,
                        last_active_at=datetime.utcnow() - timedelta(days=days_idle))
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def _notices(uid: str) -> list:
    with get_session() as s:
        return s.exec(select(UserNotification).where(
            UserNotification.user_id == _P + uid,
            UserNotification.type == "feed_paused")).all()


# ── The gate itself ──────────────────────────────────────────────────────────

def test_a_recently_active_user_is_active():
    assert server._user_is_active(_profile("recent", days_idle=1))


def test_an_idle_free_user_goes_dormant(monkeypatch):
    monkeypatch.setattr(server, "_user_paid_search_is_live", lambda p: False)
    p = _profile("idle", days_idle=settings.dormant_user_grace_days + 2)
    assert not server._user_is_active(p)


def test_a_paying_subscribers_search_keeps_running(monkeypatch):
    """The product charges for a search that runs whether or not they visit."""
    monkeypatch.setattr(server, "_user_paid_search_is_live", lambda p: True)
    p = _profile("paid", days_idle=settings.dormant_user_grace_days + 30)
    assert server._user_is_active(p)


def test_an_unresolvable_plan_does_not_pause_a_search(monkeypatch):
    """If we cannot tell whether they are paying, keep the feed on. The failure
    we must avoid is silently stopping something we may be charging for."""
    monkeypatch.setattr("app.billing.stripe_enabled", lambda: True, raising=False)

    def _boom(uid):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(server, "_get_user_plan", _boom)
    p = _profile("unknown", days_idle=settings.dormant_user_grace_days + 5)
    assert server._user_paid_search_is_live(p)


def test_pre_revenue_mode_still_applies_the_gate(monkeypatch):
    """While there is nothing to buy, nobody is a paying subscriber and the
    gate is the only thing bounding spend on abandoned accounts."""
    monkeypatch.setattr("app.billing.stripe_enabled", lambda: False, raising=False)
    p = _profile("prerev", days_idle=settings.dormant_user_grace_days + 5)
    assert not server._user_paid_search_is_live(p)


def test_a_free_plan_is_not_a_paid_search(monkeypatch):
    monkeypatch.setattr("app.billing.stripe_enabled", lambda: True, raising=False)
    monkeypatch.setattr(server, "_get_user_plan", lambda uid: PlanTier.FREE)
    p = _profile("free", days_idle=settings.dormant_user_grace_days + 5)
    assert not server._user_paid_search_is_live(p)


# ── Telling them ─────────────────────────────────────────────────────────────

def test_the_user_is_told_once_when_their_feed_pauses():
    p = _profile("told", days_idle=settings.dormant_user_grace_days + 2)
    assert server._notify_if_newly_dormant(p)
    assert len(_notices("told")) == 1

    # Re-read the stamped profile: the lanes call this every tick.
    with get_session() as s:
        p2 = s.exec(select(UserProfile).where(
            UserProfile.user_id == _P + "told")).first()
    assert not server._notify_if_newly_dormant(p2)
    assert len(_notices("told")) == 1, "the notice repeated on the next lane tick"


def test_coming_back_re_arms_the_notice():
    """A visit AFTER the notice re-arms it, so the next dormancy episode is
    announced too.

    The sequence in real time: paused and told 60 days ago, came back 30 days
    ago, quiet ever since. last_active_at is later than the stamp, so this is a
    new episode.
    """
    grace = settings.dormant_user_grace_days
    with get_session() as s:
        s.add(UserProfile(
            user_id=_P + "return",
            dormancy_notified_at=datetime.utcnow() - timedelta(days=grace * 2 + 20),
            last_active_at=datetime.utcnow() - timedelta(days=grace + 9),
        ))
        s.commit()
        p = s.exec(select(UserProfile).where(
            UserProfile.user_id == _P + "return")).first()
        s.expunge(p)
    assert server._notify_if_newly_dormant(p)
    assert len(_notices("return")) == 1

    # ...and it does not repeat for THIS episode.
    with get_session() as s:
        again = s.exec(select(UserProfile).where(
            UserProfile.user_id == _P + "return")).first()
        s.expunge(again)
    assert not server._notify_if_newly_dormant(again)
    assert len(_notices("return")) == 1


def test_the_notice_says_what_happened_and_how_to_undo_it():
    p = _profile("copy", days_idle=settings.dormant_user_grace_days + 2)
    server._notify_if_newly_dormant(p)
    msg = _notices("copy")[0].message.lower()
    assert "paused" in msg
    assert "open spotapply" in msg, "the notice must say how to restart the feed"
