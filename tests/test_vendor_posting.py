"""Guard: staffing-vendor detection must be accurate in BOTH directions.

Two failure modes matter and they pull against each other:

  * Missing a vendor posting leaves an F-1 user unwarned about the I-983 /
    client-site problem, which is the highest-consequence gap in the product.
  * Crying "vendor" on an ordinary direct-employer job trains users to ignore
    the badge, which costs us the first failure mode too.

Staffing is a legitimate industry (~75% of the Fortune 1000 buy contingent
labour this way). The badge is descriptive, never an accusation — the separate
`red_flags` axis carries misconduct.

See docs/research/hiring-machine-2026-08.md §1.7.
"""
from types import SimpleNamespace

import pytest

from app.intelligence.vendor_posting import assess


def _job(company="Acme Corp", title="Software Engineer", description=""):
    return SimpleNamespace(company=company, title=title, description=description)


def _profile(work_authorization="", stem_opt=False, visa_status=""):
    return SimpleNamespace(
        work_authorization=work_authorization, stem_opt=stem_opt, visa_status=visa_status
    )


# ── True positives ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "description",
    [
        "Our client, a leading financial services firm, is seeking a Java Developer.",
        "We are seeking candidates for our client in Charlotte, NC. 12 month contract.",
        "Position is with one of our Fortune 500 clients. Rate: $65/hr on W2.",
        "Contract-to-hire role. C2C and W2 candidates welcome. End client is a major retailer.",
        "Looking for GC/USC only. Corp to corp acceptable. $70 per hour.",
    ],
)
def test_detects_vendor_postings(description):
    a = assess(_job(description=description))
    assert a.is_vendor_posting, f"missed vendor posting: {description!r} (signals={a.signals})"
    assert a.label
    assert a.checklist, "a vendor posting must carry the before-you-apply checklist"


def test_unnamed_client_alone_is_sufficient():
    """Refusing to name the client is the single strongest tell in the guide."""
    a = assess(_job(description="Our client is a large healthcare system in the Midwest."))
    assert a.is_vendor_posting
    assert "unnamed_client" in a.signals


# ── True negatives: ordinary direct-employer postings ────────────────────────

@pytest.mark.parametrize(
    "company,description",
    [
        ("Stripe", "Join the Payments team building APIs used by millions. Full-time, salaried."),
        ("Anthropic", "We are hiring a research engineer. Competitive salary and equity."),
        # "no sponsorship" is a lawful statement by a DIRECT employer — sponsorship.py
        # owns that, and it must not read as a vendor signal here.
        ("Datadog", "This role is not eligible for visa sponsorship now or in the future."),
        # A product company whose NAME matches the agency-name shape must not be
        # flagged on the name alone.
        ("Palantir Technologies", "Forward Deployed Engineer. Salary range $150,000-$200,000."),
        ("Acme Talent Systems Inc", "We build developer tools. Full-time salaried position."),
    ],
)
def test_does_not_flag_direct_employer_postings(company, description):
    a = assess(_job(company=company, description=description))
    assert not a.is_vendor_posting, f"false positive on {company!r} (signals={a.signals})"
    assert a.label == ""
    assert a.work_auth_caution is None


def test_single_weak_signal_is_not_enough():
    """An hourly rate alone (contractors exist at direct employers too) must not trip it."""
    a = assess(_job(company="Stripe", description="Part-time role, $55/hr, on our internal tools team."))
    assert not a.is_vendor_posting


# ── The F-1 caution, which is the whole point for our core user ──────────────

def test_stem_opt_caution_only_for_f1_users_on_vendor_postings():
    vendor = _job(description="Our client, a major bank, needs a data engineer. W2 or C2C.")

    on_opt = assess(vendor, _profile(work_authorization="F-1 OPT"))
    assert on_opt.work_auth_caution is not None
    assert "I-983" in on_opt.work_auth_caution
    assert "E-Verify" in on_opt.work_auth_caution

    citizen = assess(vendor, _profile(work_authorization="US Citizen"))
    assert citizen.work_auth_caution is None, "citizens should not get the STEM OPT caution"

    no_profile = assess(vendor)
    assert no_profile.work_auth_caution is None


def test_stem_opt_flag_alone_triggers_the_caution():
    a = assess(
        _job(description="Our client is a hospital network. Contract to hire."),
        _profile(work_authorization="", stem_opt=True),
    )
    assert a.work_auth_caution is not None


def test_no_caution_on_a_direct_employer_even_for_f1_users():
    a = assess(_job(company="Stripe", description="Full-time engineer, salaried."), _profile("F-1 OPT"))
    assert a.work_auth_caution is None


# ── Red flags are a separate, harsher axis ───────────────────────────────────

@pytest.mark.parametrize(
    "description,expected",
    [
        ("A one-time processing fee is required before we submit you.", "asks_for_money"),
        ("Please send your I-20 and passport copy to proceed.", "documents_before_offer"),
        ("Interview will be conducted via WhatsApp.", "chat_only_interview"),
        ("We can adjust your resume to add a few years of experience.", "resume_modification"),
    ],
)
def test_detects_red_flags(description, expected):
    a = assess(_job(description=description))
    assert expected in a.red_flags, f"missed {expected} in {description!r}"
    assert a.red_flag_notes
    assert a.label == "Red flags"


def test_clean_posting_has_no_red_flags():
    a = assess(_job(company="Stripe", description="Great team, salaried role, apply on our site."))
    assert a.red_flags == []


def test_vendor_without_misconduct_is_not_called_a_red_flag():
    """The load-bearing distinction: a normal vendor post is not an accusation."""
    a = assess(_job(description="Our client, a retailer, seeks a QA engineer. 6 month contract, W2."))
    assert a.is_vendor_posting
    assert a.red_flags == []
    assert a.label == "Staffing vendor"
    assert "legitimate" in a.summary.lower()


# ── Shape ────────────────────────────────────────────────────────────────────

def test_payload_is_json_safe():
    import json

    json.dumps(assess(_job(description="Our client needs a dev. C2C ok."), _profile("OPT")).as_dict())


def test_handles_empty_and_missing_fields():
    for job in (_job(), _job(company="", title="", description=""), SimpleNamespace()):
        a = assess(job)
        assert a.is_vendor_posting is False
        assert 0 <= a.score <= 100
