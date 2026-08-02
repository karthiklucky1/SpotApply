import pytest
from app.qa_store.resolver import QAResolver
from app.db.models import Job

def test_qa_resolver_identity():
    resolver = QAResolver()
    
    # Test linkedin
    ans, conf = resolver.resolve("LinkedIn Profile")
    assert ans == "https://www.linkedin.com/in/amruthaluri/"
    assert conf >= 0.95
    
    # Test full name
    ans, conf = resolver.resolve("Please enter your full name:")
    assert ans == "Karthik Amruthaluri"
    assert conf >= 0.95

def test_qa_resolver_work_auth():
    resolver = QAResolver()
    
    ans, conf = resolver.resolve("Are you legally authorized to work in the United States?")
    assert ans == "Yes"
    assert conf >= 0.95


def test_qa_resolver_never_auto_answers_sponsorship():
    """The future-sponsorship knockout must always route to the human.

    This is the ONE question that genuinely auto-rejects in Greenhouse/Ashby, and
    its truthful answer depends on the applicant's real plans. Answering it
    falsely is wilful misrepresentation under INA 212(a)(6)(C)(i) — a permanent
    inadmissibility bar — and the standard is wilful misrepresentation by the
    APPLICANT, so an automated wrong answer is the applicant's problem.

    This test previously asserted the opposite (that we answer "No" at 0.95
    confidence, from a stored default explicitly set to false "to avoid
    auto-filtering"). Do not restore that behaviour.
    """
    resolver = QAResolver()

    for phrasing in (
        "Will you now or in the future require visa sponsorship?",
        "Do you require sponsorship for employment visa status?",
        "Will you require employer sponsorship now or in the future?",
        "Do you need sponsorship to work in the US?",
        "Visa sponsorship required? (Yes/No)",
        # COMPOUND phrasings: these also match the work-authorization keywords,
        # and answering them from authorized_to_work_us returns "Yes" — which is
        # FALSE for an OPT holder, who is authorized now but not without future
        # sponsorship. The sponsorship guard must therefore win over the
        # work-auth branch, not run after it.
        "Are you legally authorized to work in the United States without sponsorship?",
        "Are you authorized to work for any employer in the US without visa sponsorship?",
        "Do you now or in the future require sponsorship to maintain work authorization?",
        "Are you able to work in the US without requiring sponsorship now or in the future?",
    ):
        ans, conf = resolver.resolve(phrasing)
        assert ans is None, f"auto-answered sponsorship question: {phrasing!r} -> {ans!r}"
        assert conf == 0.0, f"non-zero confidence on {phrasing!r}"


def test_qa_resolver_still_answers_plain_work_authorization():
    """The guard must not swallow the plain authorisation question.

    "Are you legally authorized to work in the US?" has a clean truthful answer
    for someone on OPT — Yes — and stays auto-answered. Only questions that
    mention sponsorship route to the human.
    """
    resolver = QAResolver()

    for phrasing in (
        "Are you legally authorized to work in the United States?",
        "Do you have the legal right to work in the US?",
        "Are you eligible to work in the United States?",
    ):
        ans, conf = resolver.resolve(phrasing)
        assert ans == "Yes", f"{phrasing!r} -> {ans!r}"
        assert conf >= 0.95

def test_qa_resolver_always_ask_human():
    resolver = QAResolver()
    
    ans, conf = resolver.resolve("Did you use AI to fill this application?")
    assert ans is None
    assert conf == 0.0

def test_qa_resolver_safety_checks():
    resolver = QAResolver()
    
    # Criminal record
    ans, conf = resolver.resolve("Have you ever been convicted of a felony?")
    assert ans == "No"
    assert conf >= 0.95

    # Non-compete
    ans, conf = resolver.resolve("Are you subject to a non-compete agreement?")
    assert ans == "No"
    assert conf >= 0.95

def test_qa_resolver_unknown_yes_no():
    resolver = QAResolver()
    
    # Unknown yes/no should yield low confidence (0.0) so it routes to Telegram
    ans, conf = resolver.resolve("Do you like pineapples on pizza?")
    assert ans is None
    assert conf < 0.7

def test_qa_resolver_education():
    resolver = QAResolver()
    
    # Test university
    ans, conf = resolver.resolve("What university did you attend?")
    assert ans == "University of Cincinnati"
    assert conf >= 0.95
    
    # Test graduation date
    ans, conf = resolver.resolve("Date of Graduation")
    assert ans == "April 30, 2026"
    assert conf >= 0.95

    # Test degree
    ans, conf = resolver.resolve("Please state your highest degree:")
    assert ans == "Master of Engineering"
    assert conf >= 0.95
