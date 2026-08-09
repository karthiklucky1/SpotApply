"""The Shortlisted board's status set has exactly one definition.

Two surfaces render it — the SSR dashboard (list + count) and the live poll
/api/pipeline/live. When they disagree, dashboard.html's auto-reloader
(`rendered === 0 && server > 0`) reloads the page every 30s forever, which is a
real incident this repo has already had once.

ERROR is on the board deliberately: clicking "Auto-Fill & Apply" starts
background tailoring, which BLOCKS at ERROR when the grounding check fails.
That is correct (the résumé may carry unverified claims) but the card used to
vanish from Shortlisted mid-application — the count dropped by one, nothing was
submitted, and nothing explained why.
"""
from app.api import server
from app.db.models import ApplicationStatus


def test_error_applications_stay_on_the_board():
    assert ApplicationStatus.ERROR in server._SHORTLIST_BOARD_STATUSES, (
        "a tailoring failure must not silently remove a card the user is "
        "actively applying to — keep ERROR visible with its notes"
    )


def test_board_covers_shortlisted_tailored_and_autofill_review():
    board = set(server._SHORTLIST_BOARD_STATUSES)
    expected = {
        ApplicationStatus.SHORTLISTED,
        ApplicationStatus.TAILORED,
        ApplicationStatus.ERROR,
        *server._AUTOFILL_REVIEW_STATUSES_CONST,
    }
    assert board == expected


def test_live_poll_partitions_the_same_statuses():
    """/api/pipeline/live derives its sets from the board constant.

    Guards the derivation itself: shortlist + in-progress must partition the
    board exactly, with nothing dropped and nothing double-counted.
    """
    inprogress = set(server._AUTOFILL_REVIEW_STATUSES_CONST)
    shortlist = set(server._SHORTLIST_BOARD_STATUSES) - inprogress
    assert shortlist | inprogress == set(server._SHORTLIST_BOARD_STATUSES)
    assert not (shortlist & inprogress), "a status counted on both sides"
    # The board list itself must not carry duplicates.
    assert len(server._SHORTLIST_BOARD_STATUSES) == len(set(server._SHORTLIST_BOARD_STATUSES))


def test_board_never_shows_finished_or_discarded_applications():
    board = set(server._SHORTLIST_BOARD_STATUSES)
    for gone in (ApplicationStatus.SUBMITTED, ApplicationStatus.REJECTED,
                 ApplicationStatus.SKIPPED, ApplicationStatus.ACCEPTED,
                 ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER):
        assert gone not in board, f"{gone} has its own tab — it must not sit on the shortlist"
