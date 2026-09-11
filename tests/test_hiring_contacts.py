"""Offline guard for app/intelligence/hiring_contacts.

Fixture sentences below are copied from the search-index snippets of live
postings observed on 2026-09-11 (docs/research/hiring-contacts-2026-09.md
§3) plus the 8 September public-feed examples. No network: the extractor is
regex-only and the whole point is that it never invents a person.
"""
from __future__ import annotations

from app.intelligence.hiring_contacts import (
    extract_from_structured,
    extract_from_text,
    origin_key,
    scan_org_keys,
    scan_person_like_keys,
    summarize,
)


def _rels(assertions, rel):
    return [a for a in assertions if a.relationship == rel]


# ── reporting lines: title-only is the common case and must be labelled so ────

def test_title_only_reporting_line_is_not_a_person():
    # Cloudflare / Greenhouse 8099000 (snippet, 2026-09-11)
    txt = ("You will report to the Senior Engineering Manager for AI Agents Tooling & "
           "Platform and partner closely with Product Management.")
    a = _rels(extract_from_text(txt), "reporting_manager")
    assert len(a) == 1
    assert a[0].evidence_type == "title_only_for_this_job"
    assert a[0].name is None
    assert "Senior Engineering Manager" in a[0].title


def test_reports_to_colon_template():
    # CSC Generation / Lever (snippet): "Reports to: Chief Technology Officer"
    txt = "Reports to: Chief Technology Officer\nLocation: Austin (Hybrid) / Remote (United States)"
    a = _rels(extract_from_text(txt), "reporting_manager")
    assert a and a[0].title == "Chief Technology Officer" and a[0].name is None


def test_reporting_to_sentence_start():
    txt = "Reporting to the VP of Engineering, you will own the platform roadmap."
    a = _rels(extract_from_text(txt), "reporting_manager")
    assert a and a[0].title.startswith("VP of Engineering")


def test_named_manager_with_title_both_orders():
    for txt in ("You will report to Jane Doe, VP of Engineering.",
                "You will report to our CTO, Jane Doe."):
        a = _rels(extract_from_text(txt), "reporting_manager")
        assert a, txt
        assert a[0].evidence_type == "named_for_this_job"
        assert a[0].name == "Jane Doe"
        assert a[0].title and ("CTO" in a[0].title or "VP" in a[0].title)


def test_initial_reporting_qualifier_is_preserved():
    # Ashby "Engineering Manager — Americas" pattern (8 Sept experiment)
    txt = ("This role initially reports to Abhik Pramanik, Head of Engineering, and may later "
           "move to our Director of Engineering.")
    a = _rels(extract_from_text(txt), "reporting_manager")
    assert a[0].name == "Abhik Pramanik"
    assert "initially" in (a[0].qualifier or "")
    assert "later move" in (a[0].qualifier or "")


def test_author_self_identification_first_name_only():
    # Ashby TA Advisor pattern: author introduces herself; role reports to her.
    txt = ("Hi, I'm Kat and I lead Partnerships at Ashby. This person will report directly "
           "to me and work closely with our Community team.")
    a = _rels(extract_from_text(txt), "reporting_manager")
    self_ids = [x for x in a if x.evidence_type == "self_identified"]
    assert self_ids and self_ids[0].name == "Kat"
    assert "first name" in self_ids[0].qualifier


# ── false positives the research called out ──────────────────────────────────

def test_negative_phrases_do_not_become_reporting_lines():
    for txt in (
        "You will have three direct reports and report to the office twice a week.",
        "You will build reporting tools and generate reports to stakeholders.",
        "You will report to work at the Austin campus.",
        "The team reports to the CFO.",                           # team-level, not the vacancy
        "You will produce status reports for the leadership team.",
        "Experience with reporting infrastructure and dashboards.",
    ):
        assert not _rels(extract_from_text(txt), "reporting_manager"), txt


def test_senior_title_next_to_a_name_is_not_ownership():
    # A bio paragraph naming an exec is not a reporting line.
    txt = "Our CTO Jane Doe founded the company in 2019 after leading infra at Acme."
    assert not _rels(extract_from_text(txt), "reporting_manager")


# ── labelled template fields ─────────────────────────────────────────────────

def test_labelled_hiring_manager_and_recruiter():
    # Live Nation / Workday snippet exposed "Hiring Manager" as a TITLE, not a name.
    txt = ("Hiring Manager: VP - Client & Fan Support Technology\n"
           "Recruiter: Priya Raman\n"
           "Contact: careers@example.com")
    out = extract_from_text(txt)
    hm = _rels(out, "reporting_manager")
    assert hm and hm[0].evidence_type == "title_only_for_this_job" and hm[0].name is None
    rc = _rels(out, "recruiter")
    assert rc and rc[0].name == "Priya Raman" and rc[0].evidence_type == "named_for_this_job"
    em = _rels(out, "contact_email")
    assert em and em[0].evidence_type == "generic_mailbox" and em[0].name is None


def test_personal_mailbox_is_not_a_resolved_identity():
    out = extract_from_text("Questions? Email rishi@quill.example.")
    em = _rels(out, "contact_email")
    assert em[0].evidence_type == "named_for_this_job"
    assert "identity not resolved" in em[0].qualifier


# ── agency / client separation ────────────────────────────────────────────────

def test_agency_and_client_are_separate_assertions():
    # Jobway / Qrendo (Sweden JobSearch ad 31420374, 8 Sept)
    txt = ("Jobway is a recruitment agency. We are recruiting for Qrendo, a fast-growing AI "
           "company. Recruitment consultant: Oscar Thiele")
    out = extract_from_text(txt)
    assert _rels(out, "recruiting_agency")[0].organization == "Jobway"
    assert _rels(out, "hiring_company_named_in_ad")[0].organization == "Qrendo"
    rc = _rels(out, "recruiter")
    assert rc and rc[0].name == "Oscar Thiele"
    # and NO reporting manager was invented
    assert not _rels(out, "reporting_manager")


def test_team_name_is_captured_as_org_not_person():
    txt = "You will join the Subscription Conversion team at The New York Times."
    t = _rels(extract_from_text(txt), "team")
    assert t and t[0].organization == "Subscription Conversion"


# ── mirrors dedupe to one origin ─────────────────────────────────────────────

def test_syndicated_copies_share_one_origin_key():
    a = "You will report to the Engineering Manager of the Subscription Conversion team."
    b = "You  will report to the  Engineering Manager of the Subscription Conversion team.\n"
    assert origin_key(a) == origin_key(b)
    assert len(extract_from_text(a + " " + b)) == 1


# ── structured payloads ───────────────────────────────────────────────────────

def test_smartrecruiters_creator_is_posting_creator_not_manager():
    payload = {"id": "744000148622869", "name": "Head of Revenue Operations - Base44",
               "creator": {"name": "Anthony Rodriguez", "avatarUrl": None},
               "department": {"id": "1", "label": "Revenue"}, "refNumber": "REQ-1"}
    out = extract_from_structured("smartrecruiters", payload)
    pc = _rels(out, "posting_creator")
    assert pc and pc[0].name == "Anthony Rodriguez" and "not the manager" in pc[0].qualifier
    assert _rels(out, "team")[0].organization == "Revenue"
    keys = dict(scan_person_like_keys(payload))
    assert "creator" in keys
    orgs = dict(scan_org_keys(payload))
    assert "refNumber" in orgs and "department" in orgs


def test_greenhouse_metadata_person_field_only_when_exposed():
    payload = {"id": 1, "departments": [{"name": "Engineering"}],
               "metadata": [{"name": "Workplace Type", "value": "Hybrid"},
                            {"name": "Hiring Manager", "value": "Sam Lee"}]}
    out = extract_from_structured("greenhouse", payload)
    hm = _rels(out, "reporting_manager")
    assert hm and hm[0].name == "Sam Lee" and hm[0].field == "metadata[Hiring Manager]"
    # a board with no such custom field yields no person
    assert not _rels(extract_from_structured("greenhouse", {"metadata": [{"name": "Workplace Type", "value": "Hybrid"}]}),
                     "reporting_manager")


def test_jobtech_agency_vs_workplace_split():
    payload = {"employer": {"name": "Jobway AB", "workplace": "Qrendo"},
               "application_contacts": [{"name": None, "description": "Oscar Thiele",
                                         "contact_type": "Recruitment consultant"}]}
    out = extract_from_structured("jobtech", payload)
    assert _rels(out, "hiring_company_named_in_ad")[0].organization == "Qrendo"
    jc = _rels(out, "job_contact")
    assert jc and jc[0].name == "Oscar Thiele"


def test_unknown_source_asserts_nothing_but_scan_still_reports_fields():
    payload = {"offers": [{"title": "x", "hiring_manager": "Pat Q", "department": "Data"}]}
    assert extract_from_structured("recruitee", payload) == []
    found = dict(scan_person_like_keys(payload))
    assert "offers[0].hiring_manager" in found


def test_summary_shape():
    out = extract_from_text("You will report to the CTO. Recruiter: Ana Ruiz")
    s = summarize(out)
    assert s.get("reporting_manager/title_only_for_this_job") == 1
    assert s.get("recruiter/named_for_this_job") == 1
