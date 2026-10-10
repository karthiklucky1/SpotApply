"""Third review of the Tailoring Studio keyword and summary fixes (2026-10-09).

The second round let a list item skip the seen-once rule, and that exemption
reached things that are not skills: the heading above a list ("Key Skills"
left "key", "Essential Functions" came through whole), a benefits list under a
heading the offer test could not read ("What you'll get", "Total Rewards"),
the employer's name whenever the posting printed its email address, English
words that are also tool names ("excel in a busy cafe"), and "PLEASE NOTE:".
"pay" as company boilerplate dropped "Apple Pay". Removing one segment of a
bold pipe tagline left an unpaired "**" that the PDF printed as text. Each test
here fails on the code before this change.
"""
from __future__ import annotations

import re

import pytest

from app.tailoring.ats_keywords import _company_terms, analyze, extract_jd_phrases
from app.tailoring.evidence import remove_summary_sentences, summary_sentences
from app.tailoring.requirements import review

MASTER = ("# Pat Lee\npat@example.com\n\n## Experience\n**Analyst** | Foo | Jan 2020 - Present\n"
          "- Built Excel models for 3 regions.\n")
ITEMS = ["SQL", "Tableau", "Statistical modeling", "Stakeholder communication"]
WANT = {"sql", "tableau", "statistical modeling", "stakeholder communication"}


def _words(phrases):
    return {w for p in phrases for w in p.split()}


# ── 1. the heading above a list is not one of its items ─────────────────────

HEADINGS = {
    "Key Skills": {"key"}, "Technical Skills": {"technical"}, "Soft Skills": {"soft"},
    "Core Skills": {"core"}, "Mandatory Skills": {"mandatory"},
    "Primary Skills": {"primary"}, "Technical Requirements": {"technical"},
    "Essential Functions": {"essential", "functions"},
    "Physical Demands": {"physical", "demands"},
    "Core Competencies": {"core", "competencies"},
    "Additional Information": {"additional", "information"},
    "Must Haves": {"haves"}, "Our Tech Stack": {"tech", "stack"},
    "Certifications": {"certifications"}, "Education": {"education"},
}


def _posting(heading: str, fmt: str) -> str:
    intro = "We are hiring a data analyst to support our finance organization."
    outro = "We reply to every candidate within two weeks."
    if fmt == "get_text":          # how the scrapers store an HTML list
        return "\n".join([intro, heading, *ITEMS, outro])
    if fmt == "bullets":
        return "\n".join([intro, heading, *(f"- {i}" for i in ITEMS), outro])
    return (f"<p>{intro}</p><p><strong>{heading}</strong></p><ul>"
            + "".join(f"<li>{i}</li>" for i in ITEMS) + f"</ul><p>{outro}</p>")


@pytest.mark.parametrize("fmt", ["get_text", "bullets", "html"])
@pytest.mark.parametrize("heading", list(HEADINGS))
def test_a_list_heading_never_becomes_a_keyword(heading, fmt):
    jd = _posting(heading, fmt)
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert WANT <= set(phrases), phrases
    assert not HEADINGS[heading] & _words(phrases), phrases
    buttons = review(MASTER, MASTER, jd, company="Acme").missing_skills
    assert not HEADINGS[heading] & _words(buttons), buttons


def test_a_line_naming_a_tool_before_skills_is_still_an_item():
    """"Excel skills" is the tool Excel, not a section called Skills."""
    for jd in (("Office Manager\nRequirements\n- Excel skills\n- Calendar management\n"
                "- Vendor coordination\nYou will run the front desk."),
               ("Office Manager\nRequirements\nStrong Excel skills\nCalendar management\n"
                "Vendor coordination\nYou will run the front desk.")):
        phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
        assert "excel" in phrases, phrases
        assert "calendar management" in phrases, phrases


def test_a_nursing_posting_loses_its_headings_and_keeps_its_skills():
    """The live shape: <h3> headings over <ul> lists, stored with get_text."""
    jd = ("Acme Health is hiring a Registered Nurse for our Med-Surg unit.\nEssential Functions\n"
          "Wound care\nMedication administration\nPatient education\nTelemetry monitoring\n"
          "Physical Demands\nLift up to 50 lbs\nStand for long periods\nWhat You'll Get\n"
          "Tuition reimbursement\nShift differentials\nFree parking\n"
          "Acme Health is an equal opportunity employer.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme Health")
    assert {"wound care", "medication administration", "patient education",
            "telemetry monitoring"} <= set(phrases), phrases
    assert not {"essential functions", "physical demands", "tuition reimbursement",
                "shift differentials", "free parking"} & set(phrases), phrases


# ── 2. a benefits list under any offer heading is the offer ────────────────

OFFER_HEADINGS = ["What you'll get", "What you get", "What's in it for you", "Total Rewards",
                  "Our Total Rewards Package", "We provide", "What We Provide",
                  "In return, you'll receive:", "Life at Acme"]
PERKS = ["Gym membership", "Commuter benefits", "Home office stipend", "Catered lunches",
         "Wellness stipend", "Free parking"]
_PERK_RE = re.compile(r"gym|commuter|stipend|catered|lunch|parking|rewards|provide")


@pytest.mark.parametrize("bullet", ["- ", ""])
@pytest.mark.parametrize("heading", OFFER_HEADINGS)
def test_a_perks_list_under_any_offer_heading_is_not_a_keyword(heading, bullet):
    jd = ("Data Analyst\nRequirements:\n- SQL\n- Tableau\n- Statistical modeling\n" + heading
          + "\n" + "\n".join(bullet + p for p in PERKS))
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"sql", "tableau", "statistical modeling"} <= set(phrases), phrases
    assert not [p for p in phrases if _PERK_RE.search(p)], phrases
    buttons = review(MASTER, MASTER, jd, company="Acme").missing_skills
    assert not [p for p in buttons if _PERK_RE.search(p)], buttons


def test_the_job_after_an_offer_heading_lookalike_is_kept():
    """"What you'll get to do" is the job, and "Total rewards" in an HR list
    is a field: neither may wipe out the skills listed after it."""
    jd = ("Data Engineer\nWhat you'll get to do\n- Tune Snowflake warehouses\n"
          "- Write Looker dashboards\n- Stakeholder reporting\n")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"snowflake", "looker"} <= _words(phrases), phrases
    assert "stakeholder reporting" in phrases, phrases
    hr = ("HR Business Partner\nRequirements\nEmployee relations\nPerformance management\n"
          "Total rewards\nWorkday HCM\nHRIS reporting\nEmployee relations casework is daily.")
    phrases = extract_jd_phrases(hr, top_n=18, company="Acme")
    assert {"workday hcm", "hris reporting", "performance management"} <= set(phrases), phrases


# ── 3. the company's email address or domain is not the word in prose ───────

NOTION = ("About Notion\nNotion is the connected workspace for docs and projects. Notion is used "
          "by millions.\nWhat you'll do\nBuild Python services on AWS. Own the sync engine in "
          "TypeScript.\nNotion is an equal opportunity employer. If you need an accommodation, "
          "email accommodations@notion.so.")


@pytest.mark.parametrize("company", ["Notion Labs, Inc.", "Notion Labs", "Notionlabs"])
def test_an_accommodation_email_does_not_bring_back_the_employer(company):
    assert "notion" in _company_terms(company, NOTION)
    missing = analyze(NOTION, "Python AWS", company=company).missing
    assert "typescript" in missing and "notion" not in missing, missing
    buttons = review(MASTER, MASTER, NOTION, company=company).missing_skills
    assert not [b for b in buttons if "notion" in b], buttons


def test_a_domain_is_not_prose_but_a_lower_case_word_still_is():
    jd = ("Acme Labs is hiring a Robotics Engineer. Acme builds warehouse robots. Acme is "
          "remote-first. You will write ROS nodes in C++. Motion planning experience required. "
          "Experience with ROS and motion planning. Learn more at https://www.acme.com.")
    assert "acme" in _company_terms("Acme Labs", jd)
    assert "acme" not in extract_jd_phrases(jd, top_n=18, company="Acme Labs")
    # "at scale" in prose still means the word, whatever the address says.
    sj = "Scale Labs builds data tools at scale. Visit scale.com. Python required."
    assert "scale" not in _company_terms("Scale Labs", sj)


# ── 4. English words that are also tool names, and "PLEASE NOTE:" ───────────

def test_english_words_that_name_tools_are_prose_in_lower_case():
    jd = ("Barista. Prepare espresso drinks and serve guests. Espresso preparation required. "
          "Guest experience matters. You will excel in a busy cafe and stay on your feet "
          "throughout the workday. Our cafe sits at the confluence of two rivers. Take the helm "
          "at our summer bash, restock the yarn display and reach the apex of service.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert "espresso" in phrases
    assert not {"excel", "workday", "confluence", "helm", "bash", "yarn", "apex"} & _words(phrases), \
        phrases


def test_the_same_words_written_as_tools_still_count():
    jd = ("Platform Engineer. Deploy services with Helm charts. Document runbooks in Confluence.\n"
          "Requirements\n- Bash\n- Excel")
    words = _words(extract_jd_phrases(jd, top_n=18, company="Acme"))
    assert {"helm", "confluence", "bash", "excel"} <= words, words


def test_a_please_note_lead_in_is_not_a_keyword():
    jd = ("Backend Engineer. You will build Python services on AWS. Python and AWS required.\n"
          "NOTE: This position requires occasional travel. PLEASE NOTE: applicants must be "
          "authorized to work.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"python", "aws"} <= set(phrases)
    assert not {"please", "note"} & _words(phrases), phrases
    buttons = review(MASTER, MASTER, jd, company="Acme").missing_skills
    assert not {"please", "note"} & _words(buttons), buttons
    # A clinical note is still a skill.
    rn = "Registered Nurse. Document each SOAP note in Epic. SOAP note audits monthly."
    assert "soap note" in extract_jd_phrases(rn, top_n=18, company="Acme")


# ── 5. "Apple Pay" is a skill; the offer's pay is not ───────────────────────

def test_apple_pay_is_a_skill_and_the_pay_range_is_not():
    for jd in (("Mobile Engineer. Integrate Apple Pay and Google Pay into our iOS app. Swift and "
                "iOS required. Apple Pay experience is a plus."),
               "Mobile Engineer. Integrate Apple Pay, Google Pay and local wallets in Swift."):
        phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
        assert {"apple pay", "google pay"} <= set(phrases), phrases
        assert not [p for p in phrases if p.startswith("integrate")], phrases
        buttons = review(MASTER, MASTER, jd, company="Acme").missing_skills
        assert "apple pay" in buttons and "integrate apple" not in buttons, buttons
    # The offer's pay stays out, repeated or not, including the federal
    # contractor line most US postings carry.
    offer = ("Backend Engineer. Python required. Python services. Pay transparency: the pay range "
             "is $100k-$140k. Competitive pay. Pay range varies by city. Base pay is reviewed "
             "yearly. Base pay bands are public. The pay rate is fixed. Pay rate bands too.\n"
             "View the Pay Transparency Nondiscrimination Provision here.")
    phrases = extract_jd_phrases(offer, top_n=18, company="Acme")
    assert "python" in phrases
    assert not [p for p in phrases if "pay" in p.split()], phrases


# ── 6. removing a pipe segment never leaves an unpaired "**" ────────────────

_RESUME = """# Alex Tenant
alex@example.com | (555) 555-0100

## Summary
{S}

## Experience
**Software Engineer** | NTT Data | Jan 2021 - Present
- Built REST APIs with FastAPI serving 2,500 requests per minute.
"""
CLAIM = "Translating API documentation into tested connectors"


def _summary_after(summary: str) -> str:
    md = _RESUME.replace("{S}", summary)
    assert CLAIM in " ".join(summary_sentences(md))
    out, removed = remove_summary_sentences(md, [CLAIM], _RESUME.replace("{S}", "Backend engineer."))
    assert [r.rstrip(".") for r in removed] == [CLAIM]
    return out.split("## Summary\n")[1].split("\n")[0]


@pytest.mark.parametrize("summary,expected", [
    (f"**Integrations Engineer | {CLAIM}**", "**Integrations Engineer**"),
    (f"**{CLAIM} | Integrations Engineer**", "**Integrations Engineer**"),
    (f"*Integrations Engineer | {CLAIM}*", "*Integrations Engineer*"),
    (f"**Integrations Engineer | {CLAIM} | Python | AWS**", "**Integrations Engineer | Python | AWS**"),
    (f"**Integrations Engineer** | {CLAIM}", "**Integrations Engineer**"),
    (f"Rated 5* by partners | Integrations Engineer | {CLAIM}",
     "Rated 5* by partners | Integrations Engineer"),
])
def test_emphasis_across_a_removed_pipe_segment_stays_paired(summary, expected):
    from app.tailoring.render import _runs
    line = _summary_after(summary)
    assert line == expected
    if "5*" not in line:
        assert "*" not in "".join(r.text for r in _runs(line)), _runs(line)
