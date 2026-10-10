"""The extension's answer rules, run as the extension runs them (2026-09-30).

An audit of the MV3 extension (browser-driven, synthetic profiles and forms)
found the copilot answering employer questions the profile does not answer:

- "Five years of Rust?"            -> Yes, from seven years TOTAL tenure
- "Bachelor's in computer science?" -> Yes, for a Bachelor of Arts
- "Python AND Kubernetes?"          -> Yes, with only Python listed
- "UK Citizen"                      -> Yes to "authorized to work in the US"
- "Not authorized"                  -> Yes to "authorized without sponsorship"
- requires_sponsorship=false        -> No to "can you work WITHOUT sponsorship?"
- greenhouse.io.unrelated.example   -> treated as a trusted ATS

These tests load the real functions out of extension/content.js and run them
in Node, so the rule a user's browser applies is the rule tested here. Every
wrong-answer case expects Yes/No only when the profile PROVES it, and None
(= left for the applicant) otherwise. Synthetic profiles only.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTENT = (ROOT / "extension/content.js").read_text()
BACKGROUND = (ROOT / "extension/background.js").read_text()
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

FUNCTIONS = [
    "deaccent", "isDemographicQuestion", "classifyScreeningQuestion",
    "yearsQuestionIsGeneric", "degreeSubjectAsked", "skillMonthsFor", "subjectMatchesTitles", "_countryIn",
    "_normCountry", "workAuthFacts", "interpretWorkAuthQuestion", "hostIs",
    "isTrustedATSHost", "fileMatchesAccept", "currentEmployer", "residenceCountry",
    "eeoStated", "eeoCategory", "eeoPickOption", "eeoValueFor",
]
CONSTS = ["_DEMOGRAPHIC_RE", "_GENERIC_YEARS_WORDS", "_COUNTRY_WORDS", "ATS_SUFFIXES",
          "_OTHER_DOC_RE", "_EEO_DECLINE_RE", "_EEO_CATEGORIES", "_EEO_PLACEHOLDER_RE"]


def _balanced(src: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the bracket matching the first ``open_ch`` at/after start
    (string/regex-literal naive, which is enough for these declarations)."""
    i = src.index(open_ch, start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == open_ch:
            depth += 1
        elif src[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j + 1
    raise AssertionError(f"unbalanced from {start}")


def _function(src: str, name: str) -> str:
    m = re.search(r"^(async\s+)?function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    assert m, f"function {name} not found in content.js"
    body_open = src.index(")", m.end() - 1)
    return src[m.start():_balanced(src, body_open, "{", "}")]


def _const(src: str, name: str) -> str:
    m = re.search(r"^const\s+" + re.escape(name) + r"\s*=\s*", src, re.M)
    assert m, f"const {name} not found"
    rest = src[m.end():]
    if rest.startswith("["):
        end = m.end() + _balanced(rest, 0, "[", "]")
    else:
        end = src.index(";\n", m.end())
    return src[m.start():end] + ";"


# content.js state the extracted functions read (eeoStated reads the live pack).
PRELUDE = "\n".join(["let _copilotPack = null;"] +
                    [_const(CONTENT, c) for c in CONSTS] +
                    [_function(CONTENT, f) for f in FUNCTIONS])


def run_js(calls: list) -> list:
    """Evaluate ``[fn, args...]`` calls against the extracted functions."""
    script = PRELUDE + """
const calls = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = calls.map(([fn, ...args]) => {
  const v = eval(fn)(...args);
  return v === undefined ? null : v;
});
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run([NODE, "-e", script], input=json.dumps(calls),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def ask(question: str, **profile) -> object:
    return run_js([["classifyScreeningQuestion", question, profile]])[0]


BA = dict(years_experience=7, key_skills="Python, SQL", degree="Bachelor of Arts",
          education=[{"degree": "Bachelor of Arts"}],
          work_authorization="US Citizen", requires_sponsorship=False)


# ── screening: only what the profile proves ─────────────────────────────────

@pytest.mark.parametrize("question,want", [
    ("Do you have 5+ years of Rust experience?", False),              # not in the résumé = No (owner, 09-30)
    ("Do you have 5+ years of professional experience?", True),
    ("Do you have 10+ years of relevant work experience?", False),
    ("Do you have a Bachelor's degree in Computer Science?", None),   # audit
    ("Do you have a Bachelor's degree or higher?", True),
    ("Do you have a Master's degree?", None),                         # below the bar: not an auto "No"
    ("Do you have experience with Python and Kubernetes?", False),    # Kubernetes is not in the résumé
    ("Do you have experience with Python?", True),
    ("Do you have experience with Python or Go?", True),
    ("Are you proficient in SQL, Python?", True),
    ("Do you have experience with Rust?", False),                     # not in the résumé = No (owner, 09-30)
    ("Do you have experience with building products in a regulated healthcare environment?", None),
    # found filling a real profile (2026-09-30): a required-field "*" made a
    # generic question look like a named skill, and "Apache Kafka" didn't
    # answer "Kafka".
    ("Do you have at least 5 years of relevant work experience? *", True),
    ("Do you have at least 10 years of relevant work experience? *", False),
    ("Do you have experience with SQL and Python? *", True),
])
def test_screening_answers_only_what_the_profile_proves(question, want):
    assert ask(question, **BA) is want


def test_a_listed_skill_answers_its_distinctive_part():
    kafka = dict(BA, key_skills="Apache Kafka, Kubernetes, React.js")
    assert ask("Do you have experience with Kafka and Kubernetes?", **kafka) is True
    assert ask("Do you have experience with React?", **kafka) is True
    assert ask("Do you have experience with Apache Spark?", **kafka) is False  # "apache" alone proves nothing


def test_skill_years_come_from_the_resume():
    """Owner's rule (2026-09-30): a skill-years question gets Yes or No from the
    résumé — dated months first, else the length of the jobs the skill was used
    in; a skill the résumé does not show is No (the user could not say Yes)."""
    q = "Do you have 3+ years of Data Science experience?"
    assert ask(q, **BA) is False                                       # not in the résumé
    assert ask(q, **dict(BA, skill_months={"data science": 48})) is True
    assert ask(q, **dict(BA, skill_months={"data science": 24})) is False
    assert ask(q, **dict(BA, skill_role_months={"data science": 40})) is True   # used in 40 months of jobs
    both = "Do you have 3+ years of Python and SQL experience?"
    assert ask(both, **dict(BA, skill_months={"python": 48})) is False
    assert ask(both, **dict(BA, skill_months={"python": 48, "sql": 40})) is True
    # A description, not a skill: never answered No.
    assert ask("Do you have 3+ years of experience in a fast paced early stage startup environment?", **BA) is None


def test_a_field_named_by_the_users_own_titles_uses_total_years():
    eng = dict(BA, years_experience=3, current_title="Software Engineer",
               work_experience=[{"title": "Software Developer"}])
    assert ask("Do you have 3+ years of professional software engineering experience?", **eng) is True
    assert ask("Do you have 5+ years of software development experience?", **eng) is False
    assert ask("Do you have 3+ years of nursing experience?", **eng) is False   # not their field, not in résumé


def test_a_degree_subject_needs_the_subject():
    cs = dict(BA, degree="B.S. Computer Science", education=[{"degree": "B.S. Computer Science"}])
    q = "Do you have a Bachelor's degree in Computer Science or a related field?"
    assert ask(q, **cs) is True
    assert ask(q, **BA) is None
    # A field the user's own profile records counts; an extractor's guess does not.
    assert ask(q, **dict(BA, degree_fields=["Computer Science"])) is True
    assert ask(q, **dict(BA, education=[{"degree": "Bachelor of Arts",
                                         "field_of_study": "Computer Science"}])) is None


# ── work authorization: one interpreter, every wording ───────────────────────

def auth(question: str, **profile):
    return run_js([["interpretWorkAuthQuestion", question, profile]])[0]


@pytest.mark.parametrize("profile,question,want", [
    # audit: another country's citizenship says nothing about the US
    ({"work_authorization": "UK Citizen"}, "Are you legally authorized to work in the United States?", None),
    ({"work_authorization": "UK Citizen"}, "Are you legally authorized to work in the United Kingdom?", True),
    ({"work_authorization": "US Citizen"}, "Are you legally authorized to work in the US?", True),
    ({"work_authorization": "F-1 OPT"}, "Are you authorized to work in the United States?", True),
    ({"work_authorization": "Needs Sponsorship"}, "Are you authorized to work in the US?", None),
    ({"work_authorization": "Not authorized"}, "Are you authorized to work in the US?", False),
    # audit: combined question with a NOT-authorized profile
    # combined "authorized WITHOUT sponsorship?": the applicant decides, unless
    # there is nothing to decide (owner's rule, 2026-09-30)
    ({"work_authorization": "Not authorized", "requires_sponsorship": False},
     "Are you authorized to work in the US without sponsorship?", None),
    ({"work_authorization": "US Citizen", "requires_sponsorship": False},
     "Are you authorized to work in the US without sponsorship?", True),
    ({"work_authorization": "H1B", "requires_sponsorship": True},
     "Are you authorized to work in the US without sponsorship?", None),
    # audit: inverted sponsorship wording
    # "without sponsorship" in any wording = the combined question: the
    # applicant decides unless nothing is in doubt (citizen / green card)
    ({"work_authorization": "US Citizen", "requires_sponsorship": False}, "Can you work without visa sponsorship?", True),
    ({"requires_sponsorship": False}, "Can you work without visa sponsorship?", None),
    # found filling a real profile: the longer Lever wording was read as
    # "do you require sponsorship?" and answered Yes for someone who needs it
    ({"work_authorization": "US Citizen", "requires_sponsorship": True},
     "Are you authorized to work in the US without the need for visa sponsorship now or in the future?", None),
    ({"work_authorization": "US Citizen", "requires_sponsorship": False},
     "Are you authorized to work in the US without the need for visa sponsorship now or in the future?", True),
    ({"requires_sponsorship": True},
     "Are you able to work for us without the need for employer sponsorship?", None),
    ({"work_authorization": "Green Card", "requires_sponsorship": False},
     "Are you able to work for us without the need for employer sponsorship?", True),
    ({"requires_sponsorship": True}, "Do you not require visa sponsorship?", None),
    # an expired dated status: the server's verdict wins over the status text
    ({"work_authorization": "F-1 OPT", "authorized_now": None},
     "Are you legally authorized to work in the United States?", None),
    ({"work_authorization": "F-1 OPT", "authorized_now": True},
     "Are you legally authorized to work in the United States?", True),
    ({"requires_sponsorship": True}, "Can you work without visa sponsorship?", None),
    ({"requires_sponsorship": True}, "Will you now or in the future require sponsorship?", True),
    ({"requires_sponsorship": False}, "Will you now or in the future require visa sponsorship?", False),
    ({"requires_sponsorship": None}, "Will you require sponsorship?", None),   # dated status: user's call
    # the dashboard's short "Citizen" means the country the user searches in
    ({"work_authorization": "Citizen"}, "Are you legally authorized to work in the US?", True),
    ({"work_authorization": "Citizen", "preferred_country": "Canada"},
     "Are you legally authorized to work in the US?", None),
])
def test_work_authorization_answers(profile, question, want):
    assert auth(question, **profile) is want


def test_every_control_type_reads_the_same_interpreter():
    """Radios go through classifyScreeningQuestion, selects/text through
    answerWorkAuthField — both must call interpretWorkAuthQuestion."""
    assert "interpretWorkAuthQuestion(t, pack)" in _function(CONTENT, "classifyScreeningQuestion")
    assert "interpretWorkAuthQuestion(l, pack)" in _function(CONTENT, "answerWorkAuthField")


# ── hosts: exact or subdomain, never a substring ─────────────────────────────

@pytest.mark.parametrize("host,want", [
    ("greenhouse.io.unrelated.example", False),                      # audit
    ("evilgreenhouse.io", False),
    ("boards.greenhouse.io", True),
    ("greenhouse.io", True),
    ("jobs.lever.co", True),
    ("lever.co.phish.test", False),
    ("acme.wd5.myworkdayjobs.com", True),
])
def test_ats_hosts_match_exactly(host, want):
    assert run_js([["isTrustedATSHost", host]])[0] is want


def test_background_and_content_trust_the_same_hosts():
    assert _const(BACKGROUND, "ATS_SUFFIXES") == _const(CONTENT, "ATS_SUFFIXES")
    assert "ATS_HOSTS" not in BACKGROUND, "the substring regex must not come back"


# ── uploads, employer, residence, protected questions ────────────────────────

def test_the_forms_accepted_formats_are_respected():
    js = """(accept, name, mime) => fileMatchesAccept(
        { getAttribute: (k) => (k === 'accept' ? accept : null) }, name, mime)"""
    cases = [(".pdf", "resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", False),
             (".pdf,.docx", "resume.docx", "", True),
             ("application/pdf", "resume.pdf", "application/pdf", True),
             ("", "resume.docx", "", True)]
    out = run_js([[js, a, n, m] for a, n, m, _ in cases])
    assert out == [c[-1] for c in cases]


def test_the_current_company_is_the_current_employer():
    jobs = [{"company": "Old Co", "end_date": "Dec 2023"}, {"company": "Now Co", "end_date": "Present"}]
    assert run_js([["currentEmployer", {"work_experience": jobs}],
                   ["currentEmployer", {"work_experience": jobs[:1]}],
                   ["currentEmployer", {"current_title": "Software Engineer"}]]) == ["Now Co", "", ""]


def test_residence_is_never_defaulted():
    assert run_js([["residenceCountry", {"preferred_country": "United States"}],
                   ["residenceCountry", {"residence_country": "Canada"}]]) == ["", "Canada"]
    assert "United States" not in "".join(
        line for line in CONTENT.splitlines()
        if "selectOption(" in line and "country" in line.lower())


@pytest.mark.parametrize("text,want", [
    ("Gender", True), ("What is your race / ethnicity?", True), ("Veteran status", True),
    ("Do you have a disability?", True), ("Sex", True), ("Hispanic or Latino?", True),
    ("Programming languages", False), ("Essex office location", False), ("Are you over 18?", False),
])
def test_protected_questions_are_recognised(text, want):
    assert run_js([["isDemographicQuestion", text]])[0] is want


def test_every_write_path_respects_the_demographic_opt_out():
    """Recall, learning and AI answers must each refuse a protected question —
    the direct EEO writer was the only path that checked (audit 2026-09-30)."""
    for fn in ("recallFromMemory", "observeField", "fillEssayQuestions"):
        assert "isDemographicQuestion(" in _function(CONTENT, fn), fn


# ── voluntary self-identification: the saved answer lands on the form ───────
# Owner's report (2026-10-10): the self-identification given in the profile was
# not filled. Two causes, both pinned here: a CONFIRMED "Decline to
# self-identify" counted as "no answer", and options were matched by substring,
# so "I decline to self-identify" / "I don't wish to answer" never matched and
# "Male" could land on "Female".

GH_GENDER = ["Please select", "Male", "Female", "Decline To Self Identify"]
GH_RACE = ["Please select", "American Indian or Alaskan Native", "Asian (Not Hispanic or Latino)",
           "Black or African American (Not Hispanic or Latino)", "Hispanic or Latino",
           "White (Not Hispanic or Latino)", "Native Hawaiian or Other Pacific Islander",
           "Two or More Races (Not Hispanic or Latino)", "Decline To Self Identify"]
GH_VETERAN = ["Please select", "I identify as one or more of the classifications of a protected veteran",
              "I am not a protected veteran", "I decline to self-identify"]
GH_DISABILITY = ["Please select", "Yes, I have a disability, or have had one in the past",
                 "No, I do not have a disability and have not had one in the past", "I do not want to answer"]


@pytest.mark.parametrize("kind,value,options,want", [
    ("gender", "Male", GH_GENDER, 1),
    ("gender", "Female", GH_GENDER, 2),
    ("gender", "Male", ["Female", "Male"], 1),                       # never a substring match
    ("gender", "Decline to self-identify", GH_GENDER, 3),
    ("gender", "Decline to self-identify", ["Select", "Man", "Woman", "I don't wish to answer"], 3),
    ("gender", "Decline to self-identify", ["Select", "Male", "Female", "Prefer not to say"], 3),
    ("gender", "Non-binary", ["Select", "Male", "Female", "Non-Binary", "Decline"], 3),
    ("ethnicity", "Asian", GH_RACE, 2),                                # not "Hispanic" via the qualifier
    ("ethnicity", "Hispanic or Latino", GH_RACE, 4),
    ("ethnicity", "White", GH_RACE, 5),
    ("ethnicity", "Black or African American", GH_RACE, 3),
    ("ethnicity", "Two or more races", GH_RACE, 7),
    ("ethnicity", "Decline to self-identify", GH_RACE, 8),
    ("veteran", "I am not a protected veteran", GH_VETERAN, 2),
    ("veteran", "I identify as one or more of the classifications of a protected veteran", GH_VETERAN, 1),
    ("veteran", "Decline to self-identify", GH_VETERAN, 3),
    ("veteran", "I am not a protected veteran", ["Select", "Yes", "No", "Decline"], 2),
    ("disability", "No, I do not have a disability, or history/record of having a disability", GH_DISABILITY, 2),
    ("disability", "Yes, I have a disability (or previously had a disability)", GH_DISABILITY, 1),
    ("disability", "Decline to self-identify", GH_DISABILITY, 3),
    ("hispanic", "No", ["Please select", "Yes", "No", "Decline To Self Identify"], 2),
    ("hispanic", "Yes", ["Please select", "Yes", "No", "Decline To Self Identify"], 1),
    ("hispanic", "Decline to self-identify", ["Please select", "Yes", "No", "Decline To Self Identify"], 3),
    # Unknown wording on either side: nothing is guessed.
    ("gender", "Agender", GH_GENDER, -1),
    ("ethnicity", "Asian", ["Please select", "Group A", "Group B"], -1),
])
def test_the_saved_answer_lands_on_the_matching_option(kind, value, options, want):
    assert run_js([["eeoPickOption", kind, value, options]])[0] == want


@pytest.mark.parametrize("ethnicity,want", [
    ("Asian", "No"), ("White (Not Hispanic or Latino)", "No"), ("Hispanic or Latino", "Yes"),
    ("Decline to self-identify", "Decline to self-identify"), ("", ""),
])
def test_greenhouses_hispanic_question_is_derived_from_the_ethnicity_answer(ethnicity, want):
    assert run_js([["eeoValueFor", {"ethnicity": ethnicity}, "hispanic"]])[0] == want


def test_a_confirmed_decline_is_a_stated_answer():
    # The pack the extension holds decides consent — eeo_confirmed, not the wording.
    confirmed = run_js([["(p => (_copilotPack = p, true))", {"eeo_confirmed": True}],
                        ["eeoStated", "Decline to self-identify"], ["eeoStated", "Male"], ["eeoStated", ""]])
    assert confirmed[1:] == [True, True, False]
    unconfirmed = run_js([["(p => (_copilotPack = p, true))", {"eeo_confirmed": False, "gender": "Male"}],
                          ["eeoStated", "Male"], ["eeoStated", "Decline to self-identify"]])
    assert unconfirmed[1:] == [False, False]
    assert run_js([["eeoStated", "Male"]])[0] is False      # no pack at all


def test_the_custom_dropdown_pass_respects_the_opt_out():
    body = _function(CONTENT, "fillEEOCustomDropdowns")
    assert "_eeoAutofillEnabled" in body and "eeoStated(" in body and "eeoKindOf(" in body
