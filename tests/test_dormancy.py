"""A paid job search does not stop because nobody opened the dashboard.

On 2026-09-10 at ~15:00 UTC a user crossed dormant_user_grace_days and left
every lane — adoption, matching, scoring and alerts — with no notification of
any kind, while shortlist hygiene carried on pruning their board. There is no
email or push channel anywhere in the product, so there was no way for them to
find out except by coming back and noticing the board had gone quiet.

Two changes: a live paid subscription overrides the gate entirely (they are
paying for a continuously-running search, and their spend is bounded by their
own plan), and a free user who crosses the line is told once.

And then the override itself was too wide. Audit 2026-09-16: 10 ordinary users
were scored every day with NO sign-in, job view, tailor or submit in 7 days
(three last seen in June); four hit the 250-finals/day PRO ceiling with no
user_subscription row at all; 90.8% of the week's LLM spend went to them. The
override read `_get_user_plan(uid) != FREE` as "paying", and with Stripe on
TEST keys and PLAN_GRANDFATHER_UNTIL unset, every row-less profile resolves to
PRO through _is_grandfathered. A grandfathered PRO is a free ride and a sandbox
subscription is a rehearsal — neither is a paid search. Only
`billing.is_paid_entitlement` (a manual activation, or a Stripe row under an
sk_live_ key) keeps a dormant user's feed running.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import PlanTier, UserNotification, UserProfile, UserSubscription

_P = "dm_"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            s.exec(delete(UserNotification).where(
                UserNotification.user_id.like(f"{_P}%")))
            s.exec(delete(UserSubscription).where(
                UserSubscription.user_id.like(f"{_P}%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


def _subscription(uid: str, **kw) -> None:
    with get_session() as s:
        row = UserSubscription(user_id=_P + uid, plan=PlanTier.PRO)
        for k, v in kw.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def _stripe(monkeypatch, key: str) -> None:
    monkeypatch.setattr(settings, "stripe_secret_key", key, raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x" if key else "",
                        raising=False)


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
    assert server._user_is_active(_profile("recent", days_idle=0.25))


def test_the_default_window_is_one_day(monkeypatch):
    """AUDIT 2026-09-25 (finding 7): a complimentary trial kept its paid search
    running for 21 days after the person left. Active on day one and gone:
    no personalized automatic work after 24 hours."""
    from app.config import Settings
    assert Settings.model_fields["dormant_user_grace_days"].default == 1
    monkeypatch.setattr(server, "_user_paid_search_is_live", lambda p: False)
    assert not server._user_is_active(_profile("day2", days_idle=1.1))


# ── Polling is not activity ──────────────────────────────────────────────────

class _Req:
    def __init__(self, method, path):
        self.method = method

        class _U:
            pass
        self.url = _U()
        self.url.path = path


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/pipeline/live"),          # the 20 s heartbeat
    ("GET", "/api/notifications"),          # the 60 s poll
    ("GET", "/api/freshness-stats"),
    ("GET", "/api/jobs"),
    ("GET", "/api/usage"),
    ("OPTIONS", "/api/profile"),
    ("HEAD", "/dashboard"),
])
def test_passive_requests_do_not_renew_the_window(method, path):
    """A tab left open kept an account "active" forever."""
    assert not server._is_meaningful_request(_Req(method, path))


@pytest.mark.parametrize("method,path", [
    ("POST", "/application/12/viewed"),
    ("POST", "/run/tailor/12"),
    ("PUT", "/api/profile"),
    ("POST", "/application/12/skip"),
    ("GET", "/dashboard"),                   # a person navigated here
    ("GET", "/application/12/download-resume"),
    ("GET", "/application/12/review"),
    ("GET", "/api/fill-pack/12"),            # started filling an application
])
def test_meaningful_actions_renew_the_window(method, path):
    assert server._is_meaningful_request(_Req(method, path))


def test_a_poll_never_stamps_last_active(monkeypatch):
    """End to end through `_get_user_id`: the stamp is only written for a
    meaningful request."""
    stamped = []
    monkeypatch.setattr(server, "_touch_last_active", lambda uid: stamped.append(uid))
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "get_user_id_from_token", lambda tok: "u-poll")

    class R(_Req):
        def __init__(self, method, path):
            super().__init__(method, path)
            self.headers = {"Authorization": "Bearer t"}
            self.cookies = {}
    assert server._get_user_id(R("GET", "/api/pipeline/live")) == "u-poll"
    assert stamped == []
    server._get_user_id(R("POST", "/application/1/viewed"))
    assert stamped == ["u-poll"]


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
    we must avoid is silently stopping something we may be charging for.

    (The seam moved: the gate no longer consults _get_user_plan — that lookup
    WAS the bug, a grandfathered PRO read as paid — so the failure is now the
    subscription-row read itself.)"""
    monkeypatch.setattr("app.billing.stripe_enabled", lambda: True, raising=False)

    def _boom(uid):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(server, "_subscription_row", _boom)
    p = _profile("unknown", days_idle=settings.dormant_user_grace_days + 5)
    assert server._user_paid_search_is_live(p)


def test_pre_revenue_mode_still_applies_the_gate(monkeypatch):
    """While there is nothing to buy, nobody is a paying subscriber and the
    gate is the only thing bounding spend on abandoned accounts."""
    monkeypatch.setattr("app.billing.stripe_enabled", lambda: False, raising=False)
    p = _profile("prerev", days_idle=settings.dormant_user_grace_days + 5)
    assert not server._user_paid_search_is_live(p)


def test_a_free_plan_is_not_a_paid_search(monkeypatch):
    _stripe(monkeypatch, "sk_live_x")
    p = _profile("free", days_idle=settings.dormant_user_grace_days + 5)
    _subscription("free", plan=PlanTier.FREE, stripe_subscription_id="sub_ended")
    assert not server._user_paid_search_is_live(p)


# ── Complimentary is not paid ────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["sk_test_x", "sk_live_x"])
def test_a_grandfathered_user_with_no_row_is_not_a_paid_search(monkeypatch, key):
    """THE SPEND LEAK. Stripe configured, PLAN_GRANDFATHER_UNTIL unset, no
    subscription row: _get_user_plan says PRO (they get PRO limits) but nobody
    is charging them, so three weeks of silence pauses their feed like anyone
    else's. Under either key — being grandfathered has nothing to do with the
    Stripe mode."""
    _stripe(monkeypatch, key)
    monkeypatch.setattr(settings, "plan_grandfather_until", "", raising=False)
    p = _profile("gf", days_idle=settings.dormant_user_grace_days + 5)
    assert server._get_user_plan(_P + "gf") == PlanTier.PRO, "precondition: free PRO"
    assert not server._user_paid_search_is_live(p)
    assert not server._user_is_active(p), "the dormancy gate applies to a free ride"


def test_a_sandbox_subscription_is_not_a_paid_search(monkeypatch):
    """A Stripe-backed PRO row under an sk_test_ key — the only subscription
    production had. Test mode cannot collect money."""
    _stripe(monkeypatch, "sk_test_x")
    p = _profile("sandbox", days_idle=settings.dormant_user_grace_days + 5)
    _subscription("sandbox", stripe_customer_id="cus_sb", stripe_subscription_id="sub_sb",
                  current_period_end=datetime.utcnow() + timedelta(days=17))
    assert server._get_user_plan(_P + "sandbox") == PlanTier.PRO, "PRO limits, yes"
    assert not server._user_paid_search_is_live(p)
    assert not server._user_is_active(p)


def test_a_manual_activation_is_a_paid_search(monkeypatch):
    """Bank transfer / admin set-plan: no Stripe ids, a future period end.
    Someone paid outside Stripe — their search runs whether or not they
    visit, in any Stripe mode."""
    for key, uid in (("sk_test_x", "bank_t"), ("sk_live_x", "bank_l")):
        _stripe(monkeypatch, key)
        p = _profile(uid, days_idle=settings.dormant_user_grace_days + 30)
        _subscription(uid, current_period_end=datetime.utcnow() + timedelta(days=20))
        assert server._user_paid_search_is_live(p), key
        assert server._user_is_active(p), key


def test_a_live_stripe_subscription_is_a_paid_search(monkeypatch):
    _stripe(monkeypatch, "sk_live_x")
    p = _profile("live", days_idle=settings.dormant_user_grace_days + 30)
    _subscription("live", stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  current_period_end=datetime.utcnow() + timedelta(days=17))
    assert server._user_paid_search_is_live(p)
    assert server._user_is_active(p)


def test_an_expired_live_subscription_is_no_longer_a_paid_search(monkeypatch):
    """Past current_period_end + grace nobody is being charged — the gate
    comes back, exactly when _get_user_plan drops them to FREE."""
    _stripe(monkeypatch, "sk_live_x")
    p = _profile("lapsed", days_idle=settings.dormant_user_grace_days + 5)
    _subscription("lapsed", stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  current_period_end=datetime.utcnow() - timedelta(days=4))
    assert server._get_user_plan(_P + "lapsed") == PlanTier.FREE
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
