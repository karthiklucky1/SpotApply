"""A status is not a date, and neither is a plan.

PRODUCTION, 2026-09-25. One live profile held `work_authorization: F-1 OPT` with
an `ead_end_date` more than three months in the past, and `requires_sponsorship:
false` — which contradicts F-1 OPT. `work_auth.assess_profile` read only the
status STRING; `ead_end_date` was not consulted anywhere in the module. So the
profile auto-answered "Yes, authorized to work in the US" on application forms,
with `needs_user_review=False`: a legal assertion made on someone's behalf from
a field nobody had checked.

Four facts that were one, and are now separate:

    what status they hold        basis                "F-1 OPT"
    is it still live             validity/valid_through   from the end date
    might it be extended         extension_possible   MAY be, never "approved"
    what the employer must do    employer_cost_now / needs_future_sponsorship

And four questions an employer can ask, which are not the same question:
current authorisation, sponsorship now, sponsorship in future, and immigration
ASSISTANCE — an employer can owe E-Verify enrolment or an I-983 training plan
for someone who needs no new visa at all.

No test here asserts an immigration outcome. The point is the opposite: where
the answer depends on facts we do not hold, the product must say so rather than
fill one in.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.intelligence.work_auth import (answer_for, assess_profile,
                                        classify_question)


class _P:
    """Only the fields the assessment reads."""
    def __init__(self, work_authorization="F-1 OPT", ead_end_date="",
                 requires_sponsorship=True, visa_status="",
                 preferred_country="United States", stem_opt=False):
        self.work_authorization = work_authorization
        self.ead_end_date = ead_end_date
        self.requires_sponsorship = requires_sponsorship
        self.visa_status = visa_status
        self.preferred_country = preferred_country
        self.stem_opt = stem_opt


_PAST = (date.today() - timedelta(days=100)).isoformat()
_FUTURE = (date.today() + timedelta(days=200)).isoformat()


# ── the production case ──────────────────────────────────────────────────────

def test_an_expired_end_date_is_not_current_authorization():
    """THE DEFECT. Status says OPT, the date says it ended. The status alone
    used to answer "Yes"."""
    f = assess_profile(_P(ead_end_date=_PAST))
    assert f.validity == "expired"
    assert f.valid_through == _PAST
    assert f.authorized_now is False
    assert f.auth_answer == "Needs your confirmation"
    assert f.review_flag is True
    assert _PAST in f.headline


def test_an_expired_authorization_is_never_auto_answered():
    f = assess_profile(_P(ead_end_date=_PAST))
    answer, needs_review = answer_for("Are you legally authorized to work in the US?", f)
    assert needs_review is True, "an expired authorisation must go to the user"
    assert answer == "Needs your confirmation"


def test_a_missing_end_date_is_unknown_not_current():
    """Not knowing whether authorisation is live is not evidence that it is."""
    f = assess_profile(_P(ead_end_date=""))
    assert f.validity == "unknown"
    assert f.auth_answer == "Needs your confirmation"
    assert answer_for("Are you authorized to work?", f)[1] is True


def test_an_unreadable_end_date_is_unknown_not_current():
    f = assess_profile(_P(ead_end_date="sometime next year"))
    assert f.validity == "unknown"
    assert answer_for("work authorization?", f)[1] is True


def test_a_current_end_date_still_answers_yes():
    """The fix must not make every authorised user unanswerable."""
    f = assess_profile(_P(ead_end_date=_FUTURE))
    assert f.validity == "current"
    assert f.authorized_now is True
    answer, needs_review = answer_for("Are you legally authorized to work in the US?", f)
    assert answer == "Yes" and needs_review is False


@pytest.mark.parametrize("fmt_date", ["%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"])
def test_common_date_formats_are_read(fmt_date):
    from datetime import datetime
    future = datetime.combine(date.today() + timedelta(days=200), datetime.min.time())
    assert assess_profile(_P(ead_end_date=future.strftime(fmt_date))).validity == "current"


# ── a status with no end date is unaffected ──────────────────────────────────

@pytest.mark.parametrize("status", ["U.S. Citizen", "Green Card", "Permanent Resident"])
def test_an_undated_status_is_not_gated_on_a_date(status):
    """A citizen has no EAD end date and must not be asked for one."""
    f = assess_profile(_P(work_authorization=status, ead_end_date="",
                          requires_sponsorship=False))
    assert f.authorized_now is True
    assert f.validity == "not_applicable"
    assert answer_for("Are you authorized to work?", f)[1] is False


# ── an extension is a possibility, not an approval ───────────────────────────

def test_extension_possibility_is_not_approved_authorization():
    """STEM OPT eligibility is a MAY. It must never read as authorisation
    already granted, and it never extends the current end date."""
    f = assess_profile(_P(work_authorization="F-1 OPT", ead_end_date=_PAST,
                          stem_opt=True))
    assert f.extension_possible is True
    assert f.authorized_now is False, "a possible extension does not authorise today"
    assert f.validity == "expired"


# ── four distinct questions ──────────────────────────────────────────────────

@pytest.mark.parametrize("label,expected", [
    ("Are you legally authorized to work in the United States?", "auth_now"),
    ("Do you now or in the future require visa sponsorship?", "future_sponsorship"),
    ("Will you require sponsorship for employment?", "future_sponsorship"),
    ("Will you require any immigration-related assistance?", "immigration_assistance"),
    ("Is the employer required to enroll in E-Verify for you?", "immigration_assistance"),
    ("What is your preferred start date?", "other"),
])
def test_the_four_questions_are_told_apart(label, expected):
    assert classify_question(label) == expected


def test_a_combined_sponsorship_and_assistance_question_is_not_narrowed():
    """"sponsorship OR other immigration assistance" is one question about cost
    and paperwork; reading it as the narrower sponsorship question loses half."""
    assert classify_question(
        "Will you require visa sponsorship or other immigration assistance?"
    ) == "immigration_assistance"


def test_immigration_assistance_is_never_auto_answered():
    """No "always answer No" shortcut. The answer depends on the employer's own
    wording and on obligations that vary by status."""
    f = assess_profile(_P(ead_end_date=_FUTURE))
    answer, needs_review = answer_for("Will you require immigration assistance?", f)
    assert needs_review is True
    assert answer == "Needs your confirmation"
    assert answer.lower() not in ("no", "yes")


def test_future_sponsorship_always_goes_to_the_user():
    """Unchanged, and re-pinned: it depends on plans we do not hold."""
    f = assess_profile(_P(ead_end_date=_FUTURE))
    assert answer_for("Do you now or in the future require sponsorship?", f)[1] is True


def test_an_unrelated_question_is_not_answered_from_work_auth():
    f = assess_profile(_P(ead_end_date=_FUTURE))
    assert answer_for("What is your preferred start date?", f) == ("", False)


# ── the runway is the saved date, not the category's maximum ─────────────────

def test_a_week_of_stem_opt_is_not_sold_as_three_years():
    """AUDIT 2026-09-25 (finding 6). A STEM OPT profile whose end date was seven
    days away still produced a selling point promising "up to 3 years"."""
    soon = (date.today() + timedelta(days=7)).isoformat()
    f = assess_profile(_P(work_authorization="F-1 STEM OPT", ead_end_date=soon))
    assert f.validity == "current"
    assert "3 years" not in f.selling_point and "3 years" not in f.headline
    assert f.selling_point == "", "a week of runway offers no line to say to employers"
    assert f.review_flag is True
    assert soon in f.headline and "7 days" in f.headline


def test_a_long_runway_is_stated_from_the_date():
    far = (date.today() + timedelta(days=400)).isoformat()
    f = assess_profile(_P(work_authorization="F-1 STEM OPT", ead_end_date=far))
    assert far in f.selling_point
    assert "3 years" not in f.selling_point
    # The employer's obligations stay stated, never waved away.
    assert "E-Verify" in f.selling_point and "I-983" in f.selling_point
    # And the future-sponsorship question still goes to the user.
    assert answer_for("Do you now or in the future require sponsorship?", f)[1] is True


def test_an_undated_status_offers_no_runway_claim():
    """Unknown validity used to keep the optimistic selling point."""
    f = assess_profile(_P(work_authorization="F-1 STEM OPT", ead_end_date=""))
    assert f.validity == "unknown"
    assert f.selling_point == ""
    assert "3 years" not in f.headline


# ── the future-sponsorship question is never answered "No" on a dated status ─

@pytest.mark.parametrize("status,requires,expected", [
    ("F-1 OPT", False, None),          # the audit's contradictory live profile
    ("F-1 STEM OPT", False, None),
    ("H-1B", False, None),
    ("F-1 OPT", True, True),           # a truthful Yes never bypasses screening
    ("U.S. Citizen", False, False),
    ("Green Card", False, False),
    ("", False, None),                 # nothing saved: the user answers
])
def test_the_fill_pack_sends_only_a_certain_sponsorship_answer(status, requires, expected):
    from app.api.server import _sponsorship_answer_for_pack
    p = _P(work_authorization=status, requires_sponsorship=requires,
           ead_end_date=_FUTURE if "OPT" in status or "H-1B" in status else "")
    assert _sponsorship_answer_for_pack(p) is expected


def test_the_answer_pack_does_not_pre_answer_no_for_opt():
    from app.autofill.answer_pack import _sponsorship_text
    assert _sponsorship_text(_P(work_authorization="F-1 OPT", requires_sponsorship=False,
                                ead_end_date=_FUTURE)) == "Needs your confirmation"
    assert _sponsorship_text(_P(work_authorization="U.S. Citizen", requires_sponsorship=False)) == "No"


def test_the_extension_leaves_an_unknown_sponsorship_answer_blank():
    """Structural: the MV3 extension must not turn a missing answer into "No"."""
    from pathlib import Path
    js = Path("extension/content.js").read_text(encoding="utf-8")
    assert "sponsorKnown ? requires : null" in js
    assert "if (asksSponsor && answer === null) return false;" in js
