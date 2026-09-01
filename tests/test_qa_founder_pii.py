"""No code path may answer a form with a hardcoded person or a guessed fact.

Two related classes of bug, both shipped at one point:

* ``resolver.py`` carried the founder's real PII (name, email, phone, city,
  school, employer) as the literal DEFAULT on ~20 branches, so any deployment
  or store missing a key answered identity questions AS the founder — the
  same leak class as the founder-résumé leak in docs/ARCHITECTURE.md §7.1.
  Other branches guessed facts outright (work-authorized: True, US-based:
  "Yes", veteran/disability status asserted). The rule now: a stored value or
  ``(None, 0.0)`` — the human answers what the store doesn't hold. Only
  deliberately generic, fact-free answers ("Negotiable", "LinkedIn") keep
  defaults.

* The Lever radio filler hardcoded "Yes" for sponsorship and work-auth
  phrasings BEFORE consulting the resolver, and defaulted every unmatched
  yes/no radio to "Yes" — bypassing the resolver's documented rule that
  sponsorship questions are NEVER auto-answered (the one genuine auto-reject
  gate; a false answer is wilful misrepresentation under INA 212(a)(6)(C)(i)).
"""
from __future__ import annotations

from pathlib import Path

from app.qa_store.resolver import QAResolver

_PREFIX = "qapii-"  # (no DB rows are created by these tests)

_REPO = Path(__file__).resolve().parent.parent

# The founder literals that used to live in resolver.py / agent.py as code
# defaults. Their presence in answers.yaml (the founder's own store) is fine;
# their presence in CODE is the bug.
_FOUNDER_LITERALS = (
    "Karthik", "Amruthaluri", "karthikamruthaluri", "(513) 276-3950",
    "karthiklucky1", "amruthaluri", "Cincinnati", "Home Depot",
    "University of Cincinnati", "April 30, 2026",
)


def _empty_resolver(tmp_path) -> QAResolver:
    return QAResolver(yaml_path=tmp_path / "does-not-exist.yaml")


# ── resolver: empty store answers nothing factual ────────────────────────────

def test_empty_store_never_answers_identity_or_facts(tmp_path):
    r = _empty_resolver(tmp_path)
    questions = [
        "LinkedIn Profile", "GitHub URL", "Website or portfolio",
        "Please enter your full name:", "First name", "Last name",
        "Email address", "Phone number",
        "Are you legally authorized to work in the United States?",
        "Do you have an active security clearance?",
        "Are you willing to relocate?",
        "Where in the United States will you be working from?",
        "Are you based in the United States?",
        "Do you agree to our AI policy?",
        "What state do you currently reside in?",
        "How many years of experience do you have?",
        "What university did you attend?", "Date of Graduation",
        "Graduation year", "Please state your highest degree:",
        "What was your major?",
        "Gender", "Are you Hispanic or Latino?", "Race/Ethnicity",
        "Veteran Status", "Disability Status",
        "Have you ever been convicted of a felony?",
        "Are you subject to a non-compete agreement?",
        "Have you previously worked for us?",
        "Are you under the age of 18?",
        "Have you ever been terminated for cause?",
    ]
    for q in questions:
        ans, conf = r.resolve(q)
        assert ans is None and conf == 0.0, (
            f"{q!r} answered {ans!r} from an EMPTY store — a code default is "
            "impersonating or guessing about the applicant")


def test_empty_store_keeps_only_factfree_generics(tmp_path):
    """'Negotiable' and 'LinkedIn' assert nothing about the person — the two
    deliberate exceptions, pinned so a future edit doesn't silently widen
    them."""
    r = _empty_resolver(tmp_path)
    assert r.resolve("What are your salary expectations?") == ("Negotiable", 0.95)
    assert r.resolve("How did you hear about us?") == ("LinkedIn", 0.95)


def test_stored_values_still_answer(tmp_path):
    """The store (each deployment's own answers.yaml) keeps working — the fix
    removed code defaults, not store-backed answers."""
    p = tmp_path / "a.yaml"
    p.write_text(
        "identity:\n  first_name: Jane\n  email: jane@example.com\n"
        "work_authorization:\n  authorized_to_work_us: false\n"
        "general:\n  us_based: false\n")
    r = QAResolver(yaml_path=p)
    assert r.resolve("Email address") == ("jane@example.com", 0.95)
    assert r.resolve("Are you legally authorized to work in the US?") == ("No", 0.95)
    assert r.resolve("Are you based in the United States?") == ("No", 0.95)


def test_resolver_source_carries_no_founder_literals():
    src = (_REPO / "app" / "qa_store" / "resolver.py").read_text()
    for lit in _FOUNDER_LITERALS:
        assert lit not in src, f"founder literal {lit!r} is back in resolver.py"
    assert "Ohio" not in src.replace('"OH": "Ohio"', "")  # US_STATES table is fine


def test_agent_source_carries_no_founder_literals():
    src = (_REPO / "app" / "autofill" / "agent.py").read_text()
    for lit in _FOUNDER_LITERALS:
        assert lit not in src, f"founder literal {lit!r} is back in agent.py"


# ── Lever radios: sponsorship is never auto-answered, nothing is guessed ─────

def test_lever_radio_never_auto_answers_sponsorship(monkeypatch):
    from app.autofill import agent
    for phrasing in (
        "Will you now or in the future require visa sponsorship?",
        "Do you require sponsorship to continue working in the United States?",
        "Will you require sponsorship for employment now or in the future? Yes No",
        # The compound trap: matches work-auth phrasing too — the sponsorship
        # guard must win, not the eligibility rule.
        "Are you legally authorized to work in the US without sponsorship? Yes No",
        "Are you currently eligible to work in this country without sponsorship?",
    ):
        assert agent._lever_radio_answer(phrasing, None) is None, (
            f"sponsorship radio auto-answered: {phrasing!r}")


def test_lever_radio_answers_eligibility_from_store_not_hardcode(monkeypatch, tmp_path):
    from app.autofill import agent
    q = "Are you currently eligible to work in the United States?"

    yes = tmp_path / "yes.yaml"
    yes.write_text("work_authorization:\n  authorized_to_work_us: true\n")
    monkeypatch.setattr(agent, "qa_resolver", QAResolver(yaml_path=yes))
    assert agent._lever_radio_answer(q, None) == "Yes"

    no = tmp_path / "no.yaml"
    no.write_text("work_authorization:\n  authorized_to_work_us: false\n")
    monkeypatch.setattr(agent, "qa_resolver", QAResolver(yaml_path=no))
    assert agent._lever_radio_answer(q, None) == "No", (
        "eligibility must come from the stored fact — the old map said Yes "
        "for everyone")

    monkeypatch.setattr(agent, "qa_resolver", _empty_resolver(tmp_path))
    assert agent._lever_radio_answer(q, None) is None


def test_lever_radio_unknown_yesno_is_not_defaulted_yes(monkeypatch, tmp_path):
    """The blanket 'any yes/no → Yes' fallback is gone: an unclassified radio
    (which could be 'have you been convicted…' phrased unusually) is left for
    the human, who reviews before Submit anyway."""
    from app.autofill import agent
    from app.db.init_db import init_db
    init_db()
    monkeypatch.setattr(agent, "qa_resolver", _empty_resolver(tmp_path))
    assert agent._lever_radio_answer(
        "Do you accept the pineapple-on-pizza doctrine? Yes No", None) is None


# ── EEO fallbacks: neutral declines only, after the stores ───────────────────

def test_eeo_fallbacks_are_declines_not_assertions():
    from app.autofill import agent
    assert not hasattr(agent, "_EEOC_DEFAULTS"), "the pre-store defaults dict is back"
    for key, val in agent._EEO_SAFE_DECLINES.items():
        low = val.lower()
        assert "decline" in low or "wish to answer" in low or "want to answer" in low, (
            f"EEO fallback for {key!r} asserts a fact: {val!r}")
        assert "ohio" not in low and "linkedin" not in low


def test_check_memory_falls_back_to_declines_only_when_stores_empty(monkeypatch, tmp_path):
    from app.autofill import agent
    from app.db.init_db import init_db
    init_db()
    monkeypatch.setattr(agent, "qa_resolver", _empty_resolver(tmp_path))
    assert agent._check_memory("Gender") == "Decline to self-identify"
    assert agent._check_memory("Veteran Status") == "I don't wish to answer"
    # State questions no longer have a hardcoded answer at all
    assert agent._check_memory("What state do you currently reside in?") is None

    # A stored answer beats the decline layer
    stored = tmp_path / "eeo.yaml"
    stored.write_text('eeo:\n  gender: "Nonbinary"\n')
    monkeypatch.setattr(agent, "qa_resolver", QAResolver(yaml_path=stored))
    assert agent._check_memory("Gender") == "Nonbinary"


# ── the LLM question-answerer prompt carries no fallback identity ────────────

def test_question_prompt_from_empty_store_names_nobody(monkeypatch, tmp_path):
    from app.autofill import agent
    monkeypatch.setattr(agent, "qa_resolver", _empty_resolver(tmp_path))
    agent._autofill_identity.set(None)
    prompt = agent._get_system_question_answerer_prompt()
    for lit in _FOUNDER_LITERALS:
        assert lit not in prompt, (
            f"empty-store prompt still introduces the candidate as {lit!r}")


# ── the founder store answers FOUNDER fills only ─────────────────────────────
# answers.yaml is by design one person's store; the process-global qa_resolver
# must never answer for a different fill owner. Review of the first fix found
# four paths where it still did (identity/EEO/state via _check_memory, the
# Greenhouse eeo: block, the Lever eligibility radio, the Lever org field).

from types import SimpleNamespace


def _tenant_scope(monkeypatch, profile):
    """Enter a non-founder fill scope with the given owner profile."""
    from app.autofill import agent
    import app.autofill.answer_pack as ap
    tok_i = agent._autofill_identity.set(
        {"first_name": "Dana", "last_name": "Ruiz", "email": "dana@example.com"})
    tok_o = agent._autofill_owner.set("tenant-x")
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: profile)
    return tok_i, tok_o


def _reset_scope(tok_i, tok_o):
    from app.autofill import agent
    agent._autofill_identity.reset(tok_i)
    agent._autofill_owner.reset(tok_o)


def test_check_memory_never_answers_a_tenant_from_the_founder_store(monkeypatch):
    from app.autofill import agent
    from app.db.init_db import init_db
    init_db()
    profile = SimpleNamespace(first_name="Dana", last_name="Ruiz",
                              email="dana@example.com", phone="", linkedin_url="",
                              github_url="", portfolio_url="", current_title="",
                              location="", university="Rutgers University",
                              graduation_year=2020, years_experience=4)
    toks = _tenant_scope(monkeypatch, profile)
    try:
        # The founder's yaml IS present (repo file) — it must still not answer.
        assert agent._check_memory("Email") == "dana@example.com"
        assert agent._check_memory("What university did you attend?") == "Rutgers University"
        assert agent._check_memory("Gender") == "Decline to self-identify"
        assert agent._check_memory("Race/Ethnicity") == "Decline to self-identify"
        state = agent._check_memory("What state do you currently reside in?")
        assert state != "Ohio" and state is None
        for q in ("LinkedIn Profile", "Phone number", "Current employer"):
            ans = agent._check_memory(q)
            assert not ans or "amruthaluri" not in str(ans).lower(), (
                f"{q!r} answered a tenant's form from the founder store: {ans!r}")
            assert ans != "Open to work. Previously at Home Depot."
    finally:
        _reset_scope(*toks)


def test_greenhouse_eeo_answers_are_declines_for_tenant_fills(monkeypatch):
    from app.autofill import agent
    # Founder path: their stored answers
    agent._autofill_identity.set(None)
    founder = agent._eeo_field_answers()
    assert founder["gender"] == "Male"  # from the founder's own store — theirs to give
    # Tenant path: declines unconditionally, never the founder's demographics
    toks = _tenant_scope(monkeypatch, SimpleNamespace())
    try:
        tenant = agent._eeo_field_answers()
        assert tenant["gender"] == "Decline to self-identify"
        assert tenant["race"] == "Decline to self-identify"
        assert "wish to answer" in tenant["veteran_status"].lower()
        assert "wish to answer" in tenant["disability_status"].lower()
        assert "Male" not in tenant.values() and "Asian" not in tenant.values()
    finally:
        _reset_scope(*toks)


def test_lever_radio_compound_sponsorship_phrasings_route_to_human(monkeypatch):
    """Phrasings that dodge the literal 'sponsor' token but still ask the
    knockout question — found leaking 'Yes' in review."""
    from app.autofill import agent
    for phrasing in (
        "Are you currently eligible to work in this country without requiring a visa in the future?",
        "Are you legally eligible to work in the country this role is based in? Will you need visa support now or in the future?",
        "Are you eligible to work in this country? Do you need an employer to file an immigration petition for you?",
        "Do you now or in the future require work authorization support?",
    ):
        assert agent._lever_radio_answer(phrasing, None) is None, (
            f"compound sponsorship phrasing auto-answered: {phrasing!r}")


def test_lever_eligibility_is_owner_scoped(monkeypatch):
    from app.autofill import agent
    q = "Are you currently eligible to work in the United States?"
    # Tenant with a stored status → answered from THEIR status
    toks = _tenant_scope(monkeypatch, SimpleNamespace(
        work_authorization="US Citizen", visa_status="", requires_sponsorship=False))
    try:
        assert agent._lever_radio_answer(q, None) == "Yes"
    finally:
        _reset_scope(*toks)
    # Tenant with NO stored status → human (never the founder's, never a
    # default: assess_profile's empty-profile framing defaults authorized)
    toks = _tenant_scope(monkeypatch, SimpleNamespace(
        work_authorization="", visa_status=""))
    try:
        assert agent._lever_radio_answer(q, None) is None
    finally:
        _reset_scope(*toks)


def test_lever_org_is_owner_scoped(monkeypatch):
    from app.autofill import agent
    toks = _tenant_scope(monkeypatch, SimpleNamespace(
        experience_json=[{"company": "Acme Robotics"}], university="Rutgers University"))
    try:
        assert agent._lever_org_value() == "Acme Robotics"
    finally:
        _reset_scope(*toks)
    toks = _tenant_scope(monkeypatch, SimpleNamespace(experience_json=[], university=""))
    try:
        assert agent._lever_org_value() == "", (
            "an empty tenant profile must leave org blank, not borrow the "
            "founder store's school")
    finally:
        _reset_scope(*toks)
