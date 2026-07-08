"""The retrieval summary chunk must be built from the signed-in user's profile,
not the bundled single-user Q&A store (which pinned one candidate's roles,
stack, and city for every tenant)."""
from app.matching.matcher import _chunk_resume, _profile_summary_chunk

RESUME = """# JANE SMITH

## EXPERIENCE
**Software Engineer** | Barclays | Jan 2021 - Present | London, UK
- Built payment APIs serving 2M requests/day with Python and FastAPI.

## EDUCATION
Imperial College London, Bachelor of Science, 2018.
"""


class _Prof:
    target_roles = "Backend Engineer, Platform Engineer"
    current_title = "Software Engineer"
    key_skills = "Python, FastAPI, PostgreSQL, Kafka"
    location = "London"
    preferred_country = "United Kingdom"
    remote_ok = True
    professional_summary = "Backend engineer focused on payments infrastructure."


def test_summary_chunk_uses_user_profile():
    chunk = _profile_summary_chunk(_Prof())
    assert "Backend Engineer" in chunk
    assert "London" in chunk and "United Kingdom" in chunk
    assert "Kafka" in chunk
    # Nothing from the bundled owner persona.
    assert "Cincinnati" not in chunk
    assert "AI/ML Engineer, NLP Engineer" not in chunk


def test_chunk_resume_with_profile_has_no_owner_persona():
    chunks = _chunk_resume(RESUME, profile=_Prof())
    joined = "\n".join(chunks)
    assert "Cincinnati" not in joined
    assert "Backend Engineer, Platform Engineer" in joined


def test_chunk_resume_empty_profile_omits_summary():
    class _Empty:
        pass
    chunks_with = _chunk_resume(RESUME, profile=_Empty())
    # No usable profile fields → no fabricated summary chunk appended.
    assert all("Role Target" not in c for c in chunks_with)


def test_chunk_resume_legacy_fallback_still_works():
    # Local single-user mode (no profile) keeps the Q&A-store persona.
    chunks = _chunk_resume(RESUME)
    assert any("Role Target" in c for c in chunks)
