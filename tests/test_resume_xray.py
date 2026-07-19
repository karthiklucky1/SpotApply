"""Resume X-Ray — deterministic panels (no DB, no network, no LLM)."""
from app.intelligence.resume_xray import (
    ats_parse, employment_gaps, project_suggestions, screener_panel, six_second_scan,
)

RESUME = """
John Kumar
john.kumar@email.com | +1 555 123 4567 | linkedin.com/in/johnk | github.com/johnk

## Summary
Software engineer with 5+ years building backend systems.

## Experience
Senior Software Engineer - Acme Corp    Jan 2022 - Mar 2024
- Reduced API p99 latency 60% by rewriting the hot path in Go
- Led migration of 12 services to Kubernetes

Software Engineer - Beta Inc    Jun 2018 - Feb 2021
- Built payment reconciliation processing 2M records/day
- Worked with the team on internal tools

## Education
Bachelor of Technology, Computer Science - IIT Delhi, 2018

## Skills
Python, Go, PostgreSQL, Docker
"""


class _Profile:
    first_name = "John"
    current_title = "Senior Software Engineer"
    years_experience = 5
    target_roles = "backend engineer, software engineer"


def test_ats_parse_extracts_fields_and_flags_issues():
    p = ats_parse(RESUME)
    f = p["fields"]
    assert f["email"] and f["phone"] and f["linkedin"] and f["github"]
    assert set(f["sections_found"]) >= {"experience", "education", "skills"}
    assert f["date_ranges_found"] == 2
    # 4 bullets → the low-bullet warn fires; nothing blocking
    assert any(i["what"].startswith("Only") for i in p["issues"])
    assert not any(i["severity"] == "fail" for i in p["issues"])


def test_ats_parse_fails_without_contact_or_dates():
    p = ats_parse("Just some prose about my career with no structure at all.")
    sev = {i["severity"] for i in p["issues"]}
    assert "fail" in sev  # missing email + experience section + dates


def test_employment_gaps_detects_between_roles_and_current():
    gaps = employment_gaps(RESUME)
    months = sorted(g["months"] for g in gaps)
    # 11-month gap Feb 2021 -> Jan 2022, plus the run-out since Mar 2024
    assert 11 in months
    assert any(g["before"] == "today" for g in gaps)
    assert all("detail" in g and g["detail"] for g in gaps)


def test_employment_gaps_prefers_structured_history():
    exp = [
        {"title": "SWE", "company": "A", "start": "Jan 2020", "end": "Dec 2020"},
        {"title": "SWE II", "company": "B", "start": "Jun 2022", "current": True},
    ]
    gaps = employment_gaps("", exp)
    assert any(g["months"] >= 17 for g in gaps)


def test_six_second_scan_scores_and_verdicts():
    s = six_second_scan(RESUME, _Profile())
    assert s["out_of"] == 6
    assert s["score"] >= 5
    assert s["verdict"] == "survives the scan"
    empty = six_second_scan("nothing here", type("P", (), {
        "first_name": "", "current_title": "", "years_experience": 0, "target_roles": ""})())
    assert empty["score"] <= 2


def test_screener_panel_composes_four_verdicts():
    p = ats_parse(RESUME)
    s = six_second_scan(RESUME, _Profile())
    panel = screener_panel(p, s, coverage_pct=70, metrics_density=0.5, gaps=[])
    names = [x["name"] for x in panel]
    assert names == ["ATS parser", "Keyword filter", "AI grader (HiredScore-style)", "Human 6-second scan"]
    grader = panel[2]
    assert grader["grade"] in ("A", "B")
    assert all(x["kind"] == "simulated" for x in panel)


def test_screener_panel_grades_down_on_weak_signals():
    p = ats_parse(RESUME)
    s = six_second_scan("nothing", type("P", (), {
        "first_name": "", "current_title": "", "years_experience": 0, "target_roles": ""})())
    panel = screener_panel(p, s, coverage_pct=10, metrics_density=0.0,
                           gaps=[{"months": 12}])
    grader = next(x for x in panel if "grader" in x["name"])
    assert grader["grade"] in ("C", "D")


def test_project_suggestions_known_template_and_fallback():
    out = project_suggestions([
        {"skill": "kafka", "demand": 14, "pct": 47, "example_jobs": []},
        {"skill": "quantum computing", "demand": 3, "pct": 10, "example_jobs": []},
    ])
    assert out[0]["skill"] == "kafka"
    assert "Kafka" in out[0]["name"]
    assert len(out[0]["steps"]) == 3 and out[0]["bullet"]
    fallback = out[1]
    assert "quantum computing" in fallback["name"] or "quantum computing" in fallback["what"]
    assert fallback["bullet"]
