"""Temporary Pro grants the PLAN. It must never grant the word "paying".

One switch (`TEMPORARY_PRO_FOR_ALL`), read through one function, honoured by the
one entitlement lookup every backend gate and every background lane already
resolves through (`server._get_user_plan`, directly or via
`common/plan_limits.plan_limit`). The whole risk is in what it must NOT reach.

`billing.is_paid_entitlement` is the line. It answers "is money changing hands?"
and three things hang off it: MRR in the admin KPIs, the dormancy gate
(`_user_paid_search_is_live`), and the sandbox-vs-revenue split. Production has
already been burned by the weaker test: reading `plan != FREE` as "paying" let 10
dormant accounts take 90.8% of a week's LLM spend at the PRO ceiling, and a
single test-mode subscription was reported as $100 MRR. If temporary Pro ever
leaked into that function, EVERY account would read as a paying customer, the
dormancy gate would stop applying to anyone, and MRR would become a headcount.

The second half is commercial honesty. While Pro features are free there is
nothing to sell, so the purchase paths close SERVER-SIDE — hiding a button is not
closing a route, and a stale page, a cached client or a bookmarked URL will POST
to it. The customer portal stays open on purpose: an existing subscriber must
keep reaching their own card, invoices and cancel button precisely because the
features are free this month.

Nothing here mutates billing history, cancels anything or refunds anything. The
flag is reversible and writes no rows: flip it off and every user returns to
whatever their own row and the grandfathering rules already said.
"""
from __future__ import annotations

import pytest

from app import billing
from app.config import settings
from app.db.models import PLAN_LIMITS, PlanTier, UserSubscription

_UID = "temp-pro-user"


@pytest.fixture
def temp_pro(monkeypatch):
    """Turn the flag on, with Stripe configured (the case that can go wrong)."""
    monkeypatch.setattr(settings, "temporary_pro_for_all", True, raising=False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    return settings


@pytest.fixture
def no_temp_pro(monkeypatch):
    monkeypatch.setattr(settings, "temporary_pro_for_all", False, raising=False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    return settings


def _row(plan=PlanTier.PRO, **kw) -> UserSubscription:
    row = UserSubscription(user_id=_UID, plan=plan)
    for k, v in kw.items():
        setattr(row, k, v)
    return row


# ── the switch ───────────────────────────────────────────────────────────────

def test_the_flag_is_on_for_the_beta_and_is_one_env_var():
    """2026-09-26 release: Pro features for everyone during the beta, as the
    product owner asked. TEMPORARY_PRO_FOR_ALL=0 turns it off with no
    migration. Feature access is not background compute — that is gated by
    app/common/compute_policy.py regardless of this flag."""
    from app.config import Settings
    assert Settings().temporary_pro_for_all is True
    assert Settings(temporary_pro_for_all=False).temporary_pro_for_all is False


def test_the_switch_is_one_function(temp_pro):
    assert billing.temporary_pro_active() is True


def test_flipping_it_off_restores_the_users_own_entitlement(no_temp_pro):
    assert billing.temporary_pro_active() is False


# ── THE LINE: plan, never payment ────────────────────────────────────────────

def test_temporary_pro_grants_pro_limits(temp_pro):
    from app.api.server import _get_user_plan
    assert _get_user_plan("anyone-at-all") == PlanTier.PRO


@pytest.mark.parametrize("plan", [PlanTier.FREE, PlanTier.PRO])
def test_temporary_pro_never_marks_anyone_as_paying(temp_pro, plan):
    """THE INVARIANT. If this ever returns True from the flag alone, MRR becomes
    a headcount and the dormancy gate stops applying to anybody."""
    assert billing.is_paid_entitlement(None) is False
    assert billing.is_paid_entitlement(_row(plan=plan, stripe_subscription_id="sub_x")) \
        is billing.stripe_live_mode()


def test_is_paid_entitlement_does_not_read_the_flag():
    """Pinned structurally, not just behaviourally: the function must not learn
    about temporary Pro in some later edit that "makes the plans consistent"."""
    import inspect
    src = inspect.getsource(billing.is_paid_entitlement)
    assert "temporary_pro" not in src.split('"""')[-1], \
        "is_paid_entitlement must not consult the temporary-Pro flag"


def test_a_grandfathered_user_is_still_not_paying(temp_pro):
    """No row at all + temporary Pro = PRO limits and zero revenue."""
    from app.api.server import _get_user_plan
    assert _get_user_plan("no-row-user") == PlanTier.PRO
    assert billing.is_paid_entitlement(None) is False


def test_the_dormancy_gate_still_parks_inactive_free_riders(temp_pro):
    """Phase 7: no premium background work for someone who has not opened the
    app. The gate reads is_paid_entitlement on the user's OWN row, so temporary
    Pro cannot switch it off — pinned here because the previous version of this
    bug cost 90.8% of a week's spend."""
    import inspect
    from app.api.server import _user_paid_search_is_live
    src = inspect.getsource(_user_paid_search_is_live)
    assert "is_paid_entitlement" in src
    assert "temporary_pro" not in src

    class _P:
        user_id = _UID
    monkey_row = None                       # no subscription row
    assert billing.is_paid_entitlement(monkey_row) is False


def test_an_expired_row_gets_pro_limits_without_becoming_paid(temp_pro):
    from datetime import datetime, timedelta
    from app.api.server import _get_user_plan
    expired = _row(current_period_end=datetime.utcnow() - timedelta(days=90))
    assert billing.entitlement_expired(expired) is True
    assert billing.is_paid_entitlement(expired) is False
    assert _get_user_plan("expired-user") == PlanTier.PRO       # the point


def test_mrr_is_computed_from_payment_not_from_plan(temp_pro):
    """The admin KPI reads is_paid_entitlement, so a free month cannot inflate it."""
    import inspect
    import app.api.server as server
    src = inspect.getsource(server.admin_metrics)
    assert "is_paid_entitlement" in src
    # The flag is reported, so an operator can tell a complimentary month from a
    # sales quarter — but it is reported, not counted.
    assert "temporary_pro_for_all" in src
    mrr_line = [ln for ln in src.splitlines() if "mrr +=" in ln]
    assert mrr_line and "temporary" not in mrr_line[0]


# ── spend stays bounded: "Pro allowances", never "unlimited" ─────────────────

def test_pro_allowances_not_unlimited_spending(temp_pro):
    """Phase 7 asks for normal Pro allowances. The ceilings are PRO's own."""
    limits = PLAN_LIMITS[PlanTier.PRO]
    assert limits["finals_daily"] == 250
    assert limits["shortlist_daily"] == 35
    assert limits["tailor_daily"] == 35
    # None of them is None/unbounded except the one that always was.
    assert limits["autofill_weekly"] is None


def test_the_platform_backstops_are_untouched(temp_pro):
    assert settings.llm_daily_final_cap > 0
    assert settings.llm_hourly_final_cap > 0
    assert settings.tailor_abuse_daily_cap >= PLAN_LIMITS[PlanTier.PRO]["tailor_daily"], \
        "the abuse backstop must stay above the product limit or it becomes the limit"


def test_the_lanes_read_the_same_one_function(temp_pro):
    """Consistency comes from there being one lookup, not from two that agree."""
    import inspect
    from app.common import plan_limits
    src = inspect.getsource(plan_limits)
    assert "_get_user_plan" in src
    assert "temporary_pro" not in src, \
        "a second entitlement check here is exactly the drift this must not have"


# ── nothing is for sale while it is on ───────────────────────────────────────

def test_purchase_is_unavailable_while_temporary_pro_is_on(temp_pro):
    assert billing.purchase_available() is False


def test_purchase_returns_when_the_flag_goes_off(no_temp_pro):
    assert billing.purchase_available() is True


def test_payment_options_closes_every_paid_path(temp_pro, monkeypatch):
    """Including the manual bank transfer — that is still someone sending money
    for something they already have."""
    monkeypatch.setattr(settings, "payment_bank_details", "IBAN 123", raising=False)
    opts = billing.payment_options()
    assert opts["purchase_available"] is False
    assert opts["bank_transfer"] is False
    assert opts["bank_details"] is None
    assert opts["temporary_pro"]["active"] is True
    assert opts["temporary_pro"]["marks_user_as_paying"] is False


def test_payment_options_reopens_when_the_flag_goes_off(no_temp_pro, monkeypatch):
    monkeypatch.setattr(settings, "payment_bank_details", "IBAN 123", raising=False)
    opts = billing.payment_options()
    assert opts["purchase_available"] is True
    assert opts["bank_transfer"] is True
    assert opts["bank_details"] == "IBAN 123"


def test_the_checkout_route_refuses_server_side():
    """Hiding a button is not closing a route. A stale page, a cached client or a
    bookmarked URL will POST here."""
    import inspect
    import app.api.server as server
    src = inspect.getsource(server.billing_checkout)
    assert "temporary_pro_active" in src
    # Refused BEFORE anything can create a Stripe session.
    assert src.index("temporary_pro_active()") < src.index("create_checkout_session(")


def test_the_customer_portal_stays_open():
    """An existing subscriber must keep reaching their card, invoices and cancel
    button — closing this would trap someone in a subscription they can no
    longer see a reason for."""
    import inspect
    import app.api.server as server
    src = inspect.getsource(server.billing_portal)
    assert "temporary_pro_active" not in src
    assert "create_portal_session" in src


def test_the_webhook_is_untouched_by_the_flag():
    """Phase 7: preserve valid webhook processing. Rows must keep syncing during
    a free month, or the flag going off would strand every subscriber's state."""
    import inspect
    src = inspect.getsource(billing.handle_webhook)
    assert "temporary_pro" not in src


# ── the copy ─────────────────────────────────────────────────────────────────

def test_the_notice_is_the_required_sentence():
    assert billing.TEMPORARY_PRO_NOTICE == \
        "Pro features are temporarily available to everyone."


def test_no_end_date_is_invented():
    """A date nobody has committed to is a promise; a date that slips is worse
    than none at all."""
    status = billing.temporary_pro_status()
    assert status["ends_at"] is None
    assert status["auto_enrolls_on_end"] is False
    for banned in ("until", "expires", "ends on", "days left", "trial"):
        assert banned not in billing.TEMPORARY_PRO_NOTICE.lower()


def test_nothing_enrolls_anyone_when_it_ends(temp_pro):
    assert billing.temporary_pro_status()["auto_enrolls_on_end"] is False


# ── no priced recruiter-research product exists ──────────────────────────────

def test_there_is_no_recruiter_research_for_sale():
    """Phase 7 asks for a $100/month recruiter-research offer to be pulled. No
    such priced product exists in this codebase — $100 is the PRO plan price —
    so this states the absence in the payload every upgrade surface reads,
    which is what stops one being invented."""
    rr = billing.payment_options()["recruiter_research"]
    assert rr["for_sale"] is False
    assert rr["available"] is False
    assert rr["status"] == "On hold while we improve matching quality"


def test_pro_is_the_only_priced_plan():
    from app.db.models import PLAN_PRICES
    priced = {p: v for p, v in PLAN_PRICES.items() if v}
    assert set(priced.values()) == {100}
    assert PLAN_PRICES[PlanTier.FREE] == 0


# ── the user-visible surfaces ────────────────────────────────────────────────

def _render(name: str, temporary_pro: bool) -> str:
    import app.api.server as server
    return server.templates.env.get_template(name).render(
        pro_price=100, temporary_pro=temporary_pro,
        temporary_pro_notice=billing.TEMPORARY_PRO_NOTICE)


@pytest.mark.parametrize("page", ["pricing.html", "landing.html"])
def test_a_public_page_shows_the_notice_and_drops_the_price_cta(page):
    on = _render(page, True)
    assert "Pro features are temporarily available to everyone." in on
    assert "Start Pro — $100/mo" not in on
    assert "Upgrade for higher daily caps" not in on


@pytest.mark.parametrize("page", ["pricing.html", "landing.html"])
def test_the_same_page_is_unchanged_when_the_flag_is_off(page):
    off = _render(page, False)
    assert "temporarily available to everyone" not in off


def test_the_pricing_metadata_does_not_advertise_an_upgrade():
    """Phase 7 names metadata explicitly."""
    on = _render("pricing.html", True)
    head = on[:on.index("</head>")]
    assert "upgrade as your job search scales" not in head
    assert "temporarily available to everyone" in head


def test_the_public_pages_are_rendered_from_the_server_flag():
    """A page that decided for itself is how a price survives on a free month."""
    import inspect
    import app.api.server as server
    for fn in (server.pricing_page, server.index):
        src = inspect.getsource(fn)
        assert "temporary_pro_active" in src
        assert "temporary_pro_notice" in src


def test_the_dashboard_closes_its_purchase_path_before_calling_stripe():
    from pathlib import Path
    dash = Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html"
    html = dash.read_text()
    flow = html[html.index("async function openPricingFlow"):]
    flow = flow[:flow.index("function openSettings")]
    # The temporary-Pro test must come before the checkout POST, or every click
    # fires a request that 503s.
    assert "temporary_pro" in flow
    assert flow.index("temporary_pro") < flow.index("/api/billing/checkout")


def test_the_dashboard_plan_card_matches_plan_limits():
    """It claimed "Unlimited tailored resumes" while the code enforced 35/day —
    the same drift pricing.html carries a comment about."""
    from pathlib import Path
    dash = Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html"
    html = dash.read_text()
    assert "Unlimited tailored resumes" not in html
    assert f"<strong>{PLAN_LIMITS[PlanTier.PRO]['tailor_daily']}</strong> tailored resumes" in html


def test_the_usage_meter_explains_why_the_plan_is_pro():
    import inspect
    import app.api.server as server
    src = inspect.getsource(server.get_usage)
    assert "temporary_pro_status" in src


def test_the_budget_diagnostic_reports_the_flag():
    """It exists to explain an entitlement; "everyone is on PRO this month" is
    the first thing to rule out before anyone goes reading rows."""
    import inspect
    import app.api.server as server
    src = inspect.getsource(server._budget_diagnostic)
    assert "temporary_pro_for_all" in src


# ── no financial mutation ────────────────────────────────────────────────────

def test_the_flag_writes_no_subscription_rows():
    """Reversibility is the whole design: turning it off must need no migration,
    which is only true if turning it on wrote nothing."""
    import inspect
    for fn in (billing.temporary_pro_active, billing.temporary_pro_status,
               billing.purchase_available, billing.payment_options):
        src = inspect.getsource(fn)
        for mutation in ("session.add", "session.commit", "session.delete",
                         "set_plan", "UserSubscription("):
            assert mutation not in src, f"{fn.__name__} must not write billing state"


def test_nothing_cancels_or_refunds_on_the_flag():
    """The flag closes a route; it must not touch anyone's money.

    Comments are stripped first — the route already explains in prose that the
    PORTAL is where cancellation lives, and matching that text would be the test
    failing on the documentation of the thing it wants.
    """
    import inspect
    import re
    import app.api.server as server
    src = inspect.getsource(server.billing_checkout)
    src = re.sub(r"#[^\n]*", "", src)
    src = re.sub(r'"""».*?"""', "", src, flags=re.S).replace("»", "")
    body = src.split('"""')[-1]
    for call in (".cancel(", ".delete(", "Refund", ".modify(", "set_plan("):
        assert call not in body, f"billing_checkout must not call {call}"


# ── the subscription audit: a report, never a mutation ───────────────────────

def test_the_audit_script_has_no_write_path_at_all():
    """Phase 7: prepare a transition FOR REVIEW before any financial mutation.
    So the script cannot have an --apply, and cannot call a Stripe mutator."""
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "audit_subscriptions.py"
    src = path.read_text()
    # Strip the module docstring: it PROMISES there is no --apply, and matching
    # that sentence would be the test failing on the documentation of the thing
    # it wants. The check below looks for the argparse registration instead.
    tree = ast.parse(src)
    body = ast.get_source_segment(src, tree.body[-1]) or ""
    code = "\n".join(ast.get_source_segment(src, n) or "" for n in tree.body
                     if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)))
    assert 'add_argument("--apply"' not in code
    assert "add_argument('--apply'" not in code
    for forbidden in ("session.add", "session.commit", "session.delete",
                      "set_plan(", ".cancel(", ".modify(", "Refund",
                      "Subscription.delete", "handle_webhook"):
        assert forbidden not in code, f"the audit must not be able to {forbidden}"
    assert body


def test_the_audit_never_prints_an_identifier():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts"
           / "audit_subscriptions.py").read_text()
    # Ids are fingerprinted; the raw columns must never reach a print.
    assert "_fp(r.user_id)" in src
    for leak in ('d["user_id"]', "r.user_id}", "stripe_customer_id}",
                 "stripe_subscription_id}", "settings.stripe_secret_key"):
        assert leak not in src


def test_the_audit_buckets_each_row_kind_and_answers_the_charging_question(monkeypatch):
    """The audit's whole job is to say who is actually being billed."""
    from datetime import datetime, timedelta
    import scripts.audit_subscriptions as audit_mod

    future = datetime.utcnow() + timedelta(days=20)
    past = datetime.utcnow() - timedelta(days=90)
    rows = [
        UserSubscription(user_id="a-manual", plan=PlanTier.PRO, current_period_end=future),
        UserSubscription(user_id="b-stripe", plan=PlanTier.PRO, current_period_end=future,
                         stripe_subscription_id="sub_1", stripe_customer_id="cus_1"),
        UserSubscription(user_id="c-expired", plan=PlanTier.PRO, current_period_end=past,
                         stripe_subscription_id="sub_2"),
        UserSubscription(user_id="d-free", plan=PlanTier.FREE),
    ]

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def exec(self, stmt):
            class _R:
                def all(_s): return rows
                def one(_s): return 4
            return _R()

    monkeypatch.setattr(audit_mod, "audit", audit_mod.audit)      # keep the real one
    monkeypatch.setitem(__import__("sys").modules, "_noop", __import__("sys"))
    import app.db.init_db as init_db
    monkeypatch.setattr(init_db, "get_session", lambda *a, **k: _Sess())

    # Test-mode keys: a Stripe-backed row is a SANDBOX row, not revenue.
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    monkeypatch.setattr(settings, "temporary_pro_for_all", True, raising=False)

    a = audit_mod.audit()
    assert a["by_kind"] == {"manual_activation": 1, "stripe_sandbox": 1,
                            "expired": 1, "already_free": 1}
    assert a["counted_as_paying"] == 1            # only the manual activation
    assert a["mrr_usd"] == 100
    assert "NO real money" in a["are_subscribers_still_charged"]
    # And what a flag-off would do, which is the only number a transition needs.
    assert a["would_be_pro_if_flag_off"] == 2     # manual + sandbox, not expired/free


def test_a_live_key_changes_the_charging_answer(monkeypatch):
    """Under sk_live_ the honest answer flips: Stripe bills on its own schedule
    and our flag cannot stop it."""
    from datetime import datetime, timedelta
    import scripts.audit_subscriptions as audit_mod
    rows = [UserSubscription(user_id="live-one", plan=PlanTier.PRO,
                             current_period_end=datetime.utcnow() + timedelta(days=20),
                             stripe_subscription_id="sub_live")]

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def exec(self, stmt):
            class _R:
                def all(_s): return rows
                def one(_s): return 1
            return _R()

    import app.db.init_db as init_db
    monkeypatch.setattr(init_db, "get_session", lambda *a, **k: _Sess())
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    monkeypatch.setattr(settings, "temporary_pro_for_all", True, raising=False)

    a = audit_mod.audit()
    assert a["by_kind"] == {"stripe_live": 1}
    assert a["mrr_usd"] == 100
    ans = a["are_subscribers_still_charged"]
    assert ans.startswith("YES")
    assert "does not touch Stripe" in ans
    assert "NOT done by this script" in ans
