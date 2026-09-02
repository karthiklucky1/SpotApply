"""What the résumé pipeline says to the user, and what it refuses to ship.

Each test here pins one defect a measured audit found in production output
(2 real résumés, 51 metered Haiku calls, 2026-09-02). They are grouped by who
gets hurt when they regress:

  * the Doctor's verdict — the most prominent feedback a user reads, which was
    accusing correct résumés of fraud
  * the anti-fingerprint gate — our own detector scoring output 24/100 and
    shipping it with a PASS
  * the phrase targeting — instructing the generator to paste job-description
    prose into someone's employment history
"""
from __future__ import annotations

import re
from datetime import date

import pytest

from app.config import settings
from app.tailoring.ats_keywords import extract_jd_phrases, is_skill_like, skill_phrases
from app.tailoring.doctor import ResumeDoctor
from app.tailoring.style import count_bold_spans, trim_bold


# ── the recruiter verdict ────────────────────────────────────────────────────

class _Capture:
    """Stands in for the shared Anthropic client and records the request."""

    def __init__(self):
        self.kwargs = None

        class _Messages:
            @staticmethod
            def create(**kw):
                outer.kwargs = kw
                resp = type("R", (), {})()
                resp.content = [type("C", (), {"text": "1. Yes. 2. Nothing."})()]
                return resp

        outer = self
        self.messages = _Messages()


@pytest.fixture
def verdict_request(monkeypatch):
    """Run _llm_verdict against a fake client and return the request it built."""
    cap = _Capture()
    monkeypatch.setattr("app.common.llm.shared_anthropic", lambda **kw: cap)

    def run(resume_md: str, jd: str = "Senior Backend Engineer. Python, Postgres."):
        ResumeDoctor()._llm_verdict(resume_md, jd)
        assert cap.kwargs is not None, "no request was built"
        return cap.kwargs

    return run


def test_the_verdict_sees_the_whole_resume(verdict_request):
    """The résumé used to be sliced at 3,000 characters while real tailored
    résumés run 3,900-4,300, so the simulated recruiter always saw a document
    ending mid-sentence — and reported "the resume is incomplete/cut off" as the
    single biggest rejection risk in three verdicts out of four. That was our
    truncation, described back to the user as their defect."""
    resume = "# Alex Tenant\n" + ("- Built and shipped a production service.\n" * 200)
    assert len(resume) > 3000
    prompt = verdict_request(resume)["messages"][0]["content"]
    tail = resume.strip().splitlines()[-1]
    assert tail in prompt, "the end of the résumé never reached the reviewer"
    assert resume in prompt


def test_the_verdict_knows_what_day_it_is(verdict_request):
    """Without today's date the model reads employment dates near its training
    cutoff as future-dated and calls them fabricated — "any real recruiter will
    flag this as dishonest" — about dates entirely in the past. Telling users
    their real résumé is fraudulent is the most damaging thing this can output."""
    prompt = verdict_request("# Alex Tenant\n- Shipped things.\n")["messages"][0]["content"]
    assert date.today().isoformat() in prompt
    low = prompt.lower()
    assert "past" in low and "future" in low, (
        "the prompt must say explicitly that past dates are normal"
    )


def test_the_verdict_has_room_to_finish_two_sentences(verdict_request):
    """max_tokens=120 truncated a two-sentence answer mid-word
    (stop_reason=max_tokens), so the user read half a thought."""
    kwargs = verdict_request("# Alex\n- Did work.\n")
    assert kwargs["max_tokens"] >= 200
    assert kwargs["max_tokens"] == settings.doctor_verdict_max_tokens


def test_the_verdict_is_not_sampled(verdict_request):
    """Two runs of the same résumé must not disagree about whether it is good."""
    kwargs = verdict_request("# Alex\n- Did work.\n")
    assert kwargs["extra_body"]["temperature"] == settings.verifier_temperature
    assert settings.verifier_temperature == 0.0


# ── how temperature reaches the wire ─────────────────────────────────────────

def test_temperature_travels_in_extra_body_not_as_a_named_argument():
    """The Python SDK dropped `temperature` from the typed Messages.create
    signature, so a named argument is a TypeError on every model — which is how
    a "pin the temperature" change turns into a silent fallback to the OTHER
    provider, or an outright failure, rather than a determinism fix."""
    import inspect

    from anthropic.resources.messages import Messages

    from app.common.llm import sampling
    assert "temperature" not in inspect.signature(Messages.create).parameters, (
        "the SDK types a temperature argument again — pass it directly and drop "
        "the extra_body indirection"
    )
    assert sampling("claude-haiku-4-5-20251001", 0.0) == {"extra_body": {"temperature": 0.0}}


def test_models_that_reject_sampling_are_sent_nothing():
    """Anthropic removed sampling parameters from Opus 4.7 onward and the Claude
    5 family; sending one is a 400. Omitting it loses determinism on those
    models — `output_config.effort` is the lever there — but a 400 on every
    scoring call is the worse failure, so the helper degrades rather than
    breaks."""
    from app.common.llm import sampling, supports_sampling
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8",
                  "claude-opus-4-7", "claude-fable-5-1"):
        assert supports_sampling(model) is False, model
        assert sampling(model, 0.0) == {}, model
    for model in ("claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"):
        assert supports_sampling(model) is True, model


def test_the_configured_models_actually_accept_the_pinned_temperature():
    """The settings and the helper have to agree, or the pin is decorative."""
    from app.common.llm import sampling
    for model in (settings.tailoring_model, settings.cover_letter_model,
                  settings.doctor_model, settings.scoring_model):
        assert sampling(model, 0.0), (
            f"{model} does not accept a temperature — tailoring determinism is "
            f"unpinned; switch that path to output_config.effort"
        )


# ── the anti-fingerprint gate ────────────────────────────────────────────────

_MASTER = """# Alex Tenant

## PROFESSIONAL EXPERIENCE
**Backend Engineer** | Acme Corp | May 2022 - Aug 2024 | Remote
- Built REST APIs with FastAPI serving 2,500 requests per minute.
"""

# Twelve bullets, all the same length, all opening on an action verb — the
# exact shape the audit measured at 24/100.
_UNIFORM = _MASTER + "\n".join(
    f"- Optimized the {n} service pipeline reliably and efficiently every day."
    for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta",
              "eta", "theta", "iota", "kappa", "lambda", "mu")
)


def _report(md, monkeypatch):
    monkeypatch.setattr(ResumeDoctor, "_llm_verdict", lambda self, a, b: None)
    return ResumeDoctor().check(md, _MASTER, "Backend engineer. Python, FastAPI, REST APIs.")


def test_a_machine_reading_resume_does_not_report_as_passing(monkeypatch):
    """The gate. A résumé our own detector scores far below the bar used to
    clear PASS_THRESHOLD anyway, because the fingerprint penalty was folded into
    the same number as ATS coverage and diluted by points it had nothing to do
    with. (_MASTER has too few bullets to form a baseline, so this exercises the
    absolute-bar fallback.)"""
    report = _report(_UNIFORM, monkeypatch)
    assert report.human_score < settings.doctor_min_human_score
    assert report.human_passed is False
    assert any("machine-written" in i for i in report.issues)


# ── the gate is a delta, not a level ─────────────────────────────────────────

_BURSTY_MASTER = """# Alex Tenant

## PROFESSIONAL EXPERIENCE
**Backend Engineer** | Acme Corp | May 2022 - Aug 2024 | Remote
- Owned the checkout service end to end.
- Cut p99 latency 45% by tuning PostgreSQL query plans, connection pooling and the three slowest endpoints in the checkout path.
- On-call rotation for the payments tier.
- Wrote the migration runbook the team still uses, then ran the migration itself over two weekends without a rollback.
- Mentored two juniors.
"""

_UNIFORM_MASTER = """# Alex Tenant

## PROFESSIONAL EXPERIENCE
**Backend Engineer** | Acme Corp | May 2022 - Aug 2024 | Remote
""" + "\n".join(
    f"- Optimized the {n} service pipeline reliably and efficiently every day."
    for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
)


def _check(tailored, master, monkeypatch):
    monkeypatch.setattr(ResumeDoctor, "_llm_verdict", lambda self, a, b: None)
    return ResumeDoctor().check(tailored, master, "Backend engineer. Python, FastAPI.")


def test_a_users_own_resume_never_fails_its_own_gate(monkeypatch):
    """THE calibration fact, and the reason this gate is relative.

    Run the fingerprint detector over the three hand-written master résumés in
    data/profiles — documents no model has touched — and they score 24, 44 and
    24 out of 100, tripping every flag. They are 92-100% verb-start (the résumé
    advice every guide gives) with length-CV 0.13-0.14 (a person fitting twelve
    bullets on one page). An absolute bar above those scores rejects the user's
    own writing; below them it catches nothing.
    """
    from pathlib import Path
    profiles = Path("data/profiles")
    if not profiles.exists():
        import pytest as _pytest
        _pytest.skip("profile fixtures not present")
    checked = 0
    for path in sorted(profiles.glob("*.md")):
        md = path.read_text()
        report = _check(md, md, monkeypatch)
        assert report.human_passed is True, (
            f"{path.name} is human-written and fails our own machine-written "
            f"gate (score {report.human_score}, flags {report.fingerprint_flags})"
        )
        checked += 1
    assert checked >= 1


def test_output_no_worse_than_the_master_passes(monkeypatch):
    """A candidate who writes uniform bullets and gets uniform bullets back has
    not been harmed, whatever the absolute score says."""
    report = _check(_UNIFORM_MASTER, _UNIFORM_MASTER, monkeypatch)
    assert report.human_passed is True
    assert report.human_score < settings.doctor_min_human_score, (
        "this fixture is supposed to score badly in absolute terms — that is "
        "the whole point of measuring the delta instead"
    )


def test_tailoring_a_bursty_resume_into_a_uniform_one_fails(monkeypatch):
    """The failure this gate exists for: the person wrote unevenly, and what
    came back is machine-flat."""
    slop = _BURSTY_MASTER.split("Remote\n")[0] + "Remote\n" + "\n".join(
        f"- Optimized the {n} service pipeline reliably and efficiently every day."
        for n in ("alpha", "beta", "gamma", "delta", "epsilon"))
    report = _check(slop, _BURSTY_MASTER, monkeypatch)
    assert report.human_passed is False
    assert any("more machine-written than your own" in i for i in report.issues)


def test_an_unparseable_master_falls_back_to_the_absolute_bar(monkeypatch):
    """No baseline is not evidence of quality — it must not pass by default."""
    report = _check(_UNIFORM, "just a sentence, no bullets at all", monkeypatch)
    assert report.human_passed is False


def test_the_human_gate_is_separate_from_the_quality_gate(monkeypatch):
    """Reading uniform is a style defect, not a false claim. It must not collapse
    into `passed`, because `passed` is what parks an application at ERROR — and
    leaving a user with nothing to submit over a bolding count is the wrong
    trade."""
    report = _report(_UNIFORM, monkeypatch)
    assert report.human_passed is False
    # Independent fields, so the caller can retry on one and block on the other.
    assert "human_passed" in report.__dataclass_fields__
    assert "passed" in report.__dataclass_fields__
    # Integrity is intact here — nothing was invented — so the failure really is
    # style alone, and the quality gate has no reason to be dragged down with it.
    assert report.integrity_issues == []


def test_a_varied_resume_clears_the_human_bar(monkeypatch):
    """The gate has to be passable, or every résumé carries a warning and the
    warning stops meaning anything."""
    varied = _MASTER + """
- Owned the checkout service end to end.
- Cut p99 latency 45% by tuning PostgreSQL query plans, connection pooling and the slowest three endpoints in the checkout path.
- On-call rotation for the payments tier.
- Wrote the migration runbook the team still uses, then ran the migration itself over two weekends without a rollback.
- Mentored two juniors.
"""
    report = _report(varied, monkeypatch)
    assert report.human_passed is True, report.fingerprint_flags


# ── bolding ──────────────────────────────────────────────────────────────────

def test_excess_bolding_is_removed_deterministically():
    """The prompt has asked for sparing bold since it was written and the model
    produced 51 spans anyway. An instruction the generator ignores is not a
    constraint; removing emphasis changes no words and no facts, so this one is
    simply deleted rather than negotiated."""
    md = "## EXPERIENCE\n- Used " + ", ".join(f"**Tool{i}**" for i in range(20)) + " in production.\n"
    assert count_bold_spans(md) == 20
    fixed, removed = trim_bold(md, max_per_section=3)
    assert count_bold_spans(fixed) == 3
    assert removed == 17
    for i in range(20):
        assert f"Tool{i}" in fixed, "unbolding must not delete the word itself"


def test_structural_bold_is_never_stripped():
    """Employer headers and skills labels are parsed by evidence.py and
    doctor.py off their exact bold shape. Unbolding them would make a résumé's
    own employment history unreadable to the integrity checks — a style fix that
    breaks the safety layer is not a fix."""
    md = """## PROFESSIONAL EXPERIENCE
**Backend Engineer** | Acme Corp | May 2022 - Aug 2024 | Remote
- Built **FastAPI** services with **Docker**, **Kubernetes**, **Redis** and **Kafka**.

## TECHNICAL SKILLS
- **Languages**: Python, SQL
- **Infrastructure**: Docker, Kubernetes
- **Data**: PostgreSQL, Redis
- **Streaming**: Kafka
"""
    fixed, _ = trim_bold(md, max_per_section=3)
    assert "**Backend Engineer** | Acme Corp" in fixed
    for label in ("**Languages**", "**Infrastructure**", "**Data**", "**Streaming**"):
        assert label in fixed, f"{label} is a list label, not decoration"


def test_a_resume_already_within_the_cap_is_untouched():
    md = "## EXPERIENCE\n- Built **FastAPI** services on **Docker**.\n"
    fixed, removed = trim_bold(md)
    assert fixed == md and removed == 0


def test_the_cap_is_per_section_not_per_document():
    md = ("## A\n- Used **one**, **two**, **three**, **four**.\n"
          "## B\n- Used **five**, **six**, **seven**, **eight**.\n")
    fixed, removed = trim_bold(md, max_per_section=3)
    assert removed == 2
    assert count_bold_spans(fixed) == 6


# ── phrase targeting ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("fragment", [
    "design and own",          # a responsibility, not a skill
    "own ml systems",          # verb-led clause
    "ai platform company",     # marketing copy
    "backbone for enterprise", # marketing copy
    "learning engineer ai",    # job-title fragment
    "engineer ai platform",    # job-title fragment
])
def test_marketing_and_title_fragments_are_not_targeted(fragment):
    """These are genuinely the phrases the JD repeats, so they are fine as
    coverage signals — but the tailor prompt says "incorporate the EXACT
    phrasing", and a model told to work "design and own" into a résumé does
    exactly that. Lifting a JD responsibility line into someone's employment
    history is the fabrication grounding then pays to catch."""
    assert is_skill_like(fragment) is False


@pytest.mark.parametrize("skill", [
    "python", "fastapi", "kubernetes", "vector search", "machine learning",
    "postgresql", "ci/cd", "distributed systems", "model inference",
])
def test_real_skills_are_still_targeted(skill):
    """Over-filtering costs findability, which is the whole point of the list."""
    assert is_skill_like(skill) is True


def test_the_filter_narrows_a_real_ranked_list_without_emptying_it():
    jd = """Senior Machine Learning Engineer — AI Platform
    We're building the inference backbone for enterprise LLM deployments.
    You'll design and own ML systems from research prototype to production.
    Responsibilities: deploy large-scale ML pipelines using Python and PyTorch;
    optimize model inference latency; build RAG pipelines and vector search.
    Requirements: strong Python, PyTorch, Kubernetes, MLOps, FastAPI.
    """
    phrases = extract_jd_phrases(jd)
    kept = skill_phrases(phrases)
    assert kept, "the filter must not empty the list"
    assert len(kept) < len(phrases), "nothing was filtered from a JD full of prose"
    assert "python" in kept and "pytorch" in kept
    assert not any(re.search(r"\b(?:and|for|the|own)\b", p) for p in kept)
