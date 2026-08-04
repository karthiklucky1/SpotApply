"""The numbers the product's economics and safety rest on.

Every test that touches these thresholds injects its own value — which is correct
for testing behaviour, but means the DEFAULTS are asserted nowhere. Set
shortlist_max_age_days to 90 and the entire suite stays green while the board
starts showing month-old postings.

Several of these are founder decisions with a stated rationale (of real Claude
finals, 44.5% cleared 35 but only 11.6% cleared 65, so the bar moved to 60), and a
decision that lives only in a default is a decision that can be lost to a merge.
The lockstep relations matter as much as the values: pay to score jobs the board
then hides, or gate Tier-1 above the shortlist bar, and the funnel quietly stops
making sense.
"""
from __future__ import annotations

from app.config import settings
from app.db.models import PLAN_LIMITS, PlanTier


# ── the freshness window ─────────────────────────────────────────────────────

def test_the_funnel_is_fresh_only_at_five_days():
    assert settings.shortlist_max_age_days == 5
    assert settings.scoring_max_job_age_days == 5


def test_we_never_pay_to_score_jobs_the_board_will_hide():
    """Scoring a posting the shortlist window then excludes is pure waste — the
    scoring gate must never reach further back than the render window."""
    assert settings.shortlist_max_age_days >= settings.scoring_max_job_age_days, (
        f"scoring accepts jobs up to {settings.scoring_max_job_age_days}d old but "
        f"the board only shows {settings.shortlist_max_age_days}d — every job in "
        f"the gap costs a Claude final and is then never displayed"
    )


def test_user_job_retention_outlives_the_shortlist_window():
    """Closing a user's rows sooner than the board displays them would blank the
    board from underneath the user."""
    assert settings.user_job_close_age_days > settings.shortlist_max_age_days


# ── the score thresholds ─────────────────────────────────────────────────────

def test_shortlist_thresholds_are_the_calibrated_values():
    assert settings.shortlist_score_threshold == 60
    assert settings.shortlist_strong_threshold == 65


def test_the_tier_one_gate_moves_in_lockstep_with_the_shortlist_bar():
    """The prescore gate is min(advance, shortlist). If advance drifts ABOVE the
    shortlist bar, Tier-1 silently rejects jobs that would have been shortlisted;
    if it drifts far below, the cheap tier stops filtering and every candidate
    reaches Claude. They are meant to move together."""
    assert settings.prescore_advance_threshold == settings.shortlist_score_threshold, (
        f"PRESCORE_ADVANCE_THRESHOLD={settings.prescore_advance_threshold} vs "
        f"shortlist_score_threshold={settings.shortlist_score_threshold} — raise "
        f"them together (see CLAUDE.md)"
    )


def test_the_board_default_filter_is_not_stricter_than_the_shortlist_bar():
    """Shortlisting at 60 while the board's own default hides anything under 65
    is how ~1,800 jobs/user got shortlisted and then never shown."""
    assert settings.shortlist_strong_threshold >= settings.shortlist_score_threshold


# ── per-user spend ───────────────────────────────────────────────────────────

def test_plan_finals_allowances_are_per_user_and_unchanged():
    """Spend is allocated per user per plan; one global pool divided by N users
    meant every signup thinned every existing user's feed."""
    assert PLAN_LIMITS[PlanTier.FREE]["finals_daily"] == 15
    assert PLAN_LIMITS[PlanTier.PRO]["finals_daily"] == 50
    assert PLAN_LIMITS[PlanTier.AGENCY]["finals_daily"] == 100


def test_the_global_caps_are_only_a_backstop_not_the_allocation():
    """LLM_DAILY_FINAL_CAP must stay well above the per-plan sum for a realistic
    user count, or it silently becomes the real limit again."""
    pro = PLAN_LIMITS[PlanTier.PRO]["finals_daily"]
    assert settings.llm_daily_final_cap >= pro * 50, (
        f"global daily cap {settings.llm_daily_final_cap} is under 50 Pro users' "
        f"worth of finals ({pro * 50}) — it would bind before the per-plan caps do"
    )


# ── memory + safety ──────────────────────────────────────────────────────────

def test_only_one_headless_browser_at_a_time():
    """Each Chromium is ~400MB of container memory invisible in our own RSS.
    Raising this default is how the OOM kill happened (docs/MEMORY.md)."""
    assert settings.browser_max_concurrency == 1


def test_the_company_cap_displacement_margin_is_unchanged():
    assert settings.company_cap_displace_margin == 5


# ── CardRace stays in shadow ─────────────────────────────────────────────────

def test_cardrace_is_shadow_only_until_its_holdout_gates_pass():
    """CARD_MATCH_ENABLED=1 hands real shortlist decisions to the deterministic
    engine. docs/CARDRACE_DESIGN.md §3.4 gates that on recorded agreement; until
    then shadow records beside every real final and decides nothing."""
    assert settings.card_match_enabled is False, (
        "CardRace is live — it must stay shadow-only until the §3.4 holdout gates "
        "pass (see docs/CARDRACE_DESIGN.md)")
    assert settings.card_match_shadow is True, (
        "shadow recording is off, so no agreement data is accumulating and the "
        "gates can never be evaluated")


def test_card_minting_is_capped():
    assert settings.card_mint_daily_cap > 0, (
        "an uncapped mint budget is an uncapped bill")


# ── anti-hallucination posture ───────────────────────────────────────────────

def test_grounding_never_silently_reports_a_pass_it_did_not_verify():
    """grounding_required chooses between 'deliver, labelled unverified' and
    'block'. Either is defensible; the default is documented in config.py. What is
    NOT configurable is reporting 'passed' for a résumé that was never read — see
    tests/test_grounding_enforcement.py."""
    assert isinstance(settings.grounding_required, bool)
    assert settings.grounding_required is False
