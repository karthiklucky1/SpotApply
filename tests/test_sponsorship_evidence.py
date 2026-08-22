"""Sponsorship evidence: provenance, contradictions, and the re-stamp path.

Three classes of bug are pinned here, all found in the Aug 2026 review:

1. REFUSAL FALSE POSITIVES. Detection was `phrase in description`, so
   "there is no sponsorship requirement for this role" contained "no
   sponsorship" and `rule_filter` HARD-BLOCKED a job the user was fully
   eligible for — before scoring, so it never reached the board.

2. SUPPRESSED CONTRADICTIONS. A posting that refuses silently erased the
   employer's USCIS record, and a cap-exempt employer silently erased the
   posting's refusal (returning `explicitly_refuses=False` while the posting
   plainly refused). Both facts are true; the user gets both.

3. WRITE-ONCE FACETS. `_upsert` stamps the verdict on INSERT and `backfill`
   only ever filled NULLs, so no ingested dataset could change an existing
   card. Uploading USCIS data updated nothing visible.
"""
from __future__ import annotations

import json

import pytest

from app.common.sponsorship_text import find_refusal, refuses
from app.intelligence import h1b_data
from app.intelligence.sponsorship import SponsorshipLikelihood, assess


@pytest.fixture(autouse=True)
def _fresh_state():
    """Wipe the sponsor registry around each test — assess() reads it, and a
    row left behind flips every verdict in the next test."""
    def _wipe():
        from sqlmodel import delete
        from app.db.init_db import get_session, init_db
        from app.db.models import H1BSponsor
        init_db()
        with get_session() as session:
            session.exec(delete(H1BSponsor))
            session.commit()
        h1b_data.refresh_cache()
    _wipe()
    yield
    _wipe()


def _load_us(employer: str, approvals: int = 40, denials: int = 2, year: int = 2024):
    from app.db.init_db import get_session
    from app.db.models import H1BSponsor
    with get_session() as session:
        session.add(H1BSponsor(
            employer_key=h1b_data.normalize(employer), employer_name=employer,
            approvals=approvals, denials=denials,
            approval_rate=approvals / max(1, approvals + denials),
            fiscal_year=year, country="united states",
        ))
        session.commit()
    h1b_data.refresh_cache()


# ── 1. Refusal detection is sentence-scoped and negation-aware ───────────────

@pytest.mark.parametrize("text", [
    "There is no sponsorship requirement for this role.",
    "We consider candidates with or without sponsorship.",
    "Visa sponsorship is available for this position.",
    "We offer visa sponsorship to exceptional candidates.",
    "You do not need to be a US citizen or permanent resident.",
    "Candidates are eligible for visa sponsorship.",
    "There are no sponsorship restrictions on this role.",
])
def test_positive_sentences_are_not_refusals(text):
    assert find_refusal(text) is None, f"false refusal on: {text!r}"
    assert refuses(text) is False


@pytest.mark.parametrize("text", [
    "We do not sponsor work visas.",
    "This role cannot sponsor candidates.",
    "No sponsorship.",
    "Applicants must be US citizen or permanent resident.",
    "Candidates must be authorized to work without sponsorship.",
    "Unable to provide visa sponsorship for this position.",
    "US citizenship required.",
])
def test_real_refusals_are_still_caught(text):
    found = find_refusal(text)
    assert found is not None, f"missed refusal in: {text!r}"
    assert found.sentence, "the matched sentence must be returned so the UI can quote it"


def test_refusal_is_scoped_to_its_own_sentence():
    """A positive sentence must not cancel a refusal that lives elsewhere."""
    text = ("Visa sponsorship is available for senior roles. "
            "For this position we cannot sponsor.")
    found = find_refusal(text)
    assert found is not None and "cannot sponsor" == found.phrase


def test_rule_filter_does_not_block_on_a_positive_sentence():
    """The bug with teeth: this used to hard-block, score_override=10."""
    from app.db.models import Job, JobSource
    from app.matching.filters.rule_filter import RuleFilter
    job = Job(source=JobSource.GREENHOUSE, external_id="e1", company="AcmeCo",
              title="Backend Engineer", location="Austin, TX", url="https://x.co/1",
              description="There is no sponsorship requirement for this role.")
    result = RuleFilter().filter(job)      # legacy ctor => requires_sponsorship=True
    assert result.passed is True, result.reason


def test_rule_filter_still_blocks_a_real_refusal():
    from app.db.models import Job, JobSource
    from app.matching.filters.rule_filter import RuleFilter
    job = Job(source=JobSource.GREENHOUSE, external_id="e2", company="AcmeCo",
              title="Backend Engineer", location="Austin, TX", url="https://x.co/2",
              description="We will not sponsor visas for this position.")
    result = RuleFilter().filter(job)
    assert result.passed is False
    assert "will not sponsor" in result.reason


def test_the_phrase_list_has_exactly_one_home():
    """Two hand-synced copies with no test that they agreed is what shipped."""
    from app.common import sponsorship_text
    from app.intelligence import sponsorship as spons
    from app.matching.filters import constants
    assert constants.NO_SPONSORSHIP_HARD is sponsorship_text.NO_SPONSORSHIP_HARD
    assert spons.NO_SPONSORSHIP_PATTERNS is sponsorship_text.NO_SPONSORSHIP_HARD


# ── 2. Provenance and contradictions ─────────────────────────────────────────

def test_no_dataset_loaded_is_not_reported_as_a_negative_finding():
    a = assess(company="Some Startup Inc", description="Backend role.",
               location="Austin, TX")
    assert a.source == "none"
    assert a.badge == "Not checked"
    # It must NOT claim the employer was looked up and found wanting.
    assert "no filing found" not in a.reason.lower()


def test_dataset_loaded_but_employer_absent_says_so():
    _load_us("Globex Corporation")
    a = assess(company="Totally Different Co", description="Backend role.",
               location="Austin, TX")
    assert a.source == "uscis"
    assert a.badge == "No filing found"


def test_uscis_record_carries_its_year_and_high_confidence():
    _load_us("Globex Corporation", approvals=40, denials=2, year=2024)
    a = assess(company="Globex Corporation", description="Backend role.",
               location="Austin, TX")
    assert a.likelihood == SponsorshipLikelihood.HIGH
    assert a.source == "uscis" and a.as_of == "FY2024" and a.confidence == "high"
    assert any(s["kind"] == "uscis" for s in a.signals)


def test_curated_name_match_is_labelled_a_guess_not_a_record():
    """The old copy asserted 'files regularly per public USCIS/DOL records'
    for employers we had never looked up."""
    a = assess(company="Google", description="Backend role.", location="Austin, TX")
    assert a.source == "curated" and a.confidence == "low"
    assert "curated list" in a.reason.lower()
    assert "public usciss" not in a.reason.lower()


def test_refusal_does_not_erase_the_uscis_record():
    _load_us("Globex Corporation", approvals=40, denials=2, year=2024)
    a = assess(company="Globex Corporation",
               description="Great role. We do not sponsor work visas.",
               location="Austin, TX")
    assert a.explicitly_refuses is True
    assert a.contradictory is True
    assert a.badge == "Conflicting signals"
    kinds = {s["kind"] for s in a.signals}
    assert {"refusal", "uscis"} <= kinds


def test_has_sponsored_is_a_positive_tone():
    """MEDIUM used to fall through to tone='unknown', so an employer with a
    real USCIS approval on record rendered as "Sponsorship not stated" — 2,072
    jobs/day in production. Every consumer branches on 'good'/'bad' and treats
    the rest as unknown, so the missing case suppressed the card chip and the
    drawer verdict at once."""
    _load_us("Initech LLC", approvals=2, denials=0, year=2024)
    a = assess(company="Initech LLC", description="Backend role.",
               location="Austin, TX")
    assert a.likelihood == SponsorshipLikelihood.MEDIUM
    assert a.badge == "Has sponsored"
    assert a.tone == "good"


def test_contradiction_has_its_own_tone():
    """'mixed' must be distinguishable — the drawer's fallback branch says the
    posting was silent, which is the opposite of what a contradiction holds."""
    _load_us("Globex Corporation", approvals=40, denials=2, year=2024)
    a = assess(company="Globex Corporation",
               description="Great role. We do not sponsor work visas.",
               location="Austin, TX")
    assert a.tone == "mixed"


def test_drawer_renders_every_tone_the_assessor_can_emit():
    """Guard against a new tone falling into the 'not stated' fallback."""
    import pathlib
    html = pathlib.Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    for tone in ("good", "bad", "mixed"):
        assert f"tone === '{tone}'" in html, f"drawer has no branch for tone={tone!r}"


def test_cap_exempt_does_not_erase_a_refusal():
    """A university posting that refuses used to come back HIGH with
    explicitly_refuses=False — the worst kind of confident wrong answer."""
    a = assess(company="Stanford University",
               description="Lab engineer. We do not sponsor work visas.",
               url="https://stanford.edu/jobs", location="Stanford, CA")
    assert a.explicitly_refuses is True
    assert a.likelihood == SponsorshipLikelihood.LOW
    assert a.badge == "Conflicting signals"


def test_cap_exempt_without_a_refusal_is_unchanged():
    a = assess(company="Stanford University", description="Lab engineer.",
               url="https://stanford.edu/jobs", location="Stanford, CA")
    assert a.cap_exempt is True
    assert "no lottery" in a.reason.lower()


def test_edu_substring_does_not_confer_cap_exempt_status():
    a = assess(company="EducationCorp", description="Backend role.",
               url="https://jobs.educationcorp.com/1", location="Austin, TX")
    assert a.cap_exempt is False


# ── 3. The facet must be re-computable ───────────────────────────────────────

def test_compute_serializes_provenance():
    from app.strategy.job_facets import compute
    _, spons_json, _ = compute("Backend Engineer", "Backend role.",
                               "Some Startup Inc", "https://x.co/1", "Austin, TX")
    d = json.loads(spons_json)
    for key in ("source", "as_of", "confidence", "likelihood", "signals", "contradictory"):
        assert key in d, f"provenance key {key!r} missing from sponsorship_json"


def test_restamp_updates_an_already_stamped_row():
    """The headline fix: before this, an ingested dataset could never reach a
    job that had already been stamped, so an upload looked like a no-op."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    from app.strategy.job_facets import backfill

    with get_session() as session:
        session.add(Job(source=JobSource.GREENHOUSE, external_id="restamp-1",
                        company="Globex Corporation", title="Backend Engineer",
                        location="Austin, TX", url="https://x.co/1",
                        description="Backend role.", user_id="u-restamp"))
        session.commit()

    # First pass with no dataset loaded: "not checked".
    backfill("u-restamp", pause=0)
    with get_session() as session:
        before = json.loads(session.exec(
            select(Job.sponsorship_json).where(Job.user_id == "u-restamp")).first())
    assert before["source"] == "none"

    # The admin uploads USCIS data. only_missing=True must NOT revisit the row...
    _load_us("Globex Corporation", approvals=40, denials=2, year=2024)
    assert backfill("u-restamp", pause=0) == 0

    # ...but the re-stamp must.
    assert backfill("u-restamp", pause=0, only_missing=False) == 1
    with get_session() as session:
        after = json.loads(session.exec(
            select(Job.sponsorship_json).where(Job.user_id == "u-restamp")).first())
    assert after["source"] == "uscis"
    assert after["as_of"] == "FY2024"


def test_restamp_can_clear_a_stale_cap_exempt_flag():
    """is_cap_exempt was only ever written True, so a wrong True was permanent."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    from app.strategy.job_facets import backfill

    with get_session() as session:
        session.add(Job(source=JobSource.GREENHOUSE, external_id="restamp-2",
                        company="EducationCorp", title="Backend Engineer",
                        location="Austin, TX", url="https://jobs.educationcorp.com/1",
                        description="Backend role.", user_id="u-capex",
                        is_cap_exempt=True))
        session.commit()

    backfill("u-capex", pause=0, only_missing=False)
    with get_session() as session:
        flag = session.exec(
            select(Job.is_cap_exempt).where(Job.user_id == "u-capex")).first()
    assert flag is False
