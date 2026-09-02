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
    """The KNOWN-age window: five days from when WE found the posting."""
    assert settings.shortlist_max_age_days == 5
    assert settings.scoring_max_job_age_days == 5


def test_the_posted_age_bound_is_far_looser_than_the_known_age_one():
    """The two bounds exist precisely because they are not the same size, and
    collapsing them back into one is the bug (app/common/freshness.py).

    The known bound carries the product promise (be first to apply). The posted
    bound only suppresses ancient/evergreen listings, so it has to be loose
    enough to survive unreliable ATS dates: production saw a ~91.5h median
    detection lag with 36.7% of intake already >7d old at first sight. Set the
    posted bound anywhere near 5 and it starts expiring newly discovered jobs
    again — which is what stamped 11 of 13 users down to an empty queue."""
    assert settings.scoring_max_posted_age_days >= 3 * settings.scoring_max_job_age_days, (
        f"SCORING_MAX_POSTED_AGE_DAYS={settings.scoring_max_posted_age_days} is "
        f"close to the known-age bound of {settings.scoring_max_job_age_days}d — "
        f"an unreliable source date will start killing fresh discoveries again"
    )
    assert settings.shortlist_max_posted_age_days >= 3 * settings.shortlist_max_age_days


def test_we_never_pay_to_score_jobs_the_board_will_hide():
    """Scoring a posting the shortlist window then excludes is pure waste — the
    scoring gate must never reach further back than the render window. There
    are now TWO axes, and the invariant has to hold on BOTH: widening the
    scoring gate's posted bound without the render one would recreate the waste
    on the axis nobody was watching."""
    assert settings.shortlist_max_age_days >= settings.scoring_max_job_age_days, (
        f"scoring accepts jobs up to {settings.scoring_max_job_age_days}d old but "
        f"the board only shows {settings.shortlist_max_age_days}d — every job in "
        f"the gap costs a Claude final and is then never displayed"
    )
    assert settings.shortlist_max_posted_age_days >= settings.scoring_max_posted_age_days, (
        f"scoring accepts source dates up to "
        f"{settings.scoring_max_posted_age_days}d old but the board only shows "
        f"{settings.shortlist_max_posted_age_days}d — same waste, other axis"
    )


def test_descriptions_are_not_stripped_out_from_under_scorable_jobs():
    """strip_dead_descriptions blanks the JD text on rows it judges dead. It
    measures KNOWN age, so its window must outlive the known-age scoring window
    — otherwise a job still queued to be scored loses the text it would be
    scored on."""
    assert settings.job_description_strip_age_days > settings.scoring_max_job_age_days


def test_user_job_retention_outlives_the_shortlist_window():
    """Closing a user's rows sooner than the board displays them would blank the
    board from underneath the user."""
    assert settings.user_job_close_age_days > settings.shortlist_max_age_days


# ── the score thresholds ─────────────────────────────────────────────────────

def test_shortlist_thresholds_are_the_calibrated_values():
    """The QUALIFIED bar. 35 -> 60 -> 70.

    With a handful of users there is no sample to calibrate a finer cut on, so
    the bar is set where the founder is willing to stand behind every row on the
    board rather than where a distribution says the volume is. Lowering it to
    fill a thin board is the one change this test exists to make deliberate.
    """
    assert settings.shortlist_score_threshold == 70
    assert settings.shortlist_strong_threshold == 70


def test_the_tier_one_gate_matches_the_banded_prompt():
    """The prescore gate is min(advance, shortlist), and its value is COUPLED TO
    THE TIER-1 PROMPT'S BANDS, not to the shortlist bar. The banded prompt
    (reranker._prescore_system_prompt) scores adjacent-role jobs 40-59; the gate
    sits at 40 — the bottom of that band — so every adjacent fit still reaches
    Claude for adjudication. Under the OLD two-band prompt those same jobs
    scored 60+, which is why the gate used to equal the shortlist bar.

    If you change this number, you are asserting a new prescore DISTRIBUTION:
    re-run scripts/eval_scorers.py --mode gate at N>=2000 first and cut at
    p05_prescore_among_strong. A gate above the adjacent band's floor converts
    Tier-1 false-highs into permanent false-lows — the live eval caught a
    Claude-72 job prescoring 25."""
    assert settings.prescore_advance_threshold == 40, (
        f"PRESCORE_ADVANCE_THRESHOLD={settings.prescore_advance_threshold} — the gate "
        f"and the Tier-1 prompt bands ship together; re-derive from a gate eval"
    )
    # The gate must stay BELOW the shortlist bar: min(advance, shortlist) means a
    # gate above it would be clamped anyway, and equality would drain the whole
    # adjacent band unseen.
    assert settings.prescore_advance_threshold < settings.shortlist_score_threshold


def test_the_board_default_filter_is_not_stricter_than_the_shortlist_bar():
    """Shortlisting at 60 while the board's own default hides anything under 65
    is how ~1,800 jobs/user got shortlisted and then never shown."""
    assert settings.shortlist_strong_threshold >= settings.shortlist_score_threshold


def test_the_alert_bar_never_sits_below_the_shortlist_bar():
    """An alert for a job the board will not show is a notification to nowhere.

    Below the shortlist bar nothing is shortlisted at all, so a lower alert
    threshold cannot fire on anything that exists — it is dead config that
    reads like a working feature."""
    assert settings.fresh_alert_min_score >= settings.shortlist_score_threshold


def test_no_per_day_count_cap_hides_qualified_jobs():
    """Everything clearing the qualified bar reaches the user.

    The bar is the filter; a count cap on top of it would silently drop
    qualified jobs on a good day and be invisible on a bad one. Both existing
    caps are backstops against runaway volume, and at a 70 bar they sit far
    above anything a real user produces — 11.6% of production Claude finals
    cleared even 65. If either is ever tightened toward the tens, it has stopped
    being a backstop and become the allocation."""
    assert settings.daily_shortlist_limit >= 100, (
        f"daily_shortlist_limit={settings.daily_shortlist_limit} is low enough to "
        f"become the real limit — qualified jobs would be dropped, not ranked"
    )
    assert settings.shortlist_render_cap >= settings.daily_shortlist_limit, (
        f"the board renders {settings.shortlist_render_cap} cards but up to "
        f"{settings.daily_shortlist_limit} can be shortlisted in a day — the "
        f"difference is jobs the user is never shown"
    )


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
    'block'. Either is defensible; the default is documented in config.py and was
    flipped to BLOCK before opening the app to real users. What is NOT
    configurable is reporting 'passed' for a résumé that was never read — see
    tests/test_grounding_enforcement.py."""
    assert isinstance(settings.grounding_required, bool)
    assert settings.grounding_required is True
