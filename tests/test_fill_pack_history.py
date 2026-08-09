"""The fill pack must carry the user's own work history.

/api/profile returned experience(2)/education(2) while /api/fill-pack
returned work_experience=[] and education=[]: the builder only consulted
résumé-text extraction and a thin current_title fallback, never the
structured profile columns the user edits in Settings. Downstream that made
the résumé generator 503 ("Could not generate resume") on every ATS, so the
required résumé field was never filled, and left "current company" with
nothing to write.
"""
import json

from app.autofill.answer_pack import _history_from_profile


class _Profile:
    def __init__(self, experience_json=None, education_json=None):
        self.experience_json = experience_json
        self.education_json = education_json


def test_profile_experience_reaches_the_pack():
    prof = _Profile(experience_json=json.dumps([
        {"company": "Globali20 India", "title": "ML Engineer",
         "start": "2022", "end": "Present", "location": "Remote"},
        {"company": "Acme", "title": "Data Analyst", "start": "2020", "end": "2022"},
    ]))
    hist = _history_from_profile(prof)
    assert len(hist["work_experience"]) == 2
    first = hist["work_experience"][0]
    assert first["company"] == "Globali20 India"
    assert first["title"] == "ML Engineer"
    # Pack shape uses start_date/end_date, not start/end.
    assert "start_date" in first and "end_date" in first


def test_profile_education_reaches_the_pack():
    prof = _Profile(education_json=json.dumps([
        {"university": "State U", "degree": "M.S.", "field": "Data Science",
         "start_year": 2018, "end_year": 2020},
    ]))
    hist = _history_from_profile(prof)
    assert len(hist["education"]) == 1
    assert hist["education"][0]["school"] == "State U"
    assert hist["education"][0]["degree"] == "M.S."


def test_empty_profile_yields_empty_lists_not_an_error():
    hist = _history_from_profile(_Profile())
    assert hist == {"work_experience": [], "education": []}


def test_malformed_json_is_survivable():
    hist = _history_from_profile(_Profile(experience_json="not json"))
    assert hist["work_experience"] == []
