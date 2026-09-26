"""A résumé every time, and nothing invented (2026-09-26).

The export gate withheld a whole draft because it mentioned C++, Rust and
"large language models". The last was a false alarm (the résumé says "LLM");
the first two were real inventions — so they come OUT of the draft, the rest
of the résumé is delivered, and they are shown as skills to learn or to
confirm. Adding them to the résumé is never an option: that is the one thing
this product exists not to do.
"""
from datetime import date
from pathlib import Path

from app.tailoring.doctor import drop_false_future_claims
from app.tailoring.export_gate import evaluate, reset_state, strip_unconfirmed
from app.tailoring.requirements import _present_in, review

ROOT = Path(__file__).resolve().parents[1]

MASTER = """# Candidate
## Experience
### Software Engineer | Walmart | Jan 2023 - Mar 2026
- Built LLM-powered agents with Python on AWS.
## Skills
Python, SQL, AWS"""

DRAFT = """# Candidate
## Summary
Backend engineer building Python and Rust services with large language models.
## Skills
- **Languages:** Python, C++, Rust, SQL
## Experience
### Software Engineer | Walmart | Jan 2023 - Mar 2026
- Built Python and C++ microservices on AWS.
- Wrote a Rust ingestion engine.
- Built LLM-powered agents with Python on AWS."""

JD = ("Requirements: experience with C++ and Rust is required. "
      "Experience with large language models. Python required.")


def test_plural_expansion_of_an_acronym_is_present():
    assert _present_in("large language models", MASTER)


def test_strip_removes_only_the_claims():
    new, removed = strip_unconfirmed(DRAFT, ["c++", "rust"])
    assert set(removed) == {"c++", "rust"}
    for gone in ("C++", "Rust"):
        assert gone not in new
    assert "- **Languages:** Python, SQL" in new
    assert "- Built Python microservices on AWS." in new
    assert "ingestion engine" not in new            # the bullet was only about Rust
    assert "large language models" in new           # supported, kept
    assert "Built LLM-powered agents" in new


def test_strip_never_adds_anything():
    new, _ = strip_unconfirmed(DRAFT, ["c++", "rust"])
    assert set(new.split()) <= set(DRAFT.split())


def test_a_stripped_draft_passes_the_export_gate():
    reset_state()
    before = evaluate(grounding_rejected=False, grounding_reason="",
                      master=MASTER, tailored=DRAFT, jd=JD)
    assert before.blocked
    rv = review(MASTER, DRAFT, JD)
    new, _ = strip_unconfirmed(DRAFT, list(rv.unconfirmed_claims))
    after = evaluate(grounding_rejected=False, grounding_reason="",
                     master=MASTER, tailored=new, jd=JD)
    assert not after.blocked, after.reason


def test_missing_skills_are_reported_to_learn():
    rv = review(MASTER, DRAFT, JD)
    assert {"c++", "rust"} <= {s.lower() for s in rv.missing_skills}
    assert "python" not in {s.lower() for s in rv.missing_skills}


def test_a_user_confirmed_skill_counts_as_evidence():
    master = MASTER + "\n\n## Additional Verified Achievements (candidate-confirmed)\n- Skill confirmed by user: Rust"
    assert _present_in("rust", master)


def test_tailor_strips_before_writing_the_document():
    src = (ROOT / "app" / "tailoring" / "tailor.py").read_text()
    i = src.index("strip_unconfirmed(resume_md")
    assert i < src.index("_md_to_docx(resume_md, resume_path)")
    assert '"removed_claims": removed_claims' in src and '"skills_to_learn": skills_to_learn' in src


def test_dangling_item_number_is_removed_from_the_verdict():
    v = "1. Yes — the resume hits every signal: Python backend at scale. 2."
    assert drop_false_future_claims(v, date(2026, 9, 26)) == \
        "1. Yes — the resume hits every signal: Python backend at scale."


DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()
SERVER = (ROOT / "app" / "api" / "server.py").read_text()


def test_studio_shows_the_document_and_offers_download():
    assert '"resume_md": resume_md,' in SERVER
    assert "function _tsDocHtml(md, keywords)" in DASH
    assert 'id="ts-download"' in DASH and "/download-resume" in DASH


def test_studio_offers_confirm_not_insert():
    assert "window.tsConfirmSkill" in DASH and "/api/resume/xray/approve-skill" in DASH
    assert "a resume can only claim what you have done" in DASH


def test_a_trailing_skill_phrase_is_trimmed_not_the_whole_sentence():
    new, _ = strip_unconfirmed(
        "## Summary\nBackend engineer building Python services on AWS, with Rust experience.", ["rust"])
    assert "Backend engineer building Python services on AWS." in new


def test_old_reports_are_cleaned_when_read():
    assert "quality[\"verdict\"] = drop_false_future_claims(quality[\"verdict\"])" in SERVER


def test_user_facing_text_says_resume_not_the_accented_form():
    """Founder's rule (2026-09-26): "resume", never "résumé", in what users see."""
    for rel in ("app/templates/dashboard.html", "extension/popup.js"):
        text = (ROOT / rel).read_text()
        assert "résumé" not in text and "Résumé" not in text, rel
    from app.tailoring.export_gate import evaluate as _ev
    reset_state()
    v = _ev(grounding_rejected=True, grounding_reason="", master="", tailored="x", jd="")
    assert "résumé" not in v.reason and "resume" in v.reason


def test_rebuild_uses_an_in_app_dialog_not_a_browser_prompt():
    assert "window.prompt(" not in DASH.split("async function tailorJob")[1].split("function ")[0]
    assert "function _askRebuildDirection()" in DASH and "Rebuild this resume" in DASH
    assert "if (answer === null) return;" in DASH          # Cancel no longer rebuilds


def test_career_office_word_choice_is_in_the_prompt_and_checker():
    from app.tailoring.doctor import ACTION_VERBS, BANNED_WORDS
    from app.tailoring.tailor import TAILOR_SYSTEM
    assert "WORD CHOICE" in TAILOR_SYSTEM and "Rule 2 still wins" in TAILOR_SYSTEM
    assert "responsible for" in BANNED_WORDS
    assert {"diagnosed", "consolidated", "mentored"} <= ACTION_VERBS
