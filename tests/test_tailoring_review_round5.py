"""Fifth review of the Tailoring Studio keyword fixes (2026-10-09).

Round four limited "Total Rewards" to three exact headings and anchored "we
offer", so common package headings ("Total Rewards & Benefits", "Total
Rewards at Acme", "Compensation & Total Rewards", "At Acme, we offer:")
stopped opening the offer and their benefits came through as keywords. It
also made every bullet an item, so "<li>Strong technical skills</li>" left
"technical" behind. And the heading "What You'll Bring" has always produced
the keyword "ll bring". Every test here fails on the code before this change.
"""
from __future__ import annotations

import pytest

from app.tailoring.ats_keywords import _without_offer, extract_jd_phrases
from app.tailoring.requirements import review

MASTER = ("# Pat Lee\npat@example.com\n\n## Experience\n**Analyst** | Foo | Jan 2020 - Present\n"
          "- Built reports for 3 regions.\n")

SWE_BODY = ("About the Role\nAcme is hiring a Backend Engineer to build the services behind our "
            "logistics platform.\nWhat You'll Do\nBuild Python and Go microservices on AWS\n"
            "Own PostgreSQL schemas and Kafka pipelines\nImprove observability with Datadog\n"
            "What You'll Bring\n4+ years of backend engineering experience\n"
            "Experience with Kubernetes and Terraform\n")
BENEFITS = ("Medical, dental, and vision insurance\n401(k) with company match\nFlexible PTO\n"
            "Paid parental leave\nLife insurance\nEmployee assistance program\nCommuter benefits\n"
            "Learning and development stipend\nHome office setup\n")
PERKS = {"employee assistance program", "home office setup", "life insurance", "learning",
         "commuter benefits", "parental leave"}


def _words(phrases):
    return {w for p in phrases for w in p.split()}


@pytest.mark.parametrize("heading", [
    "Total Rewards & Benefits", "Total Rewards at Acme", "Compensation & Total Rewards",
    "Total Rewards Program", "At Acme, we offer:", "Total Rewards", "Our Total Rewards Package",
])
@pytest.mark.parametrize("html", [False, True], ids=["get_text", "html"])
def test_package_headings_with_companions_open_the_offer(heading, html):
    if html:
        items = "".join(f"<li>{x}</li>" for x in BENEFITS.strip().splitlines())
        jd = ("<p><strong>What You'll Do</strong></p><ul><li>Build Python and Go microservices on "
              "AWS</li><li>Own PostgreSQL schemas and Kafka pipelines</li></ul>"
              f"<p><strong>{heading}</strong></p><ul>{items}</ul>")
    else:
        jd = SWE_BODY + heading + "\n" + BENEFITS
    kw = extract_jd_phrases(jd, company="Acme")
    assert not (set(kw) & PERKS), kw
    assert {"python", "postgresql", "kafka"} <= set(kw), kw


@pytest.mark.parametrize("heading", ["Total Rewards Analyst", "About the Total Rewards Team",
                                     "Total Rewards Strategy"])
def test_the_job_named_total_rewards_is_still_the_job(heading):
    jd = (heading + "\nRun market pricing analyses\nMaintain salary structures in Workday\n"
          "Build compensation models in Excel\n")
    kept = _without_offer(jd)
    assert "Run market pricing analyses" in kept and "salary structures in Workday" in kept


SKILL_BULLETS = {
    "get_text": ("Administrative Assistant\nRequirements\n- Strong technical skills\n"
                 "- Good soft skills\n- Key skills\n- Excellent communication skills\n"
                 "- Basic Math Skills\n- Prepare expense reports in Concur\n"),
    "html": ("<p>Acme is hiring a Customer Success Manager.</p><p><strong>Requirements</strong>"
             "</p><ul><li>Strong technical skills</li><li>Excellent communication skills</li>"
             "<li>Key Responsibilities</li><li>Basic Math Skills</li>"
             "<li>Experience with Salesforce and Gainsight</li></ul>"),
}


@pytest.mark.parametrize("name", list(SKILL_BULLETS))
def test_a_heading_shaped_bullet_leaves_no_qualifier_behind(name):
    jd = SKILL_BULLETS[name]
    kw = extract_jd_phrases(jd, company="Acme")
    buttons = review(MASTER, MASTER, jd, company="Acme").missing_skills
    junk = {"technical", "soft", "key"}
    assert not (_words(kw) & junk), kw
    assert not (_words(buttons) & junk), buttons
    assert "basic math" in kw, kw              # a real skill in the same shape stays
    assert "communication" in _words(kw), kw


def test_contraction_tails_are_not_keywords():
    jd = SWE_BODY + "You're a self-starter. We've built tools you'll love.\n"
    kw = extract_jd_phrases(jd, company="Acme")
    assert not (_words(kw) & {"ll", "re", "ve", "bring"}), kw
