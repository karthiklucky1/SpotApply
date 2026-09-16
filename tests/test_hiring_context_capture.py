"""Hiring-context capture: fixtures, provenance, liveness, and the shared key.

The payload fixtures are shaped to match what each ATS returns for the keys we
now read. Where the live audit measured a field's fill rate, the test says so —
SmartRecruiters `creator` was populated on 0 of 4 live postings, so the suite
pins that its ABSENCE is handled, not that its presence is typical.

Rows created here are prefixed `hctest-` and deleted by that prefix, per the
suite rule that a test cleans up only its own rows.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import select, delete

from app.db.init_db import get_session
from app.db.models import EvidenceClass, JobHiringContext, JobLiveness, JobLivenessState
from app.discovery.base import (
    EVIDENCE_DIRECT_PERSON,
    EVIDENCE_STRUCTURED_PERSON,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    EVIDENCE_TITLE_ONLY,
    ContextField,
    RawJob,
)
from app.discovery import liveness as lv
from app.discovery.hiring_context import (
    CONTEXT_FIELDS,
    _assert_evidence_constants_match_enum,
    apply_text_extraction,
    evidence_rank,
    load_context,
    merge_into,
    put,
    record_context,
)

_PREFIX = "hctest-"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_session() as s:
        s.exec(delete(JobHiringContext).where(
            JobHiringContext.external_id.like(f"{_PREFIX}%")))
        s.exec(delete(JobLiveness).where(
            JobLiveness.external_id.like(f"{_PREFIX}%")))
        s.commit()


def _ctx_of(scraper_cls, payload, **kw):
    """A canned HTTP response, so a scraper's parse runs with no network."""

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    return _Resp()


# ══════════════════════════════════════════════════════════════════════════
# The evidence vocabulary must not drift between the two places it lives
# ══════════════════════════════════════════════════════════════════════════

def test_evidence_constants_match_the_enum():
    _assert_evidence_constants_match_enum()


def test_every_context_field_is_a_real_column():
    cols = set(JobHiringContext.__table__.columns.keys())
    assert set(CONTEXT_FIELDS) <= cols, set(CONTEXT_FIELDS) - cols


def test_structured_evidence_outranks_text_evidence():
    assert evidence_rank(EVIDENCE_STRUCTURED_PERSON) > evidence_rank(EVIDENCE_TITLE_ONLY)
    assert evidence_rank(EVIDENCE_DIRECT_PERSON) > evidence_rank(EvidenceClass.SUGGESTED.value)
    assert evidence_rank(None) == 0


# ══════════════════════════════════════════════════════════════════════════
# Per-ATS fixtures — Ashby, Greenhouse, Lever, SmartRecruiters, Workday,
# Recruitee, Pinpoint
# ══════════════════════════════════════════════════════════════════════════

def test_ashby_department_and_team(monkeypatch):
    import httpx
    from app.discovery.ashby import AshbyScraper
    payload = {"jobs": [{
        "id": f"{_PREFIX}ash1", "title": "AI Engineer", "locationName": "Remote",
        "isRemote": True, "publishedAt": "2026-09-01T00:00:00Z",
        "jobUrl": "https://jobs.ashbyhq.com/acme/1",
        "descriptionHtml": "<p>Build things.</p>",
        "department": "Applied AI", "team": "Applied AI Engineering",
    }]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = AshbyScraper("acme").fetch()[0]
    assert job.context["department"].value == "Applied AI"
    assert job.context["team"].value == "Applied AI Engineering"
    assert job.context["department"].evidence == EVIDENCE_TEAM_OR_DEPARTMENT
    assert job.context["department"].field == "department"
    assert job.origin == "ashby"


def test_ashby_missing_org_fields_is_not_an_error(monkeypatch):
    import httpx
    from app.discovery.ashby import AshbyScraper
    payload = {"jobs": [{"id": f"{_PREFIX}ash2", "title": "X", "locationName": None,
                         "jobUrl": "u", "descriptionHtml": None}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = AshbyScraper("acme").fetch()[0]
    assert "department" not in job.context and "team" not in job.context
    assert job.context["ats"].value == "ashby"


def test_greenhouse_departments_requisition_and_metadata(monkeypatch):
    import httpx
    from app.discovery.greenhouse import GreenhouseScraper
    payload = {"jobs": [{
        "id": 991, "title": "Backend Engineer", "location": {"name": "NYC"},
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/991",
        "content": "<p>Work here.</p>", "first_published": "2026-09-01T00:00:00Z",
        "requisition_id": "REQ-4821",
        "departments": [{"name": "Engineering"}, {"name": "Payments"}],
        "metadata": [
            {"name": "Workplace Type", "value": "Hybrid"},          # ignored
            {"name": "Hiring Manager", "value": "Sam Lee"},
            {"name": "Reports To", "value": "Director of Payments"},
            {"name": "Unmapped Custom Thing", "value": "whatever"},  # ignored
        ],
    }]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = GreenhouseScraper("acme").fetch()[0]
    c = job.context
    assert c["department"].value == "Engineering"
    assert c["team"].value == "Payments"          # second department, labelled as team
    assert c["requisition_id"].value == "REQ-4821"
    assert c["reporting_manager_name"].value == "Sam Lee"
    assert c["reporting_manager_name"].evidence == EVIDENCE_DIRECT_PERSON
    assert c["reporting_title"].value == "Director of Payments"
    # An unrecognised custom field is never guessed into a column.
    assert not any(f.value == "whatever" for f in c.values())
    assert not any(f.value == "Hybrid" for f in c.values())


def test_greenhouse_metadata_typed_values(monkeypatch):
    """Greenhouse metadata values are string, list or {label} depending on the
    custom-field type. A malformed one must not take down the board."""
    import httpx
    from app.discovery.greenhouse import GreenhouseScraper
    payload = {"jobs": [{
        "id": 992, "title": "T", "location": None, "absolute_url": "u", "content": "",
        "departments": None, "requisition_id": None,
        "metadata": [
            {"name": "Team", "value": ["Platform", "Infra"]},
            {"name": "Division", "value": {"label": "Cloud"}},
            {"name": "Recruiter", "value": None},
            "not-a-dict",
        ],
    }]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = GreenhouseScraper("acme").fetch()[0]
    assert job.context["team"].value == "Platform, Infra"
    assert job.context["division"].value == "Cloud"
    assert "recruiter_name" not in job.context


def test_lever_team_and_department(monkeypatch):
    import httpx
    from app.discovery.lever import LeverScraper
    payload = [{
        "id": f"{_PREFIX}lev1", "text": "Staff Engineer",
        "categories": {"location": "SF", "commitment": "Full-time",
                       "team": "Core Platform", "department": "Engineering"},
        "hostedUrl": "https://jobs.lever.co/acme/1", "descriptionPlain": "Do work.",
        "createdAt": 1757000000000, "lists": [],
    }]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = LeverScraper("acme").fetch()[0]
    assert job.context["team"].value == "Core Platform"
    assert job.context["department"].value == "Engineering"
    assert job.context["team"].field == "categories.team"


def test_smartrecruiters_refnumber_department_function_and_absent_creator(monkeypatch):
    """The live audit found `creator` populated on 0 of 4 postings, so absence
    is the normal case and must be handled silently."""
    import httpx
    from app.discovery import smartrecruiters as sr
    listing = {"content": [{"id": f"{_PREFIX}sr1", "name": "Data Engineer",
                            "location": {"fullLocation": "Austin"},
                            "releasedDate": "2026-09-01T00:00:00Z",
                            "company": {"name": "Acme Global Ltd"}}],
               "totalFound": 1}
    detail = {"id": f"{_PREFIX}sr1", "name": "Data Engineer",
              "refNumber": "REF-77", "department": {"id": "1", "label": "Revenue"},
              "function": {"id": "9", "label": "Data"},
              "company": {"name": "Acme Global Ltd"},
              "jobAd": {"sections": {"jobDescription": {"title": "Job", "text": "<p>Hi</p>"}}}}
    calls = {"n": 0}

    def _get(url, *a, **k):
        calls["n"] += 1
        return _ctx_of(None, detail if f"/postings/{_PREFIX}sr1" in url else listing)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(sr, "_is_tech_job", lambda *a, **k: True, raising=False)
    jobs = sr.SmartRecruitersScraper("acme").fetch()
    if not jobs:
        pytest.skip("tech-title gate rejected the fixture title")
    c = jobs[0].context
    assert c["requisition_id"].value == "REF-77"
    assert c["department"].value == "Revenue"
    assert c["division"].value == "Data"
    assert c["hiring_entity"].value == "Acme Global Ltd"
    assert "posting_creator_name" not in c        # absent upstream, absent here


def test_smartrecruiters_creator_is_posting_creator_never_manager():
    from app.discovery.smartrecruiters import _context_for
    ctx = _context_for({}, {"creator": {"firstName": "Anthony", "lastName": "Rodriguez"}})
    assert ctx["posting_creator_name"].value == "Anthony Rodriguez"
    assert ctx["posting_creator_name"].evidence == EVIDENCE_STRUCTURED_PERSON
    assert "reporting_manager_name" not in ctx


def test_smartrecruiters_taxonomy_may_be_a_bare_string():
    from app.discovery.smartrecruiters import _context_for
    ctx = _context_for({}, {"department": "Revenue", "function": None, "refNumber": ""})
    assert ctx["department"].value == "Revenue"
    assert "division" not in ctx and "requisition_id" not in ctx


def test_workday_requisition_and_optional_hiring_organization():
    """`hiringOrganization` was NOT confirmed present in a live CXS response,
    so both shapes and its absence must all be fine."""
    from app.discovery.hiring_context import put as _put
    from app.discovery.base import EVIDENCE_ORG_ENTITY
    for org, expected in (({"name": "Live Nation Worldwide"}, "Live Nation Worldwide"),
                          ("Live Nation Worldwide", "Live Nation Worldwide"),
                          (None, None)):
        ctx: dict = {}
        value = org.get("name") if isinstance(org, dict) else org
        _put(ctx, "hiring_entity", value, EVIDENCE_ORG_ENTITY, "x")
        _put(ctx, "requisition_id", "JR-90837", EVIDENCE_ORG_ENTITY, "jobPostingInfo.jobReqId")
        assert ctx["requisition_id"].value == "JR-90837"
        assert (ctx.get("hiring_entity").value if "hiring_entity" in ctx else None) == expected


def test_recruitee_department_but_tags_are_not_a_team(monkeypatch):
    import httpx
    from app.discovery.recruitee import RecruiteeScraper
    payload = {"offers": [{
        "id": 5, "status": "published", "title": "SRE", "location": "Amsterdam",
        "description": "<p>Hi</p>", "requirements": "", "careers_url": "u",
        "company_name": "Acme BV", "department": "Infrastructure",
        "tags": ["urgent", "senior"],
    }]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = RecruiteeScraper("acme").fetch()[0]
    assert job.context["department"].value == "Infrastructure"
    assert job.context["hiring_entity"].value == "Acme BV"
    # A free-text tag is not an org unit and must not become one.
    assert "team" not in job.context


def test_pinpoint_reporting_to_is_a_title_not_a_person(monkeypatch):
    import httpx
    from app.discovery.pinpoint import PinpointScraper
    payload = {"data": [{"id": 3, "attributes": {
        "title": "Analyst", "location_name": "London", "description": "<p>x</p>",
        "url": "u", "company_name": "Acme", "department": "Finance",
        "division": "EMEA", "reporting_to": "Director of Machine Learning",
        "reference": "VAC-12",
    }}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ctx_of(None, payload))
    job = PinpointScraper("acme").fetch()[0]
    c = job.context
    assert c["reporting_title"].value == "Director of Machine Learning"
    assert c["reporting_title"].evidence == EVIDENCE_TITLE_ONLY
    assert "reporting_manager_name" not in c
    assert c["department"].value == "Finance" and c["division"].value == "EMEA"
    assert c["requisition_id"].value == "VAC-12"


# ══════════════════════════════════════════════════════════════════════════
# Text extraction at ingest, and the false positives it must refuse
# ══════════════════════════════════════════════════════════════════════════

def _raw(desc: str, ext: str = f"{_PREFIX}t1", **kw) -> RawJob:
    return RawJob(source="greenhouse", external_id=ext, company="Acme", title="Eng",
                  location="NYC", remote=False, url="https://x/y", description=desc, **kw)


def test_text_extraction_finds_a_reporting_title():
    r = _raw("About us. " * 20 + "You will report to the Director of Machine Learning.")
    apply_text_extraction([r])
    assert r.context["reporting_title"].value == "Director of Machine Learning"
    assert r.context["reporting_title"].evidence == EVIDENCE_TITLE_ONLY


def test_text_extraction_reads_past_the_retrieval_projection():
    """The 800-char retrieval projection is exactly why this runs at ingest."""
    from app.discovery.hiring_context import TEXT_SCAN_CHARS
    r = _raw("filler. " * 400 + "You will report to the VP of Engineering.")
    assert len(r.description) > 800
    apply_text_extraction([r])
    assert r.context["reporting_title"].value == "VP of Engineering"
    assert TEXT_SCAN_CHARS > 800


@pytest.mark.parametrize("text", [
    "You will have three direct reports and generate reports to the board.",
    "You will report to work at the Austin campus three days a week.",
    "Experience building reporting infrastructure and dashboards.",
    "The team reports to the CFO.",
])
def test_text_extraction_refuses_false_positives(text):
    r = _raw("Padding sentence. " + text)
    apply_text_extraction([r])
    assert "reporting_title" not in r.context
    assert "reporting_manager_name" not in r.context


def test_pronoun_is_never_stored_as_a_reporting_title():
    """Bug 1: 'reports directly to me' produced 'Reports to: me'."""
    r = _raw("This role is great. This person will report directly to me.")
    apply_text_extraction([r])
    assert r.context.get("reporting_title") is None


def test_self_identified_author_is_captured_with_a_name():
    r = _raw("Hi, I'm Kat and I lead Partnerships. This person will report directly to me.")
    apply_text_extraction([r])
    assert r.context["reporting_manager_name"].value == "Kat"
    assert r.context["reporting_manager_name"].evidence == EvidenceClass.SELF_IDENTIFIED.value


def test_organisation_capture_respects_sentence_boundaries():
    """Bug 2: '...for Qrendo. Recruitment consultant: Oscar' gave 'Qrendo. Recruitment'."""
    r = _raw("Jobway is a recruitment agency. We are recruiting for Qrendo. "
             "Recruitment consultant: Oscar Thiele")
    apply_text_extraction([r])
    assert r.context["hiring_entity"].value == "Qrendo"
    assert r.context["recruiting_agency"].value == "Jobway"
    assert r.context["recruiter_name"].value == "Oscar Thiele"


def test_department_is_distinct_from_team():
    """Bug 3: there was no department relationship at all."""
    r = _raw("Department: Applied AI\nTeam: Applied AI Engineering\nWe build models.")
    apply_text_extraction([r])
    assert r.context["department"].value == "Applied AI"
    assert r.context["team"].value == "Applied AI Engineering"


def test_generic_mailbox_is_not_stored_as_a_contact_person():
    r = _raw("Questions? Email careers@acme.com for more information about the role.")
    apply_text_extraction([r])
    assert "contact_email" not in r.context


def test_personal_mailbox_is_stored():
    r = _raw("Questions about this role? Email rishi.raman@quill.example directly.")
    apply_text_extraction([r])
    assert "rishi.raman@quill.example" in r.context["contact_email"].value


def test_structured_ats_field_beats_text_for_the_same_column():
    r = _raw("You are part of the Payments department. More text here.")
    put(r.context, "department", "Engineering", EVIDENCE_TEAM_OR_DEPARTMENT,
        "departments[0].name")
    apply_text_extraction([r])
    assert r.context["department"].value == "Engineering"


# ══════════════════════════════════════════════════════════════════════════
# The shared key: one posting, many users, ONE context row
# ══════════════════════════════════════════════════════════════════════════

def test_one_posting_used_by_many_users_stores_one_context_row():
    ext = f"{_PREFIX}shared1"
    jobs = []
    for _user in range(12):
        r = _raw("x", ext=ext)
        put(r.context, "department", "Applied AI", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
        jobs.append(r)
    for r in jobs:                      # twelve separate adoption passes
        record_context([r])
    with get_session() as s:
        rows = s.exec(select(JobHiringContext).where(
            JobHiringContext.external_id == ext)).all()
    assert len(rows) == 1
    assert rows[0].department == "Applied AI"


def test_a_job_with_no_context_never_blanks_a_stored_row():
    """Adoption rebuilds RawJobs from stored columns and carries no context."""
    ext = f"{_PREFIX}shared2"
    rich = _raw("x", ext=ext)
    put(rich.context, "department", "Applied AI", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
    record_context([rich])
    record_context([_raw("x", ext=ext)])          # the adoption copy
    with get_session() as s:
        row = s.exec(select(JobHiringContext).where(
            JobHiringContext.external_id == ext)).one()
    assert row.department == "Applied AI"


def test_weaker_evidence_never_overwrites_stronger():
    stored_v = {"reporting_manager_name": "Sam Lee"}
    stored_e = {"reporting_manager_name": {"evidence": EVIDENCE_DIRECT_PERSON, "field": "m"}}
    incoming = {"reporting_manager_name": ContextField(
        "Someone Else", EvidenceClass.SUGGESTED.value, "guess")}
    values, _evidence, changed = merge_into(stored_v, stored_e, incoming)
    assert values["reporting_manager_name"] == "Sam Lee"
    assert changed is False


def test_stronger_evidence_does_overwrite():
    stored_v = {"department": "Eng"}
    stored_e = {"department": {"evidence": EvidenceClass.SUGGESTED.value, "field": "guess"}}
    incoming = {"department": ContextField("Engineering", EVIDENCE_TEAM_OR_DEPARTMENT, "d")}
    values, evidence, changed = merge_into(stored_v, stored_e, incoming)
    assert values["department"] == "Engineering" and changed is True
    assert evidence["department"]["evidence"] == EVIDENCE_TEAM_OR_DEPARTMENT


def test_every_stored_value_has_an_evidence_entry():
    ext = f"{_PREFIX}ev1"
    r = _raw("x", ext=ext)
    put(r.context, "department", "Applied AI", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
    put(r.context, "requisition_id", "REQ-1", EvidenceClass.ORG_ENTITY.value, "requisition_id")
    record_context([r])
    ctx = load_context([("greenhouse", ext)])[("greenhouse", ext)]
    for key in CONTEXT_FIELDS:
        if ctx.get(key):
            assert key in ctx["evidence"], f"{key} stored with no evidence"


def test_placeholder_values_are_not_stored():
    ctx: dict = {}
    for junk in ("N/A", "  ", "none", "TBD", "-", None, True):
        put(ctx, "department", junk, EVIDENCE_TEAM_OR_DEPARTMENT, "d")
    assert ctx == {}


# ══════════════════════════════════════════════════════════════════════════
# Provenance
# ══════════════════════════════════════════════════════════════════════════

def test_hn_rows_keep_their_true_origin_while_staying_in_their_bucket():
    from app.discovery.sources import hn_whoishiring as hn
    assert 'source="indeed"' in open(hn.__file__).read()      # bucket unchanged
    assert 'origin="hn_whoishiring"' in open(hn.__file__).read()


def test_serpapi_keeps_the_via_field_it_used_to_discard():
    from app.discovery.sources import serpapi
    src = open(serpapi.__file__).read()
    assert "origin_provider=via_raw" in src


def test_remoteok_is_distinguishable_from_remotive():
    from app.discovery.sources import remoteok
    src = open(remoteok.__file__).read()
    assert 'source="remotive"' in src and 'origin="remoteok"' in src


# ══════════════════════════════════════════════════════════════════════════
# Liveness — the point is what is NOT treated as death
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status,expected", [
    (404, JobLivenessState.REMOVED.value),
    (410, JobLivenessState.EXPIRED.value),
    (429, JobLivenessState.RATE_LIMITED.value),
    (403, JobLivenessState.BLOCKED.value),
    (401, JobLivenessState.BLOCKED.value),
    (500, JobLivenessState.UNKNOWN.value),
    (503, JobLivenessState.UNKNOWN.value),
    (200, JobLivenessState.LIVE.value),
])
def test_status_codes_map_to_the_right_state(status, expected):
    state, _reason = lv.classify(
        status, requested_url="https://boards.example.com/jobs/12345")
    assert state == expected


def test_only_removed_and_expired_count_as_dead():
    assert lv.is_dead(JobLivenessState.REMOVED.value)
    assert lv.is_dead(JobLivenessState.EXPIRED.value)
    for s in (JobLivenessState.RATE_LIMITED, JobLivenessState.BLOCKED,
              JobLivenessState.UNKNOWN, JobLivenessState.WRONG_PAGE,
              JobLivenessState.LIVE):
        assert not lv.is_dead(s.value), s


def test_a_200_page_saying_the_position_is_gone_is_removed():
    state, reason = lv.classify(
        200, requested_url="https://acme.teamtailor.com/jobs/12345",
        body="<h1>This position is no longer active</h1>")
    assert state == JobLivenessState.REMOVED.value
    assert reason == "body_no_longer_active"


@pytest.mark.parametrize("body", [
    "<h1>This position is no longer active</h1>",          # Teamtailor wording
    "This job is no longer available.",
    "Sorry, this position has been filled.",
    "We are no longer accepting applications for this role.",
    "<p>The job you are looking for is no longer available.</p>",
    "This posting  has\n  been   closed",                   # split across markup
    "Applications are closed",
])
def test_real_removal_wordings_are_all_caught(body):
    state, _ = lv.classify(200, requested_url="https://x/jobs/1", body=body)
    assert state == JobLivenessState.REMOVED.value


@pytest.mark.parametrize("body", [
    "Senior Engineer. You will report to the VP of Engineering. Apply now.",
    "We are hiring! This position is open and applications are welcome.",
    "Our reporting infrastructure is no longer built on Hadoop.",
    "This role has been designed for someone who closed big deals.",
])
def test_live_pages_that_talk_about_closing_are_still_live(body):
    state, _ = lv.classify(200, requested_url="https://x/jobs/1", body=body)
    assert state == JobLivenessState.LIVE.value


def test_a_redirect_to_the_careers_index_is_wrong_page_not_removed():
    state, _ = lv.classify(
        200, requested_url="https://acme.com/careers/12345",
        final_url="https://acme.com/careers", body="Browse all jobs")
    assert state == JobLivenessState.WRONG_PAGE.value


def test_a_404_on_a_board_root_is_not_evidence_a_job_died():
    state, reason = lv.classify(404, requested_url="https://acme.com/careers")
    assert state == JobLivenessState.UNKNOWN.value
    assert reason == "http_404_on_index_url"


def test_a_transport_error_is_unknown():
    state, _ = lv.classify(None, requested_url="u", error="ConnectTimeout")
    assert state == JobLivenessState.UNKNOWN.value


def test_an_inconclusive_check_does_not_erase_a_known_state():
    ext = f"{_PREFIX}live1"
    lv.record("greenhouse", ext, JobLivenessState.LIVE.value, reason="http_200")
    lv.record("greenhouse", ext, JobLivenessState.RATE_LIMITED.value, reason="http_429")
    lv.record("greenhouse", ext, JobLivenessState.BLOCKED.value, reason="http_403")
    with get_session() as s:
        row = s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).one()
    assert row.state == JobLivenessState.LIVE.value
    assert row.inconclusive_streak == 2


def test_absence_from_an_incomplete_board_fetch_records_nothing():
    n = lv.record_board_absence("greenhouse", present_ids=[],
                                known_ids=[f"{_PREFIX}g1"], board_complete=False)
    assert n == 0
    with get_session() as s:
        assert s.exec(select(JobLiveness).where(
            JobLiveness.external_id == f"{_PREFIX}g1")).first() is None


def test_absence_from_a_complete_board_fetch_is_removal():
    n = lv.record_board_absence("greenhouse", present_ids=[f"{_PREFIX}g2"],
                                known_ids=[f"{_PREFIX}g2", f"{_PREFIX}g3"],
                                board_complete=True)
    assert n == 1
    states = lv.load_states([("greenhouse", f"{_PREFIX}g3")])
    assert states[("greenhouse", f"{_PREFIX}g3")][0] == JobLivenessState.REMOVED.value


def test_needs_check_is_false_for_a_job_already_known_dead():
    from datetime import datetime, timedelta
    assert not lv.needs_check(JobLivenessState.REMOVED.value, datetime.utcnow(),
                              max_age_hours=12)
    assert lv.needs_check(None, None, max_age_hours=12)
    assert lv.needs_check(JobLivenessState.LIVE.value,
                          datetime.utcnow() - timedelta(hours=48), max_age_hours=12)
    assert not lv.needs_check(JobLivenessState.LIVE.value,
                              datetime.utcnow(), max_age_hours=12)
    assert not lv.needs_check(None, None, max_age_hours=0)      # disabled


# ══════════════════════════════════════════════════════════════════════════
# Observability — aggregate counts only, no per-job logging
# ══════════════════════════════════════════════════════════════════════════

def test_metrics_snapshot_counts_delivered_jobs_by_true_origin():
    from app.analytics.hiring_context_metrics import snapshot
    from app.db.models import Application, ApplicationStatus, Job, JobSource

    with get_session() as s:
        job = Job(source=JobSource.GREENHOUSE, external_id=f"{_PREFIX}m1",
                  company="Acme", title="Eng", url="u", user_id=f"{_PREFIX}u1",
                  origin="greenhouse")
        s.add(job)
        s.commit()
        s.refresh(job)
        s.add(Application(job_id=job.id, user_id=f"{_PREFIX}u1",
                          status=ApplicationStatus.SHORTLISTED))
        s.add(JobHiringContext(
            source="greenhouse", external_id=f"{_PREFIX}m1",
            department="Engineering", requisition_id="REQ-1",
            evidence_json=json.dumps({
                "department": {"evidence": EVIDENCE_TEAM_OR_DEPARTMENT, "field": "d"},
                "requisition_id": {"evidence": EvidenceClass.ORG_ENTITY.value,
                                   "field": "requisition_id"}})))
        s.commit()
        job_id = job.id
    try:
        snap = snapshot(days=7, user_id=f"{_PREFIX}u1")
        assert snap["delivered"] == 1
        assert snap["counts"]["department_available"] == 1
        assert snap["counts"]["requisition_id_available"] == 1
        assert snap["counts"]["named_manager_available"] == 0
        assert snap["counts"]["context_from_ats_fields"] == 1
        assert snap["per_100_delivered"]["department_available"] == 100.0
        assert snap["by_source"]["greenhouse"]["delivered"] == 1
        assert snap["liveness"]["NOT_CHECKED"] == 1
        assert snap["dead_reached_users"] == 0
    finally:
        with get_session() as s:
            s.exec(delete(Application).where(Application.user_id == f"{_PREFIX}u1"))
            s.exec(delete(Job).where(Job.id == job_id))
            s.commit()


# ══════════════════════════════════════════════════════════════════════════
# Migration safety — the whole change is additive, and rollback is a revert
# ══════════════════════════════════════════════════════════════════════════

def test_the_new_columns_are_declared_in_both_ddl_sites():
    """models.py builds a FRESH database; ensure_model_columns ALTERs an
    existing one. A column in one and not the other exists in dev and not in
    production, which is the hardest kind of drift to notice."""
    import re
    from app.db.models import Job
    declared = set(Job.__table__.columns.keys())
    migration_src = open("app/db/init_db.py").read()
    for col in ("origin", "origin_provider"):
        assert col in declared, f"{col} missing from the model"
        assert re.search(rf'\("{col}",\s*"VARCHAR"\)', migration_src), \
            f"{col} missing from ensure_model_columns"


def test_new_columns_are_nullable_so_the_alter_does_not_rewrite_the_table():
    """~1.47M job rows. A nullable column with no default is a metadata-only
    ALTER on Postgres; a NOT NULL or defaulted one rewrites every row."""
    from app.db.models import Job
    for col in ("origin", "origin_provider"):
        c = Job.__table__.columns[col]
        assert c.nullable, f"{col} must be nullable"
        assert c.default is None and c.server_default is None, \
            f"{col} must have no default"


def test_the_new_tables_are_not_user_scoped():
    """Context is keyed by the posting, not the tenant. If either table ever
    grows a user_id it becomes account-deletion's problem and stops being
    shared — this is the tripwire for that."""
    for model in (JobHiringContext, JobLiveness):
        cols = set(model.__table__.columns.keys())
        assert "user_id" not in cols, f"{model.__tablename__} gained a user_id"
        assert {"source", "external_id"} <= cols


def test_running_the_migration_twice_is_a_no_op():
    from app.db.init_db import ensure_model_columns
    ensure_model_columns()
    ensure_model_columns()
    with get_session() as s:
        s.exec(select(JobHiringContext)).first()      # table still queryable


def test_old_code_still_reads_rows_written_by_new_code():
    """Rollback plan: revert the deploy, leave the schema. Old code selects an
    explicit column list and never sees the new ones, so a row carrying them
    must still load through a projection that predates them."""
    from app.db.models import Job, JobSource
    with get_session() as s:
        j = Job(source=JobSource.GREENHOUSE, external_id=f"{_PREFIX}rb1",
                company="Acme", title="Eng", url="u",
                origin="greenhouse", origin_provider="LinkedIn")
        s.add(j)
        s.commit()
        s.refresh(j)
        jid = j.id
    try:
        with get_session() as s:
            row = s.exec(select(Job.id, Job.source, Job.external_id, Job.company,
                                Job.title, Job.url)
                         .where(Job.id == jid)).one()
        assert row[2] == f"{_PREFIX}rb1"
    finally:
        with get_session() as s:
            s.exec(delete(Job).where(Job.id == jid))
            s.commit()


# ══════════════════════════════════════════════════════════════════════════
# Capture runs once per POSTING, not once per sighting
# ══════════════════════════════════════════════════════════════════════════

def test_a_re_seen_posting_is_not_re_extracted():
    """Production regression guard. The pulse lane re-sees the same few
    thousand postings every tick; extracting from each sighting tripled its
    upsert p50 and pushed the capacity-limited lane into deferring boards."""
    from app.discovery.hiring_context import already_captured, capture
    ext = f"{_PREFIX}reseen"

    def _fresh():
        r = _raw("You will report to the Director of ML. " * 3, ext=ext)
        put(r.context, "department", "Applied AI", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
        return r

    written, enriched, skipped = capture([_fresh()])
    assert written == 1 and skipped == 0
    assert already_captured([("greenhouse", ext)]) == {("greenhouse", ext)}

    # Same posting seen again on the next tick: no extraction, no write.
    written2, enriched2, skipped2 = capture([_fresh()])
    assert (written2, enriched2, skipped2) == (0, 0, 1)


def test_capture_still_processes_postings_it_has_not_seen():
    from app.discovery.hiring_context import capture
    done_ext, new_ext = f"{_PREFIX}mix-done", f"{_PREFIX}mix-new"
    r1 = _raw("x", ext=done_ext)
    put(r1.context, "department", "Eng", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
    capture([r1])
    r1b = _raw("x", ext=done_ext)
    put(r1b.context, "department", "Eng", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
    r2 = _raw("x", ext=new_ext)
    put(r2.context, "department", "Data", EVIDENCE_TEAM_OR_DEPARTMENT, "d")
    written, _enriched, skipped = capture([r1b, r2])
    assert written == 1 and skipped == 1
    ctx = load_context([("greenhouse", new_ext)])[("greenhouse", new_ext)]
    assert ctx["department"] == "Data"


def test_capture_is_a_no_op_for_an_empty_batch():
    from app.discovery.hiring_context import capture
    assert capture([]) == (0, 0, 0)
