"""Hard constraints checked BEFORE any paid ranking (audit 2026-09-25, P1/P10).

The rule filter runs inside `Reranker` ahead of Tier-1 and Tier-2 for every
lane, so a posting the candidate cannot take costs nothing. Every check here
acts only on CONFIRMED profile facts — an unset status or degree never blocks.
Synthetic postings and profiles only.
"""
from __future__ import annotations

import pytest

from app.db.models import Job, JobSource
from app.matching.filters.rule_filter import (RuleFilter, classify_job_kind,
                                              required_degree_level,
                                              requires_citizenship)


class _Prof:
    def __init__(self, **kw):
        self.years_experience = 4
        self.salary_min = self.salary_max = 0
        self.salary_currency = "USD"
        self.requires_sponsorship = False
        self.preferred_country = "United States"
        self.key_skills = "python"
        self.degree = "B.S. Computer Science"
        self.job_type_preference = "full_time"
        self.include_internships_in_discovery = False
        self.work_authorization = "Permanent Resident"
        self.visa_status = ""
        self.target_roles = "Software Engineer"
        for k, v in kw.items():
            setattr(self, k, v)


def _job(title="Software Engineer", desc="Build services in Python for our platform."):
    return Job(title=title, company="Co", location="Austin, TX", remote=False,
               description=desc, source=JobSource.GREENHOUSE, external_id="x", url="u")


@pytest.mark.parametrize("title,desc,kind", [
    ("AI Trainer - Software Engineers", "Help train AI models by evaluating code.", "gig_ai_training"),
    ("Software Engineer (Freelance)", "Help train AI models. Flexible hours, paid per hour.", "gig_ai_training"),
    ("Machine Learning Engineer", "Train generative AI models on our GPU fleet.", "permanent"),
    ("Coding Expert", "Freelance: evaluate AI-generated code responses. Flexible hours.", "gig_ai_training"),
    ("Data Annotation Specialist (Python)", "Label data.", "gig_ai_training"),
    ("Backend Engineer", "This is a 6 month contract position with our team.", "contract"),
    ("Java Developer", "Our client, a Fortune 500 bank, is hiring.", "agency"),
    ("Software Engineering Intern", "Summer 2027.", "internship"),
    ("Software Engineer", "Build training pipelines for ML models at scale.", "permanent"),
])
def test_job_kinds(title, desc, kind):
    assert classify_job_kind(title, desc) == kind


def test_ai_training_gig_work_is_filtered_before_ranking():
    r = RuleFilter(_Prof()).filter(_job("AI Trainer - Software Engineers",
                                        "Help train AI models by evaluating code."))
    assert not r.passed and "gig" in r.reason


def test_ml_engineering_is_not_mistaken_for_gig_work():
    job = _job("Machine Learning Engineer",
               "You will build training infrastructure for our ranking models.")
    assert classify_job_kind(job.title, job.description) == "permanent"
    assert RuleFilter(_Prof()).filter(job).passed


def test_a_citizens_only_role_is_filtered_for_a_permanent_resident():
    desc = "Must be a U.S. citizen. Active secret clearance required."
    r = RuleFilter(_Prof()).filter(_job(desc=desc))
    assert not r.passed and "citizenship" in r.reason


def test_a_citizen_keeps_a_citizens_only_role():
    desc = "Must be a U.S. citizen."
    assert RuleFilter(_Prof(work_authorization="U.S. Citizen")).filter(_job(desc=desc)).passed


def test_an_unknown_status_never_blocks():
    desc = "Must be a U.S. citizen."
    assert RuleFilter(_Prof(work_authorization="")).filter(_job(desc=desc)).passed


def test_citizen_or_permanent_resident_is_not_citizens_only():
    assert requires_citizenship("Must be a U.S. citizen or permanent resident.") is None
    assert RuleFilter(_Prof()).filter(
        _job(desc="Must be a U.S. citizen or permanent resident.")).passed


def test_a_required_masters_filters_a_bachelors_holder():
    r = RuleFilter(_Prof()).filter(_job(desc="A Master's degree in Computer Science is required."))
    assert not r.passed and "degree" in r.reason


@pytest.mark.parametrize("desc", [
    "Master's degree required, or equivalent practical experience.",
    "Master's degree preferred.",
    "Bachelor's degree required.",
])
def test_equivalent_experience_or_preferred_is_not_a_requirement(desc):
    assert RuleFilter(_Prof()).filter(_job(desc=desc)).passed


def test_an_unknown_degree_never_blocks():
    assert RuleFilter(_Prof(degree="")).filter(
        _job(desc="A PhD is required.")).passed


def test_required_degree_parser_reads_the_level():
    assert required_degree_level("Requires a PhD in physics.")[0] == 3
    assert required_degree_level("Nothing about degrees here.") is None
