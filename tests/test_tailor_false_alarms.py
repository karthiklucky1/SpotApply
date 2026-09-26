"""Three false alarms a user met in one Tailoring Studio window (2026-09-26).

1. The export gate withheld a real LLM engineer's résumé: the draft wrote
   "LLMs" and "production systems"; the master résumé says "LLM-powered" and
   "backend systems in production". Grammar, not a claim.
2. The recruiter verdict called projects dated June and April 2026 "in the
   future relative to today (September 26, 2026) … fabricated", despite the
   prompt stating today's date. Past dates are never accused.
3. The badge said "✓ Grounded in your resume" beside "(Withheld — …)".
"""
from datetime import date
from pathlib import Path

from app.tailoring.doctor import drop_false_future_claims
from app.tailoring.requirements import _present_in, review

MASTER = """# Candidate
## Experience
### Software Engineer | Walmart | Jan 2023 - Mar 2026
- Built LLM-powered agents and deployed them to production on AWS with Python.
- Designed backend systems in production serving 1M requests/day.
## Skills
Python, SQL, AWS"""


def test_plural_of_a_word_on_the_resume_is_present():
    assert _present_in("LLMs", MASTER) and _present_in("llms", MASTER)


def test_words_of_a_phrase_in_one_line_are_present():
    assert _present_in("production systems", MASTER)


def test_real_gaps_are_still_missing():
    for phrase in ("kafka", "distributed systems", "machine learning"):
        assert not _present_in(phrase, MASTER), phrase


def test_words_scattered_across_lines_do_not_add_up():
    text = "Worked on data pipelines.\nLater joined a streaming team."
    assert not _present_in("streaming pipelines", text)


def test_export_review_blocks_only_the_real_invention():
    jd = ("Requirements: 3+ years of experience building LLMs applications. "
          "Experience with production systems. Experience with Kafka is required.")
    draft = "## Experience\n- Built LLMs agents in production systems on AWS.\n- Used Kafka streams."
    assert review(MASTER, draft, jd).unconfirmed_claims == ("kafka",)


TODAY = date(2026, 9, 26)
VERDICT = ("1. **Yes** – the resume hits every core requirement: 3+ years production "
           "backend engineering, strong Python/FastAPI. 2. The SpotApply and VOLTA projects "
           "are dated June 2026 and April 2026 respectively, which are in the future relative "
           "to today (September 26, 2026), making them look fabricated or incomplete.")


def test_sentence_calling_past_dates_future_is_dropped():
    out = drop_false_future_claims(VERDICT, TODAY)
    assert "future" not in out and "fabricated" not in out
    assert out.startswith("1. Yes") and "**" not in out
    assert not out.rstrip().endswith("2.")


def test_a_really_future_date_is_still_flagged():
    v = "1. Borderline – thin keywords. 2. The project dated March 2027 is in the future and looks fabricated."
    assert drop_false_future_claims(v, TODAY) == v


def test_unrelated_verdict_is_untouched():
    v = "No – missing Kubernetes. The biggest risk is no cloud experience."
    assert drop_false_future_claims(v, TODAY) == v


DASH = (Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html").read_text()


def test_withheld_draft_is_not_labelled_grounded():
    assert "const gStatus = d.blocked ? 'withheld'" in DASH
    assert "⚠ Withheld — check needed" in DASH


def test_pause_lives_in_settings_and_the_banner_only_shows_when_not_running():
    assert 'id="settings-search-toggle"' in DASH and "toggleSearchFromSettings" in DASH
    assert "if (state.state === 'setup' || running) { b.style.display = 'none'; return; }" in DASH


SERVER = (Path(__file__).resolve().parents[1] / "app" / "api" / "server.py").read_text()


def test_no_ats_score_is_computed_for_a_withheld_draft():
    assert "if resume_text and not _verdict.blocked and (job.description" in SERVER


def test_pro_accounts_are_not_told_to_upgrade_to_pro():
    from app.api.server import _limit_upsell
    from app.db.models import PlanTier
    assert "Upgrade" not in _limit_upsell(PlanTier.PRO, "for more")
    assert "Pro limit" in _limit_upsell(PlanTier.PRO, "for more")
    assert "Upgrade to Pro" in _limit_upsell(PlanTier.FREE, "for more")


def test_sidebar_plan_card_follows_the_plan():
    # `.sb-upgrade{display:flex}` beat Tailwind's `.hidden`, so the card showed
    # to everyone; and under temporary Pro it must say what they HAVE.
    assert ".sb-upgrade.hidden { display: none; }" in DASH
    assert "You're on Pro" in DASH and "_renderPlanCard(d);" in DASH
    assert 'id="settings-plan-label"' in DASH
    assert '<span class="ml-auto text-[10px] text-indigo-400 font-bold">Free</span>' not in DASH


def test_notifications_pill_has_the_profile_outline():
    assert 'id="header-notif-btn"' in DASH and "#header-notif-btn {" in DASH
