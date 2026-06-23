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


def _blob(profile) -> str:
    wa = (getattr(profile, "work_authorization", "") or "")
    vs = (getattr(profile, "visa_status", "") or "")
    return f"{wa} {vs}".lower()


def assess_profile(profile) -> WorkAuthFraming:
    """Map a UserProfile's work-authorization to a truthful framing."""
    if profile is None:
        return WorkAuthFraming(
            True, "Work authorization not set", False, False,
            "Set your work authorization in Profile to unlock visa-fit guidance.",
            "—", "—", True, "",
        )

    blob = _blob(profile)
    requires = bool(getattr(profile, "requires_sponsorship", False))

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

    # STEM OPT — the strongest "no cost to employer now" story
    if has("stem opt", "stem-opt"):
        return WorkAuthFraming(
            True, "F-1 STEM OPT", False, True,
            "✅ Authorized via STEM OPT for up to 3 years — zero cost or filing "
            "required from the employer right now (H-1B sponsorship needed later).",
            "Yes", "Yes — in the future, after my OPT period", True,
            "I can start immediately and work up to 3 years on STEM OPT — "
            "no paperwork, cost, or sponsorship required from you now.",
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
        return WorkAuthFraming(
            False, "Requires visa sponsorship", True, True,
            "Requires visa sponsorship — JobAgent prioritizes sponsor-friendly and "
            "cap-exempt (no-lottery) employers for you.",
            "Not yet — I would require sponsorship", "Yes", True,
            "I'm targeting employers that sponsor; cap-exempt roles need no lottery.",
        )

    # Default: assume authorized, no sponsorship implied
    wa = (getattr(profile, "work_authorization", "") or "Authorized to work")
    return WorkAuthFraming(
        True, wa, False, False,
        "✅ Authorized to work in the U.S.",
        "Yes", "No", False,
        "Authorized to work in the U.S.",
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


def classify_question(label: str) -> str:
    """Return 'auth_now', 'future_sponsorship', or 'other' for an application Q."""
    lab = (label or "").lower()
    if any(h in lab for h in _FUTURE_SPONSOR_HINTS):
        return "future_sponsorship"
    if any(h in lab for h in _AUTH_NOW_HINTS):
        return "auth_now"
    return "other"


def answer_for(label: str, framing: WorkAuthFraming) -> tuple[str, bool]:
    """(answer, needs_user_review) for a work-auth question — never auto-lies."""
    kind = classify_question(label)
    if kind == "auth_now":
        return framing.auth_answer, False
    if kind == "future_sponsorship":
        return framing.future_sponsorship_answer, True
    return "", False
