"""Role routing: the domain tokens that waved whole professions through.

`_role_matchers` accepts every >=4-character token of a target role that is not
generic, so "Full Stack Developer" accepted the bare term "full" and matched
every "Full Time ..." posting in the corpus. These jobs then occupied a queue
slot, a Tier-1 call and — because the pulse lane applied no country gate either
— a share of the day's finals. This pins the leaks shut, and pins the recall
that the fix must not cost.
"""
from app.discovery.title_filter import (
    keyword_hit, role_match_terms, role_title_match,
)


# ── The leaks ────────────────────────────────────────────────────────────────

def test_full_stack_does_not_match_full_time():
    roles = ["Full Stack Developer"]
    assert not role_title_match("Full Time Warehouse Associate", roles)
    assert not role_title_match("Full-Time Retail Sales Associate", roles)
    assert not role_title_match("Full Time Registered Nurse", roles)


def test_data_engineer_does_not_match_data_entry():
    roles = ["Data Engineer"]
    assert not role_title_match("Data Entry Clerk", roles)
    assert not role_title_match("Data Entry Specialist (Remote)", roles)


def test_software_engineer_does_not_match_software_sales_or_support():
    roles = ["Software Engineer"]
    assert not role_title_match("Software Sales Representative", roles)
    assert not role_title_match("Software Support Technician", roles)
    assert not role_title_match("Software Asset Management Analyst", roles)


def test_weak_tokens_do_not_rescue_junk_through_keyword_hit():
    # keyword_hit is what lets a department user's own titles past the
    # non-tech gate in _upsert. It must not fire on a domain word.
    assert not keyword_hit("Software Sales Representative", ["Software Engineer"])
    assert not keyword_hit("Data Entry Clerk", ["Data Engineer"])
    assert not keyword_hit("Full Time Cleaner", ["Full Stack Developer"])
    # ...but a genuinely distinctive token still rescues its own department.
    assert keyword_hit("Graduate Engineer (Civil)", ["Civil Engineer"])
    assert keyword_hit("Registered Nurse - ICU", ["Registered Nurse"])


# ── The recall the fix must not cost ─────────────────────────────────────────

def test_the_real_titles_still_match():
    assert role_title_match("Senior Data Engineer", ["Data Engineer"])
    assert role_title_match("Data Engineering Manager", ["Data Engineer"])
    assert role_title_match("Staff Software Engineer", ["Software Engineer"])
    assert role_title_match("Software Development Engineer II", ["Software Engineer"])
    assert role_title_match("Senior Full Stack Engineer", ["Full Stack Developer"])
    assert role_title_match("Fullstack Developer (Remote)", ["Full Stack Developer"])


def test_suffix_tolerance_survives_the_dropped_token():
    # "Platform Engineering" holds "platform engineer"; without the suffix rule
    # dropping the weak token "platform" would have lost it.
    assert role_title_match("Platform Engineering Lead", ["Platform Engineer"])
    assert role_title_match("Cloud Engineering Manager", ["Cloud Engineer"])


# ── The families that were missing ───────────────────────────────────────────

def test_language_titles_reach_the_software_family():
    assert role_title_match("Backend Engineer", ["Java Developer"])
    assert role_title_match("Software Engineer, Platform", ["Java Developer"])
    assert role_title_match("SDE II", ["Java Developer"])


def test_platform_and_reliability_are_one_family():
    for title in ("Site Reliability Engineer", "DevOps Engineer", "Cloud Engineer",
                  "Infrastructure Engineer"):
        assert role_title_match(title, ["Platform Engineer"]), title


def test_research_titles_reach_the_ai_family():
    for title in ("Applied Scientist", "Research Engineer", "Research Scientist"):
        assert role_title_match(title, ["AI Engineer"]), title


def test_ml_and_ai_are_still_one_market():
    assert role_title_match("Machine Learning Engineer", ["AI Engineer"])
    assert role_title_match("AI Engineer", ["Machine Learning Engineer"])


def test_sql_terms_agree_with_the_gate_on_the_weak_tokens():
    # role_match_terms feeds a substring ILIKE for the "my roles" board filter;
    # it must not offer "data" or "full" as a term either.
    terms = role_match_terms(["Data Engineer", "Full Stack Developer"])
    assert "data" not in terms
    assert "full" not in terms
    assert "data engineer" in terms
