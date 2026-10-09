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
    "isAntiBotField", "looksLikeFieldIdentifier", "looksLikeMetaReply",
    "looksLikeModelOnlyReply", "parseLocation", "pickLocationSuggestion",
]
CONSTS = ["_DEMOGRAPHIC_RE", "_GENERIC_YEARS_WORDS", "_COUNTRY_WORDS", "ATS_SUFFIXES",
          "_OTHER_DOC_RE", "_ANTI_BOT_RE", "_CAPTCHA_WIDGET_SEL", "_META_START_RE",
          "_META_ANY_RE", "_META_MODEL_ONLY_RE", "US_STATES", "CA_PROVINCES"]


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


PRELUDE = "\n".join([_const(CONTENT, c) for c in CONSTS] +
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
             ("", "resume.docx", "", True),
             # read as the server's _accept_allows reads them
             ("*/*", "resume.pdf", "application/pdf", True),
             ("*", "resume.pdf", "application/pdf", True)]
    out = run_js([[js, a, n, m] for a, n, m, _ in cases])
    assert out == [c[-1] for c in cases]
    from app.api.server import _accept_allows
    assert [_accept_allows(a, "." + n.rsplit(".", 1)[1], m) for a, n, m, _ in cases] == out


def test_the_resume_request_always_says_what_the_field_takes():
    """Review 2026-10-09: 1.0.1+ always sends ?accept= ("*" when the field
    names nothing), so a request without it is the 1.0.0 Store build."""
    body = _function(CONTENT, "attachResume")
    assert "|| '*'" in body
    assert "/resume?accept=${encodeURIComponent(_accept)}" in body
    assert "(_accept ?" not in body, "the accept parameter must never be optional again"


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


# ── live test 2026-10-09: fields nobody fills, replies nobody types ──────────

def _fake_el(attrs=None, label="", in_widget=False):
    """Just enough of an element for isAntiBotField."""
    return {"attrs": attrs or {}, "label": label, "in_widget": in_widget}


_EL_JS = """(spec) => isAntiBotField({
    getAttribute: (k) => (k in spec.attrs ? spec.attrs[k] : null),
    id: spec.attrs.id || '',
    closest: () => (spec.in_widget ? {} : null),
    labels: spec.label ? [{ textContent: spec.label }] : [],
})"""


@pytest.mark.parametrize("spec,want", [
    (_fake_el({"name": "g-recaptcha-response", "id": "g-recaptcha-response",
               "class": "g-recaptcha-response"}), True),          # the live-test field
    (_fake_el({"name": "h-captcha-response"}), True),
    (_fake_el({"name": "cf-turnstile-response"}), True),
    (_fake_el({"name": "website", "class": "honeypot-field"}), True),
    (_fake_el({"name": "comments_extra"}, label="Leave this field blank"), True),
    (_fake_el({"name": "comments_extra"}, label="\n  Leave this\n  field blank *\n"), True),
    (_fake_el({"name": "hp_email", "id": "hp2", "placeholder": "Please leave this field empty."}), True),
    (_fake_el({"name": "extra"}, label="If you are human, leave this field blank"), True),
    (_fake_el({"name": "token"}, in_widget=True), True),            # inside .g-recaptcha
    (_fake_el({"name": "why_acme", "id": "why"}, label="Why Acme?"), False),
    (_fake_el({"name": "_systemfield_name"}, label="Name"), False),
    # Review 2026-10-09: a REAL field that says when to leave it blank is the
    # applicant's to fill (the 1.0.0 build filled these; the looser pattern
    # stopped them).
    (_fake_el({"name": "linkedin"}, label="LinkedIn Profile (leave blank if none)"), False),
    (_fake_el({"name": "github"}, label="GitHub URL - leave empty if you don't have one"), False),
    (_fake_el({"name": "referrer", "placeholder": "Leave blank if you were not referred"}), False),
    (_fake_el({"name": "preferred"}, label="Preferred first name - leave empty if same as legal name"), False),
    (_fake_el({"name": "ref"}, label="If you have no referral code, leave this field blank."), False),
])
def test_anti_bot_fields_are_never_fill_targets(spec, want):
    assert run_js([[_EL_JS, spec]])[0] is want


def test_every_ai_and_memory_path_skips_anti_bot_and_hidden_fields():
    """The essay path asked the AI about a hidden captcha textarea; recall and
    learning must not touch one either, and fillInput refuses it outright."""
    assert "canFillField(ta)" in _function(CONTENT, "fillEssayQuestions")
    assert "looksLikeFieldIdentifier(q)" in _function(CONTENT, "fillEssayQuestions")
    assert "looksLikeMetaReply(answer)" in _function(CONTENT, "fillEssayQuestions")
    # A remembered answer may be the user's own words: only the model-only
    # forms drop it (review 2026-10-09).
    assert "looksLikeModelOnlyReply(answer)" in _function(CONTENT, "fillEssayQuestions")
    assert "canFillField(el)" in _function(CONTENT, "recallFromMemory")
    for fn in ("observeField", "observeAnswer", "fillInput"):
        assert "isAntiBotField(" in _function(CONTENT, fn), fn


@pytest.mark.parametrize("text,want", [
    ("g-recaptcha-response", True), ("question_68444493", True), ("_systemfield_name", True),
    ("why are you interested in this role?", False), ("comments", False), ("", False),
])
def test_a_field_name_is_not_a_question(text, want):
    assert run_js([["looksLikeFieldIdentifier", text]])[0] is want


_META = [
    "I'd be happy to help, but I notice the essay question appears incomplete or unclear. "
    "\"g-recaptcha-response\" looks like a technical parameter.",
    "It seems like the question is missing. Please provide the actual question.",
    "SKIP",
    "This doesn't look like an essay question; it is a form field name.",
]
_REAL = [
    "I notice patterns in messy data quickly, which is why integration work suits me.",
    "I can't wait to build connectors that security teams rely on every day.",
    "Over three years I built ETL pipelines and REST integrations in Python.",
    # review 2026-10-09: plausible answers the first patterns refused
    "I am unable to start before January 2027 because of my notice period.",
    "I'm not able to relocate, but I am happy to work remotely from Ohio.",
    "For me, choosing a team is a question of mission alignment.",
    "I tuned every technical parameter of our Kafka pipeline.",
    "As an AI engineer, I built retrieval systems for support teams.",
]
# Only a model writes these; text a user typed is refused for these alone.
_MODEL_ONLY = [_META[0], "As an AI language model, I cannot know that.",
               "The question appears to be incomplete.",
               "It seems like the question is missing. Please provide the actual question."]
# Meta for a model, but a person might type them: never refused as user text.
_USER_MAY_TYPE = ["SKIP", "This doesn't look like an essay question; it is a form field name.",
                  "I'm sorry, but I can't answer that without more context."]


def test_meta_replies_read_the_same_in_the_extension_and_the_server():
    """The extension's copy of the pattern must agree with app/autofill/field_guards."""
    from app.autofill.field_guards import looks_like_meta_reply
    out = run_js([["looksLikeMetaReply", t] for t in _META + _REAL])
    assert out == [True] * len(_META) + [False] * len(_REAL)
    assert [looks_like_meta_reply(t) for t in _META + _REAL] == out


def test_model_only_replies_read_the_same_in_the_extension_and_the_server():
    from app.autofill.field_guards import is_model_only_reply
    texts = _MODEL_ONLY + _USER_MAY_TYPE + _REAL
    out = run_js([["looksLikeModelOnlyReply", t] for t in texts])
    assert out == [True] * len(_MODEL_ONLY) + [False] * (len(_USER_MAY_TYPE) + len(_REAL))
    assert [is_model_only_reply(t) for t in texts] == out


def test_anti_bot_patterns_agree_with_the_server():
    from app.autofill.field_guards import _ANTI_BOT_RE as PY_RE
    js_body = _const(CONTENT, "_ANTI_BOT_RE").split("= /", 1)[1].rsplit("/i;", 1)[0]
    assert js_body == PY_RE.pattern, "keep content.js _ANTI_BOT_RE identical to field_guards.py"


def _opts(*texts):
    return [{"textContent": t} for t in texts]


@pytest.mark.parametrize("options,pack,want", [
    # the profile's state decides between two Cincinnatis
    (_opts("Cincinnati, Iowa, United States", "Cincinnati, Ohio, United States"),
     {"location": "Cincinnati, OH"}, "Cincinnati, Ohio, United States"),
    (_opts("Cincinnati, IA, USA", "Cincinnati, OH, USA"),
     {"location": "Cincinnati, OH"}, "Cincinnati, OH, USA"),
    # no suggestion for the city: nothing is picked
    (_opts("Columbus, Ohio, United States"), {"location": "Cincinnati, OH"}, None),
    # two Springfields and no state: the user picks
    (_opts("Springfield, Illinois", "Springfield, Missouri"), {"location": "Springfield"}, None),
    (_opts("Toronto, Ontario, Canada"), {"location": "Toronto"}, "Toronto, Ontario, Canada"),
    # a city name inside another word is not the city
    (_opts("Pittsburgh, Pennsylvania"), {"location": "Burgh, PA"}, None),
    # review 2026-10-09: the region is a whole comma component, never a
    # substring ("on" is inside "London" and "Toronto"; "Virginia" is inside
    # "West Virginia"), and a short code never matches lowercase text
    (_opts("London, England, United Kingdom", "London, Ontario, Canada", "London, Kentucky, United States"),
     {"location": "London, ON"}, "London, Ontario, Canada"),
    (_opts("London, England, United Kingdom", "London, Kentucky, United States"),
     {"location": "London, ON"}, None),
    (_opts("London, England, United Kingdom"), {"location": "London, ON"}, None),    # another country
    (_opts("London, England, United Kingdom", "London, ON, Canada"),
     {"location": "London, ON"}, "London, ON, Canada"),
    (_opts("Toronto, Ohio, United States", "Toronto, Ontario, Canada"),
     {"location": "Toronto, ON"}, "Toronto, Ontario, Canada"),
    (_opts("Toronto, Ohio, United States"), {"location": "Toronto, ON"}, None),      # another state
    (_opts("Bluefield, West Virginia, United States", "Bluefield, Virginia, United States"),
     {"location": "Bluefield, VA"}, "Bluefield, Virginia, United States"),
    (_opts("Bluefield, West Virginia, United States"), {"location": "Bluefield, VA"}, None),
    (_opts("Kansas City, Arkansas, United States", "Kansas City, Kansas, United States"),
     {"location": "Kansas City, Kansas"}, "Kansas City, Kansas, United States"),
    (_opts("Portland, Maine, United States", "Portland, Oregon, United States"),
     {"location": "Portland, OR"}, "Portland, Oregon, United States"),
    (_opts("Vancouver, Washington, United States", "Vancouver, British Columbia, Canada"),
     {"location": "Vancouver, BC"}, "Vancouver, British Columbia, Canada"),
    # "Columbus" is not "Columbus Grove"
    (_opts("Columbus Grove, Ohio, United States", "Columbus, Ohio, United States"),
     {"location": "Columbus, OH"}, "Columbus, Ohio, United States"),
    # a ZIP beside the code is still the code
    (_opts("Cincinnati, IA 52538", "Cincinnati, OH 45202"),
     {"location": "Cincinnati, OH"}, "Cincinnati, OH 45202"),
    # a country in the region slot is the country
    (_opts("London, England, United Kingdom", "London, Ontario, Canada"),
     {"location": "London, UK"}, "London, England, United Kingdom"),
    (_opts("London, Ontario, Canada"), {"location": "London, UK"}, None),
])
def test_location_suggestion_is_the_profiles_place(options, pack, want):
    js = "(o, p) => { const r = pickLocationSuggestion(o, p); return r ? r.textContent : null; }"
    assert run_js([[js, options, pack]])[0] == want
