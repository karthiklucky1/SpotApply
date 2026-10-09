"""Fourth review of the Tailoring Studio keyword fixes (2026-10-09).

The third round taught the offer test new headings, and some of them named
the JOB as well as the package: "Total Rewards" anywhere on a line wiped out an
HR Total Rewards Analyst's posting ("About the Total Rewards Team", the title
line, a "Total Rewards Administration" skill), "<anything> We Offer/Provide"
erased a tutor's subjects and a therapist's services, and "Life at Acme" ran
on through "The Opportunity". A benefits list under a heading no rule names
("Why You'll Love Working Here", "What's On Offer", "Life @ Acme") still came
through as "gym membership". "shop pay" and "amazon pay" read a technician's
or a driver's pay as a wallet. A bulleted "- Basic Math Skills" was taken for
a section heading and lost. Every test here fails on the code before this
change except two guards that pass on both: the package headings still open
the offer, and a perk word the posting repeats still counts.
"""
from __future__ import annotations

import re

import pytest

from app.tailoring.ats_keywords import _named_tokens, _without_offer, extract_jd_phrases
from app.tailoring.requirements import review

MASTER = ("# Pat Lee\npat@example.com\n\n## Experience\n**Analyst** | Foo | Jan 2020 - Present\n"
          "- Built reports for 3 regions.\n")


def _words(phrases):
    return {w for p in phrases for w in p.split()}


def _buttons(jd, company="Acme"):
    return review(MASTER, MASTER, jd, company=company).missing_skills


# ── 1. an offer heading names the package, never the job ────────────────────

TR_ANALYST = (
    "About the Total Rewards Team\n"
    "Our team designs compensation and benefits programs for 4,000 employees across 12 "
    "countries.\n"
    "As a Total Rewards Analyst you will build compensation models in Excel, maintain job "
    "architecture in Workday HCM, run market pricing with Radford and Mercer surveys, and "
    "support the annual merit cycle.\n"
    "What You'll Do\nRun market pricing analyses\nMaintain salary structures in Workday\n"
    "Prepare HRIS reporting for leadership\n"
    "Benefits\nMedical, dental and vision\nGym membership")
TR_ANALYST_HTML = (
    "<p><strong>About the Total Rewards Team</strong></p><p>Our team designs compensation and "
    "benefits programs for 4,000 employees across 12 countries.</p><p>As a Total Rewards "
    "Analyst you will build compensation models in Excel, maintain job architecture in Workday "
    "HCM, run market pricing with Radford and Mercer surveys, and support the annual merit "
    "cycle.</p><p><strong>What You'll Do</strong></p><ul><li>Run market pricing analyses</li>"
    "<li>Maintain salary structures in Workday</li><li>Prepare HRIS reporting for leadership"
    "</li></ul><p><strong>Benefits</strong></p><ul><li>Medical, dental and vision</li>"
    "<li>Gym membership</li></ul>")


@pytest.mark.parametrize("jd", [TR_ANALYST, TR_ANALYST_HTML], ids=["get_text", "html"])
def test_a_total_rewards_analyst_keeps_the_job(jd):
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"market", "pricing", "workday", "hcm", "excel", "radford", "mercer"} <= \
        _words(phrases), phrases
    # The real benefits list after it is still the offer.
    assert not {"gym", "membership", "dental", "vision"} & _words(phrases), phrases
    assert {"pricing", "workday", "mercer"} <= _words(_buttons(jd)), _buttons(jd)


def test_a_total_rewards_title_line_is_the_job():
    jd = ("Total Rewards Analyst\nAcme is seeking an analyst to build compensation models in "
          "Excel and maintain Workday HCM data. You will partner with HR business partners on "
          "salary benchmarking using Radford data and prepare HRIS reporting.\n"
          "Minimum 3 years in compensation analysis.\n")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"workday hcm", "radford"} <= set(phrases), phrases
    assert {"workday hcm", "radford"} <= set(_buttons(jd)), _buttons(jd)


def test_a_total_rewards_skill_in_a_list_is_an_item():
    jd = ("Acme is hiring an HR Generalist to support our people operations team in Austin.\n"
          "Key Skills\nWorkday HCM\nTotal Rewards Administration\nHRIS Reporting\n"
          "Employee Relations\nFMLA Compliance\nBenefits Administration")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"workday hcm", "hris reporting", "employee relations", "fmla compliance"} <= \
        set(phrases), phrases


@pytest.mark.parametrize("heading", [
    "Total Rewards", "Our Total Rewards", "Total Rewards Package", "Our Total Rewards Package:",
    "TOTAL REWARDS", "What We Provide", "Here's what we offer", "In return, we offer:",
    "We provide", "What We Can Offer You", "Benefits We Offer", "Life at Acme",
    "Life at Acme Health",
])
def test_package_headings_still_open_the_offer(heading):
    assert _without_offer(f"{heading}\nFree parking").split("\n")[1] == ""


@pytest.mark.parametrize("heading", [
    "About the Total Rewards Team", "Total Rewards Analyst", "Total Rewards Manager",
    "Total Rewards Specialist", "Total Rewards Administration", "Total Rewards Strategy",
    "Total Rewards Team", "Subjects We Offer", "Services We Provide", "Services We Offer",
    "Products We Offer", "Life at Acme is fast-paced",
])
def test_a_heading_about_the_job_does_not_open_the_offer(heading):
    assert _without_offer(f"{heading}\nFree parking").split("\n")[1] == "Free parking"


TUTOR = ("Acme Tutoring is hiring part-time tutors for high school students.\nSubjects We Offer\n"
         "AP Calculus\nOrganic Chemistry\nSAT Prep\nAP Physics\n"
         "Tutors must hold a bachelor's degree.")
THERAPY = ("Acme Rehab is hiring a licensed therapist for home health visits.\n"
           "Services We Provide\nPhysical therapy\nOccupational therapy\n"
           "Speech-language pathology\nWound care\nValid state license required.")
INSURANCE = ("Acme Insurance is hiring a licensed sales agent to advise seniors.\n"
             "Products We Offer\nMedicare Advantage\nMedicare Supplement\nFinal expense\n"
             "Term life insurance\nActive state life and health license required.")


@pytest.mark.parametrize("jd,want", [
    (TUTOR, {"ap calculus", "organic chemistry", "sat prep", "ap physics"}),
    (THERAPY, {"speech-language pathology", "wound care"}),
    (INSURANCE, {"medicare", "final expense"}),
], ids=["subjects", "services", "products"])
def test_what_the_job_offers_its_clients_is_the_job(jd, want):
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert want <= set(phrases), phrases


def test_life_at_acme_ends_at_the_opportunity():
    jd = ("Life at Acme\nAcme builds accounting software for 40,000 small businesses.\n"
          "The Opportunity\nAs a Senior Data Engineer you will own our Snowflake warehouse, "
          "model data with dbt, orchestrate Airflow DAGs and build streaming ingestion with Kafka "
          "and Flink. You will partner with finance on revenue recognition data.\n"
          "What You'll Do\nBuild CDC pipelines with Debezium\n"
          "Own data quality checks with Great Expectations")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"dbt", "dags", "flink"} <= set(phrases), phrases


# ── 2. perk vocabulary is the offer under any heading ───────────────────────

PERKS = ["Gym membership", "Catered lunches", "Commuter benefits", "Wellness stipend",
         "Unlimited PTO", "Tuition reimbursement", "Employee discount", "Free snacks",
         "401(k) matching"]
_PERK_RE = re.compile(r"gym|membership|lunch|commuter|stipend|pto|tuition|discount|snack|"
                      r"wellness|matching")


@pytest.mark.parametrize("style", ["bullets", "title_case_get_text"])
@pytest.mark.parametrize("heading", ["Why You'll Love Working Here", "What's On Offer",
                                     "Life @ Acme"])
def test_a_perks_list_under_an_unnamed_heading_is_not_a_keyword(heading, style):
    if style == "bullets":
        items = [f"- {p}" for p in PERKS]
    else:
        items = [p.title().replace("Pto", "PTO") for p in PERKS]
    jd = ("We are hiring a data analyst to support our finance organization with reporting.\n"
          "Requirements\n- SQL\n- Tableau\n" + heading + "\n" + "\n".join(items) + "\n")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"sql", "tableau"} <= set(phrases), phrases
    assert not [p for p in phrases if _PERK_RE.search(p)], phrases
    buttons = _buttons(jd)
    assert not [p for p in buttons if _PERK_RE.search(p)], buttons


def test_a_repeated_perk_word_skill_still_counts():
    """The backstop removes the seen-once exemption; it never removes a phrase
    the posting repeats."""
    jd = ("Membership Sales Representative. Drive membership sales at our fitness clubs. "
          "Membership sales experience required.\nRequirements\n- CRM software\n"
          "- Membership sales")
    assert "membership sales" in extract_jd_phrases(jd, top_n=18, company="Acme")


# ── 3. the pay in a technician's or driver's posting is not a wallet ────────

AUTO_TECH = ("Automotive Technician\nWe are hiring an Automotive Technician to diagnose and "
             "repair customer vehicles.\nRequirements\nASE certification\n"
             "Valid driver's license\nOwn hand tools\nWe pay flat rate with guaranteed shop pay "
             "for training hours and comebacks.\n")
CAFE = ("Barista\nPrepare espresso drinks and keep the cafe clean. Shop pay starts at $17 per "
        "hour plus tips.\nEspresso experience a plus.\n")
DRIVER = ("Delivery Driver\nDeliver packages safely along an assigned route. Amazon pay rates "
          "start at $21 per hour. Valid driver's license required. Clean driving record.\n")


@pytest.mark.parametrize("jd,company,wallet", [
    (AUTO_TECH, "Acme", "shop pay"),
    (CAFE, "Acme", "shop pay"),
    (DRIVER, "Speedy Logistics LLC", "amazon pay"),
], ids=["auto_technician", "cafe", "delivery_driver"])
def test_shop_pay_and_amazon_pay_are_not_keywords(jd, company, wallet):
    phrases = extract_jd_phrases(jd, top_n=18, company=company)
    assert wallet not in phrases, phrases
    assert wallet not in _buttons(jd, company), _buttons(jd, company)


def test_guaranteed_shop_pay_leaves_the_technician_skills():
    phrases = extract_jd_phrases(AUTO_TECH, top_n=18, company="Acme")
    assert {"automotive technician", "ase certification"} <= set(phrases), phrases
    assert not _words(phrases) & {"shop", "pay"}, phrases
    # A wallet that is a skill is still one.
    jd = "Mobile Engineer. Integrate Apple Pay and Google Pay into our iOS app in Swift."
    assert {"apple pay", "google pay"} <= set(extract_jd_phrases(jd, top_n=18, company="Acme"))


# ── 4. a bullet is an item, never a section heading ─────────────────────────

@pytest.mark.parametrize("items", [
    ["Customer Service Skills", "Cash Handling", "Basic Math Skills", "Visual merchandising"],
    ["Customer service skills", "Cash handling", "Basic math skills", "Visual merchandising"],
], ids=["title_case", "sentence_case"])
def test_a_bulleted_skill_ending_in_skills_is_kept(items):
    jd = ("Sales Associate\nHelp customers find products and run the register.\nRequirements\n"
          + "".join(f"- {i}\n" for i in items))
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"basic math", "customer service", "cash handling", "visual merchandising"} <= \
        set(phrases), phrases
    assert "basic math" in _buttons(jd), _buttons(jd)


def test_a_bullet_keeps_its_capitals_and_a_heading_does_not():
    named = _named_tokens("Core Competencies\n- Basic Math Skills")
    assert "math" in named and "competencies" not in named, named
