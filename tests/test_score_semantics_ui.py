"""What the board says a number means.

``Job.rerank_score`` is overloaded: it carries real 0-100 Claude verdicts AND
the ghost (5.0) and age-expiry (8.0) sentinels AND Tier-1 prescore stamps. The
board rendered every one of them as "N% match" with a "Reviewed" verdict, so a
posting nobody had ever scored appeared to the user as one the AI had read and
rated 5. The status copy also still quoted a shortlist bar of 35, which stopped
being the bar when it moved to 70 — everything scoring 40-69 was labelled
"above the bar, awaiting a slot" when it had actually been rejected.
"""
from __future__ import annotations

import re
from datetime import datetime

from app.api.server import _score_kind
from app.common.freshness import EXPIRY_SENTINEL_SCORE, GHOST_SENTINEL_SCORE

NOW = datetime.utcnow()


def test_a_real_claude_verdict_is_a_final():
    assert _score_kind(82.0, NOW, NOW, None) == "final"


def test_a_tier1_stamp_is_not_a_fit_score():
    """32 from the cheap first pass is not "32% match" — nothing authoritative
    ever looked at this job."""
    assert _score_kind(32.0, None, NOW, None) == "prescore"


def test_the_sentinels_are_named_for_what_they_are():
    assert _score_kind(GHOST_SENTINEL_SCORE, None, None, None) == "ghost"
    assert _score_kind(EXPIRY_SENTINEL_SCORE, None, None, NOW) == "expired"
    # The lifecycle column is authoritative even when the value is not a sentinel.
    assert _score_kind(11.0, None, None, NOW) == "expired"


def test_an_unscored_job_is_queued():
    assert _score_kind(None, None, None, None) == "queued"


def test_a_legacy_row_with_no_lifecycle_columns_is_still_shown_as_a_score():
    """Rows written before prescored_at/scored_at/expired_at shipped have a real
    verdict and nothing else — they must not all become 'screened out'."""
    assert _score_kind(74.0, None, None, None) == "final"


def test_a_scored_row_beats_an_expiry_stamp_written_later():
    """A job that was genuinely scored and later swept by the expiry pass is
    still a scored job — scored_at is checked first."""
    assert _score_kind(76.0, NOW, NOW, NOW) == "final"


# ── The board ────────────────────────────────────────────────────────────────

def _dashboard() -> str:
    with open("app/templates/dashboard.html") as f:
        return f.read()


def test_the_board_renders_a_percentage_only_for_a_final():
    src = _dashboard()
    assert "j.score_kind" in src, "the board no longer distinguishes kinds of score"
    assert "kind === 'final' && j.rerank !== null" in src, (
        "the percentage badge is no longer gated on the score being a real verdict")


def test_the_board_does_not_quote_a_bar_of_35():
    src = _dashboard()
    assert "below the shortlist bar (35)" not in src
    assert "shortlist_score_threshold or 70" in src, (
        "the status copy should read the configured bar, not a literal")


def test_the_api_reports_the_score_kind():
    import inspect
    from app.api import server
    src = inspect.getsource(server)
    assert '"score_kind": _score_kind(' in src
    # The lifecycle columns have to be selected or the kind cannot be derived.
    cols = re.search(r"_JOB_LIST_COLS = \((.*?)\n\)", src, re.S).group(1)
    for col in ("Job.scored_at", "Job.prescored_at", "Job.expired_at"):
        assert col in cols, f"{col} is not projected, so score_kind cannot be right"
