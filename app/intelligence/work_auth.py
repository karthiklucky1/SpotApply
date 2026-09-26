"""Work-Authorization Framing Engine — the LEGAL version of "no sponsorship needed".

The point: most employers reject visa candidates out of *ignorance*, not policy.
A candidate on F-1 OPT / STEM OPT is authorized to work for up to 3 years with
ZERO cost or paperwork from the employer. Surfacing that truth — and answering
each application question with the strongest *honest* phrasing — is a real edge.

Hard rule we never cross: we do NOT tell a user to claim they will never need
sponsorship when they will. "Are you authorized to work now?" → truthful Yes for
OPT. "Will you require sponsorship in the future?" → truthful answer based on the
user's actual status, and we FLAG that question for the user instead of auto-
answering it. That protects the user from offer rescission / falsification.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkAuthFraming:
    authorized_now: bool          # legally allowed to start work without new filing?
    basis: str                    # e.g. "F-1 STEM OPT", "U.S. Citizen"
    employer_cost_now: bool       # must the employer pay/file anything to start you now?
    needs_future_sponsorship: bool
    headline: str                 # UI-ready, always truthful
    auth_answer: str              # answer to "Are you authorized to work in the US?"
    future_sponsorship_answer: str  # truthful answer to "will you need sponsorship now/future?"
    review_flag: bool             # true → user should answer the future-sponsorship Q themselves
    selling_point: str            # one line the user can say to de-risk themselves to an employer
    # ── validity, kept SEPARATE from the status ─────────────────────────────
    # A status and a date are different facts. "F-1 OPT" says what the
    # authorisation IS; `valid_through` says whether it is still live. Before
    # these were separate, `assess_profile` read only the status STRING, so a
    # profile whose EAD ended months ago still auto-answered "Yes, authorized to
    # work" on application forms with no review — a legal answer inferred from a
    # stale field. `ead_end_date` was not consulted anywhere in this module.
    validity: str = "not_applicable"   # current | expired | unknown | not_applicable
    valid_through: str = ""            # the date as the profile holds it
    extension_possible: bool = False   # MAY be eligible — never "already approved"


def _blob(profile) -> str:
    wa = (getattr(profile, "work_authorization", "") or "")
    vs = (getattr(profile, "visa_status", "") or "")
    return f"{wa} {vs}".lower()


# Statuses whose authorisation is DATED: the document carries an end date, so
# the status alone does not establish that work is authorised today.
_DATED_STATUSES = ("opt", "stem opt", "stem-opt", "f-1", "f1", "ead", "h-1b",
                   "h1b", "h1-b", "l-1", "l1", "tn", "j-1", "j1", "cpt")


def _parse_end(raw: str):
    """The end date as a `date`, or None when the free-text field is unreadable."""
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime((raw or "")[:10], fmt).date()
        except ValueError:
            continue
    return None


def _validity(profile) -> tuple[str, str]:
    """('current'|'expired'|'unknown', the date as stored).

    Parsed leniently: the field is free text and a value we cannot read is
    UNKNOWN, never "current". Not knowing whether authorisation is live is not
    evidence that it is.
    """
    raw = (getattr(profile, "ead_end_date", "") or "").strip()
    if not raw:
        return "unknown", ""
    from datetime import date
    end = _parse_end(raw)
    if end is None:
        return "unknown", raw
    return ("current" if end >= date.today() else "expired"), raw


#: Below this many days of remaining authorisation the framing stops offering a
#: line to say to an employer and asks the user to confirm their status first:
#: a hiring process routinely outlasts it, and "I can start immediately" from
#: someone whose card ends next week is a promise the document cannot keep.
SHORT_RUNWAY_DAYS = 90


def _runway_text(days: int) -> str:
    """'about 7 months' / '23 days'. Never rounded up."""
    if days < 60:
        return f"{days} day{'s' if days != 1 else ''}"
    months = days // 30
    return f"about {months} month{'s' if months != 1 else ''}"


def remaining_days(profile) -> "int | None":
    """Days of authorisation left from the saved end date, or None when the
    date is absent/unreadable. Negative when it has passed."""
    from datetime import date
    end = _parse_end((getattr(profile, "ead_end_date", "") or "").strip())
    return None if end is None else (end - date.today()).days


def _apply_validity(framing: "WorkAuthFraming", profile) -> "WorkAuthFraming":
    """Gate a DATED authorisation on its own end date.

    This is the difference between "what status do they hold" and "may we state
    on an employer's form that they are authorised today". Only the first is in
    the status string.
    """
    import dataclasses
    blob = _blob(profile)
    if not any(k in blob for k in _DATED_STATUSES):
        return framing                      # citizen / green card: no end date
    state, raw = _validity(profile)
    framing = dataclasses.replace(framing, validity=state, valid_through=raw,
                                  extension_possible=framing.needs_future_sponsorship)
    if state == "current":
        # The runway is the SAVED date, not the category's maximum. The STEM
        # OPT framing used to promise "up to 3 years" to a profile whose card
        # ended in seven days — true of the category, false of this person, and
        # the selling point is pasted straight to employers.
        days = remaining_days(profile)
        if days is None:            # cannot happen for "current"; stay safe
            return framing
        through = f"through {raw} ({_runway_text(days)} remaining)"
        if days < SHORT_RUNWAY_DAYS:
            return dataclasses.replace(
                framing,
                headline=(f"⚠️ Your {framing.basis} authorisation runs {through}. "
                          "Confirm your status (and any extension you have actually "
                          "filed) before telling an employer how long you can work."),
                review_flag=True,
                selling_point="")
        return dataclasses.replace(
            framing,
            headline=f"✅ Authorized via {framing.basis} {through}. " + _obligations(framing),
            selling_point=(f"I'm currently authorized to work on {framing.basis} {through}. "
                           + _obligations(framing)).strip())
    if state == "expired":
        return dataclasses.replace(
            framing,
            authorized_now=False,
            headline=(f"⚠️ Your {framing.basis} end date ({raw}) has passed. "
                      "Confirm your current status before applying — this is not "
                      "a question SpotApply can answer for you."),
            auth_answer="Needs your confirmation",
            future_sponsorship_answer="Needs your confirmation",
            review_flag=True,
            selling_point="")
    # UNKNOWN — no readable end date. We can say what the status IS, never how
    # long it lasts: the category's maximum is not this person's runway, so the
    # line offered to employers goes too.
    return dataclasses.replace(
        framing,
        headline=(f"{framing.basis} — add your authorisation end date in Profile "
                  "so application answers can be filled with confidence."),
        auth_answer="Needs your confirmation",
        review_flag=True,
        selling_point="")


def _obligations(framing: "WorkAuthFraming") -> str:
    """What an employer must do for this status — stated, never waved away."""
    basis = (framing.basis or "").lower()
    if "stem opt" in basis:
        return ("It needs an E-Verify-enrolled employer and a signed Form I-983 "
                "training plan; H-1B sponsorship would be needed later.")
    if "opt" in basis:
        return "No employer filing is needed to start; sponsorship would be needed later."
    if "h-1b" in basis:
        return "A new employer files an H-1B transfer, which is not subject to the lottery."
    return ""


def assess_profile(profile) -> WorkAuthFraming:
    """Map a UserProfile's work-authorization to a truthful framing.

    The status is read from the profile; whether that status is still LIVE is a
    separate question answered from its end date (`_apply_validity`).
    """
    return _apply_validity(_assess_status(profile), profile) if profile is not None \
        else _assess_status(profile)


def _assess_status(profile) -> WorkAuthFraming:
    """The status framing, before any validity gate."""
    if profile is None:
        return WorkAuthFraming(
            True, "Work authorization not set", False, False,
            "Set your work authorization in Profile to unlock visa-fit guidance.",
            "—", "—", True, "",
        )

    blob = _blob(profile)
    requires = bool(getattr(profile, "requires_sponsorship", False))

    # Country-aware framing: a Berlin or London user must never be told (or have
    # application answers pre-filled saying) they are "authorized to work in the
    # U.S." US-visa branches below still fire on explicit US statuses; only the
    # generic/default wording adapts to the user's own country.
    _country = ""
    try:
        from app.common.geo import norm_country
        _country = norm_country(getattr(profile, "preferred_country", "") or "")
    except Exception:
        pass
    _non_us = bool(_country) and _country != "united states"
    _place = _country.title() if _non_us else "the U.S."

    def has(*keys) -> bool:
        return any(k in blob for k in keys)

    # Fully authorized, never needs sponsorship
    if has("citizen", "u.s. citizen", "us citizen"):
        return WorkAuthFraming(
            True, "U.S. Citizen", False, False,
            "✅ U.S. Citizen — fully authorized, no sponsorship ever required.",
            "Yes", "No", False,
            "No work authorization or sponsorship needed at any point.",
        )
    if has("green card", "permanent resident", "lawful permanent", "lpr"):
        return WorkAuthFraming(
            True, "Permanent Resident (Green Card)", False, False,
            "✅ Green Card holder — fully authorized, no sponsorship required.",
            "Yes", "No", False,
            "Authorized to work permanently with no employer sponsorship.",
        )

    # STEM OPT — strong "no petition, no lottery" story, but NOT a no-paperwork
    # one. STEM OPT has two hard employer obligations we must not paper over:
    # the employer must be enrolled in E-Verify, and it must sign a Form I-983
    # training plan. Claiming "zero cost or filing required" (as this did) is
    # false, and the user pastes this line straight to employers — so it set
    # them up to be corrected by the first recruiter who knows the rule.
    # See docs/research/hiring-machine-2026-08.md §1.7.
    if has("stem opt", "stem-opt"):
        return WorkAuthFraming(
            True, "F-1 STEM OPT", False, True,
            "✅ Authorized via STEM OPT for up to 3 years — no petition, no filing "
            "fee and no lottery. The employer must be enrolled in E-Verify and "
            "sign a Form I-983 training plan (H-1B sponsorship needed later).",
            "Yes", "Yes — in the future, after my OPT period", True,
            "I can start immediately and work up to 3 years on STEM OPT — no "
            "H-1B petition, filing fee or lottery involved. It does need an "
            "E-Verify-enrolled employer and a signed I-983 training plan.",
        )
    if has("opt", "f-1", "f1"):
        return WorkAuthFraming(
            True, "F-1 OPT", False, True,
            "✅ Authorized via F-1 OPT — no cost or filing from the employer now. "
            "STEM degrees can extend this to 3 years; H-1B needed afterward.",
            "Yes", "Yes — in the future, after my OPT period", True,
            "I'm work-authorized now on OPT at no cost to you; we can plan H-1B later.",
        )

    # H-1B already held — a new employer files a transfer (no lottery, fast start)
    if has("h-1b", "h1b", "h1-b"):
        return WorkAuthFraming(
            True, "H-1B", True, True,
            "✅ On H-1B — authorized to work; a new employer files a transfer "
            "(no lottery, can typically start within weeks).",
            "Yes", "Yes — via an H-1B transfer (no lottery required)", True,
            "An H-1B transfer is not subject to the lottery and lets me start fast.",
        )

    # Other employment-authorized categories
    if has("tn ", " tn", "e-3", "e3", "h-4 ead", "h4 ead", "l-2", "l2 ead", "ead"):
        cat = (getattr(profile, "visa_status", "") or getattr(profile, "work_authorization", "") or "Work-authorized")
        return WorkAuthFraming(
            True, cat, False, requires,
            "✅ Currently work-authorized — exact terms depend on your category; "
            "confirm per role.",
            "Yes", "Depends on my category — I'll confirm per role", True,
            "I'm currently authorized to work in the U.S.",
        )

    # Explicitly requires sponsorship (no current authorization)
    if requires:
        if _non_us:
            # H-1B/cap-exempt framing is meaningless outside the US.
            return WorkAuthFraming(
                False, "Requires visa sponsorship", True, True,
                f"Requires visa sponsorship — SpotApply prioritizes employers known "
                f"to sponsor work visas in {_place}.",
                "Not yet — I would require sponsorship", "Yes", True,
                f"I'm targeting employers that sponsor work visas in {_place}.",
            )
        return WorkAuthFraming(
            False, "Requires visa sponsorship", True, True,
            "Requires visa sponsorship — JobAgent prioritizes sponsor-friendly and "
            "cap-exempt (no-lottery) employers for you.",
            "Not yet — I would require sponsorship", "Yes", True,
            "I'm targeting employers that sponsor; cap-exempt roles need no lottery.",
        )

    # Default: assume authorized in the USER'S country, no sponsorship implied
    wa = (getattr(profile, "work_authorization", "") or "Authorized to work")
    return WorkAuthFraming(
        True, wa, False, False,
        f"✅ Authorized to work in {_place}.",
        "Yes", "No", False,
        f"Authorized to work in {_place}.",
    )


# Question classification used by the answer pack so we never auto-answer a
# future-sponsorship question, but always give the strongest truthful auth answer.
_AUTH_NOW_HINTS = (
    "authorized to work", "legally authorized", "work authorization",
    "eligible to work", "right to work",
)
_FUTURE_SPONSOR_HINTS = (
    "require sponsorship", "need sponsorship", "now or in the future",
    "future require", "visa sponsorship", "require visa", "sponsorship now or",
)
# A FOURTH question, distinct from the other three. "Will you require any
# immigration-related assistance" is not "are you authorised now" and not "will
# you need sponsorship": an employer can owe E-Verify enrolment, an I-983
# training plan or a transfer filing for someone who needs no new visa at all.
# Answering it from the sponsorship answer would be wrong in both directions.
_ASSISTANCE_HINTS = (
    "immigration assistance", "immigration-related", "immigration support",
    "relocation or immigration", "any assistance with immigration",
    "e-verify", "i-983", "training plan",
)


def classify_question(label: str) -> str:
    """'auth_now' | 'future_sponsorship' | 'immigration_assistance' | 'other'.

    Four distinct questions, and the wording decides which. Assistance is tested
    BEFORE sponsorship: "will you require visa sponsorship or other immigration
    assistance" is one question an employer asks about cost and paperwork, and
    reading it as the narrower sponsorship question loses the rest of it.
    """
    lab = (label or "").lower()
    if any(h in lab for h in _ASSISTANCE_HINTS):
        return "immigration_assistance"
    if any(h in lab for h in _FUTURE_SPONSOR_HINTS):
        return "future_sponsorship"
    if any(h in lab for h in _AUTH_NOW_HINTS):
        return "auth_now"
    return "other"


def answer_for(label: str, framing: WorkAuthFraming) -> tuple[str, bool]:
    """(answer, needs_user_review) for a work-auth question — never auto-lies.

    `auth_now` is auto-answered ONLY when the authorisation is currently valid.
    It used to be auto-answered from the status string alone, so an expired or
    undated authorisation still filled in "Yes" with no review — a legal
    assertion made on someone's behalf from a field nobody had checked.
    """
    kind = classify_question(label)
    if kind == "auth_now":
        needs_review = framing.validity in ("expired", "unknown") or not framing.authorized_now
        return framing.auth_answer, needs_review
    if kind == "future_sponsorship":
        return framing.future_sponsorship_answer, True
    if kind == "immigration_assistance":
        # Never auto-answered. It depends on the employer's own wording and on
        # obligations (E-Verify, I-983) that vary by status, and a confident
        # "No" here is exactly the shortcut this must not take.
        return "Needs your confirmation", True
    return "", False
