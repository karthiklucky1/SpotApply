"""H-1B cap-season calendar — where you are in the lottery year, today.

For a cap-subject international candidate, the H-1B cycle is the clock that
actually governs the job search, and it is almost entirely fixed:

    late Jan – Feb   USCIS announces the registration period
    ~first 3 weeks   electronic registration window (March)
    by Mar 31        selection and notification
    Apr 1            petition filing window opens (90 days)
    Oct 1            employment start date

The consequence that changes behaviour: an offer landing in April cannot put you
into cap-subject H-1B status until **October of the following year** — you
missed March's registration by weeks. Knowing that in October, when you still
have runway, is worth far more than discovering it in April.

Cap-EXEMPT employers (universities, affiliated non-profits and hospitals,
non-profit and governmental research organisations) are outside all of this
under INA 214(g)(5): they file year-round with no lottery. That is why this
module always points at the cap-exempt route when the cap-subject calendar is
unhelpful — see app/intelligence/sponsorship.py for how we detect them.

Deliberately pure: no DB, no LLM, no network, no I/O. Dates in, facts out.

ACCURACY NOTE: the March registration window is announced by USCIS each year and
its exact dates move (the FY2027 window ran through 31 Mar 2026, with the cap
reached 17 Jul 2026). Everything here is the *typical* schedule and is flagged
approximate in the payload; it is scheduling context, never legal advice, and
users are pointed at uscis.gov to confirm.

See docs/research/hiring-machine-2026-08.md §1.6.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date

# Typical schedule. Month/day pairs — see the accuracy note above.
_REGISTRATION_OPENS = (3, 1)    # electronic registration, ~first 3 weeks of March
_REGISTRATION_CLOSES = (3, 21)
_SELECTION_BY = (3, 31)
_FILING_OPENS = (4, 1)          # 90-day petition window
_EMPLOYMENT_STARTS = (10, 1)

USCIS_SOURCE_URL = "https://www.uscis.gov/h-1b-electronic-registration-process"


@dataclass
class H1BPhase:
    phase: str                       # machine key, stable for callers
    label: str                       # short badge text
    message: str                     # one honest sentence for the UI
    registration_opens: str          # ISO date of the next registration window
    days_to_registration: int        # 0 when the window is open now
    next_employment_start: str       # ISO date of the earliest reachable Oct 1
    is_approximate: bool = True
    source_url: str = USCIS_SOURCE_URL

    def as_dict(self) -> dict:
        return asdict(self)


def _at(year: int, md: tuple[int, int]) -> date:
    return date(year, md[0], md[1])


def phase(today: date | None = None) -> H1BPhase:
    """Where `today` sits in the H-1B cap year, and what it implies.

    The rule tying the two dates together: registering in March of year Y is
    what makes an Oct 1 of year Y start possible. Miss March and the earliest
    cap-subject start slips a full year.
    """
    today = today or date.today()
    y = today.year

    reg_open, reg_close = _at(y, _REGISTRATION_OPENS), _at(y, _REGISTRATION_CLOSES)
    selection_by, filing_opens = _at(y, _SELECTION_BY), _at(y, _FILING_OPENS)
    start_this_cycle = _at(y, _EMPLOYMENT_STARTS)

    if today < reg_open:
        # Jan–Feb: the window is imminent. This is the highest-leverage moment —
        # an employer still has time to register you for an Oct 1 start this year.
        return H1BPhase(
            phase="pre_registration",
            label="H-1B registration opens soon",
            message=(
                f"H-1B registration usually opens in early March — about "
                f"{(reg_open - today).days} days away. An employer who registers you "
                f"this March could start you on {start_this_cycle.isoformat()}. "
                f"If you are close to an offer, raise it now."
            ),
            registration_opens=reg_open.isoformat(),
            days_to_registration=(reg_open - today).days,
            next_employment_start=start_this_cycle.isoformat(),
        )

    if today <= reg_close:
        return H1BPhase(
            phase="registration_open",
            label="H-1B registration open now",
            message=(
                "The H-1B electronic registration window is open (typically the first "
                f"three weeks of March). An employer registering you now is playing for "
                f"an {start_this_cycle.isoformat()} start. Confirm exact dates on uscis.gov."
            ),
            registration_opens=reg_open.isoformat(),
            days_to_registration=0,
            next_employment_start=start_this_cycle.isoformat(),
        )

    if today <= selection_by:
        return H1BPhase(
            phase="selection",
            label="H-1B selection in progress",
            message=(
                "Registration has closed; USCIS notifies selections by 31 March. "
                f"Selected registrations can file from {filing_opens.isoformat()} for an "
                f"{start_this_cycle.isoformat()} start."
            ),
            registration_opens=_at(y + 1, _REGISTRATION_OPENS).isoformat(),
            days_to_registration=(_at(y + 1, _REGISTRATION_OPENS) - today).days,
            next_employment_start=start_this_cycle.isoformat(),
        )

    # April onward: this year's registration is gone. A new cap-subject offer now
    # cannot reach H-1B status until Oct 1 of NEXT year. This is the fact worth
    # surfacing loudest, because it reframes what a candidate should target.
    next_reg = _at(y + 1, _REGISTRATION_OPENS)
    next_start = _at(y + 1, _EMPLOYMENT_STARTS)

    if today < start_this_cycle:
        return H1BPhase(
            phase="petition_filing",
            label="H-1B filing window",
            message=(
                "Selected petitions are being filed now for an "
                f"{start_this_cycle.isoformat()} start. This year's registration has "
                f"closed, so a new cap-subject employer could not start you until "
                f"{next_start.isoformat()}. Cap-exempt employers (universities, "
                "non-profit research, hospitals) can still file year-round with no lottery."
            ),
            registration_opens=next_reg.isoformat(),
            days_to_registration=(next_reg - today).days,
            next_employment_start=next_start.isoformat(),
        )

    return H1BPhase(
        phase="between_cycles",
        label="Next H-1B registration is March",
        message=(
            f"The next H-1B registration window is around {next_reg.isoformat()}, for an "
            f"{next_start.isoformat()} start — so a cap-subject offer signed now still "
            f"waits on March. Cap-exempt employers (universities, non-profit research, "
            "hospitals) file year-round with no lottery and are the faster route from here."
        ),
        registration_opens=next_reg.isoformat(),
        days_to_registration=(next_reg - today).days,
        next_employment_start=next_start.isoformat(),
    )
