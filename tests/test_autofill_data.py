"""Auto-fill data (owner, 2026-11-14): the answers application forms ask for
that no resume holds, saved once in the profile.

ONE catalog (app/autofill/autofill_fields.py) draws the editor, decides what is
stored and decides which saved answer a form question asks for. These tests pin
the three things that would hurt a user if they drifted:

* a saved answer is never offered for a question a saved default must not
  answer (work authorization, self-identification, how-did-you-hear, a captcha
  field), and never for a different question that shares a word with it;
* saving three answers never wipes the others;
* nothing the product refuses to hold (a password, a signature, a consent)
  ever becomes a field.

The shared dev profile's object is restored as found.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.autofill import autofill_fields as af

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app
    return TestClient(app)


@pytest.fixture()
def restore(client):
    """Put the shared dev profile's auto-fill object back exactly as it was."""
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import UserProfile
    client.get("/api/profile")                       # make sure the row exists
    with get_session() as s:
        before = s.exec(select(UserProfile.autofill_json)).first()
    yield
    with get_session() as s:
        row = s.exec(select(UserProfile)).first()
        row.autofill_json = before
        s.add(row)
        s.commit()


SAVED = {
    "middle_name": "Kumar", "address_line1": "123 Main St", "city": "Cincinnati",
    "state": "Ohio", "postal_code": "45220", "country": "United States",
    "over_18": "Yes", "felony_conviction": "No", "non_compete": "No",
    "highest_education": "Master's degree", "willing_to_travel": "Yes",
    "travel_percentage": "Up to 25%", "security_clearance": "None",
    "citizenship_country": "India", "time_zone": "Eastern Time (ET)",
    "reason_for_leaving": "Contract ended",
    "languages": [{"language": "English", "proficiency": "Fluent"},
                  {"language": "Telugu", "proficiency": "Native or bilingual"}],
    "certifications": [{"name": "AWS Solutions Architect", "issuer": "AWS", "year": "2024"}],
}


# ── the catalog ──────────────────────────────────────────────────────────────

def test_every_key_is_unique_and_every_column_field_is_a_real_column():
    from app.db.models import UserProfile
    keys = [f.key for s in af.SECTIONS for f in s.fields]
    assert len(keys) == len(set(keys)), "a key appears in two sections"
    for f in af.COLUMN_FIELDS.values():
        assert f.column in UserProfile.model_fields, f"{f.key}: no column {f.column}"
    # A JSON key must never shadow a column: the column would be the one read.
    for key in af.JSON_FIELDS:
        assert key not in UserProfile.model_fields, key


def test_every_column_field_round_trips_through_the_profile_routes(client):
    """The editor draws these inputs from the catalog, so the static-form
    round-trip guard (test_profile_form_roundtrip) cannot see them."""
    from app.api.server import ProfileUpdate
    data = client.get("/api/profile").json()
    for f in af.COLUMN_FIELDS.values():
        assert f.column in data, f"GET /api/profile omits {f.column}: a save would wipe it"
        assert f.column in ProfileUpdate.model_fields, f"the update drops {f.column}"


@pytest.mark.parametrize("word", ["password", "passcode", "signature", "arbitration",
                                  "non-disclosure", "certify", "consent to", "ssn",
                                  "social security", "sexual", "religio", "transgender",
                                  "date of birth"])
def test_what_the_product_refuses_to_hold_is_not_a_field(word):
    """A password is never stored; an agreement or a signature is the user's
    act on one employer's form; further self-identification has no fill path."""
    for s in af.SECTIONS:
        for f in s.fields:
            # "Consent to a drug test?" is a screening answer, not an agreement.
            if f.key == "drug_test":
                continue
            assert word not in f.label.lower() and word not in f.key.replace("_", " "), (word, f.key)
    for l in af.LISTS.values():
        assert l.key != "references"


def test_the_editor_description_carries_no_patterns_and_no_user_data():
    ui = af.catalog_for_ui()
    blob = json.dumps(ui)
    assert "ask" not in {k for s in ui["sections"] for f in s["fields"] for k in f}
    assert "\\b" not in blob
    assert [s["key"] for s in ui["sections"]][:4] == ["personal", "contact", "address", "online"]
    assert set(ui["profile_lists"]) == {"certifications", "projects"}


def test_the_column_is_declared_everywhere_a_column_is_listed():
    from app.api.server import _USERPROFILE_COLUMNS
    from app.db.models import UserProfile
    assert "autofill_json" in UserProfile.model_fields
    assert "autofill_json" in [c for c, _s, _p in _USERPROFILE_COLUMNS]
    assert '("autofill_json", "TEXT")' in (ROOT / "app" / "db" / "init_db.py").read_text()


# ── what may be stored ───────────────────────────────────────────────────────

def test_clean_keeps_known_values_and_drops_the_rest():
    got = af.clean({
        "middle_name": "  Kumar \n ", "over_18": "yes", "sms_ok": "maybe",
        "highest_education": "master's degree", "time_zone": "Mars Time",
        "hours_per_week": "40", "postal_code": "45220", "earliest_start_date": "2027-01-04",
        "first_name": "X",                      # a column: stored there, not here
        "password": "hunter2", "unknown": 1,
        "twitter_url": "x.com/me", "proud_project": "line one\r\nline two  ",
    })
    assert got == {
        "middle_name": "Kumar", "over_18": "Yes", "highest_education": "Master's degree",
        "hours_per_week": "40", "postal_code": "45220", "earliest_start_date": "2027-01-04",
        "twitter_url": "x.com/me", "proud_project": "line one\nline two",
    }


@pytest.mark.parametrize("key,bad", [
    ("earliest_start_date", "next month"), ("earliest_start_date", "1802-01-01"),
    ("hours_per_week", "forty"), ("twitter_url", "not a url"), ("over_18", ["Yes"]),
    ("middle_name", {"a": 1}),
])
def test_a_value_a_field_cannot_hold_is_refused_not_stored(key, bad):
    assert af.clean({key: bad}) == {}


def test_text_is_bounded():
    assert len(af.clean({"middle_name": "x" * 500})["middle_name"]) == 60
    assert len(af.clean({"proud_project": "y" * 5000})["proud_project"]) == 1200


def test_lists_keep_complete_entries_only_and_are_capped():
    got = af.clean({"languages": [
        {"language": "English", "proficiency": "Fluent", "reading": "fluent", "x": 1},
        {"proficiency": "Basic"},                      # no language: not an entry
        {"language": "Hindi"},                         # no proficiency: not an entry
        "nonsense",
    ] + [{"language": f"L{i}", "proficiency": "Basic"} for i in range(20)]})
    assert got["languages"][0] == {"language": "English", "proficiency": "Fluent", "reading": "Fluent"}
    assert len(got["languages"]) == af.LISTS["languages"].max_items
    assert af.clean({"languages": "English"}) == {}


def test_merge_keeps_what_the_update_did_not_send_and_clears_on_empty():
    stored = af.dumps({"middle_name": "Kumar", "city": "Cincinnati",
                       "languages": [{"language": "English", "proficiency": "Fluent"}]})
    got = af.merge(stored, {"city": "Columbus", "state": "Ohio"})
    assert got["middle_name"] == "Kumar" and got["city"] == "Columbus" and got["state"] == "Ohio"
    assert got["languages"], "a list the update did not send is kept"
    got = af.merge(af.dumps(got), {"middle_name": "", "languages": []})
    assert "middle_name" not in got and "languages" not in got
    assert af.merge("not json", {"city": "X"}) == {"city": "X"}
    assert af.dumps({}) is None, "nothing saved is NULL, not '{}'"


def test_load_never_hands_out_what_todays_catalog_would_refuse():
    raw = json.dumps({"middle_name": "Kumar", "retired_key": "x", "over_18": "sometimes"})
    assert af.load(raw) == {"middle_name": "Kumar"}
    assert af.load(None) == {} and af.load("") == {} and af.load("[1,2]") == {}


# ── which saved answer a form field asks for ─────────────────────────────────

@pytest.mark.parametrize("label,want", [
    ("Middle Name | middle_name", "Kumar"),
    ("Middle initial", "K"),
    ("Middle name (optional)", "Kumar"),
    ("City*", "Cincinnati"),
    ("State / Province", "Ohio"),
    ("ZIP code", "45220"),
    ("Address", "123 Main St"),
    ("Street address | address1", "123 Main St"),
    ("Country | country", "United States"),
    ("Country of citizenship | country", "India"),
    ("Are you at least 18 years of age?", "Yes"),
    ("Have you ever been convicted of a felony?", "No"),
    ("Are you subject to a non-compete agreement?", "No"),
    ("What is the highest level of education you have completed?", "Master's degree"),
    ("Are you willing to travel?", "Yes"),
    ("How much travel are you willing to do?", "Up to 25%"),
    ("Security clearance level", "None"),
    ("Do you currently hold an active security clearance?", "No"),
    ("Time zone", "Eastern Time (ET)"),
    ("Reason for leaving your current employer", "Contract ended"),
    ("Languages spoken", "English (Fluent), Telugu (Native or bilingual)"),
    ("Certifications", "AWS Solutions Architect"),
])
def test_a_saved_answer_is_offered_for_the_question_that_asks_it(label, want):
    assert af.answer_for_label(label, af.load(SAVED)) == want


@pytest.mark.parametrize("label", [
    # one interpreter each, never a saved default (field_guards)
    "Will you now or in the future require sponsorship?",
    "Are you authorized to work in the United States?",
    "Countries you are authorized to work in",
    "Gender", "Race / ethnicity", "Veteran status", "Disability status", "Pronouns",
    "How did you hear about us?",
    "g-recaptcha-response", "question_68444493",
    # a different question that only shares a word
    "City, State",
    "Which city would you prefer to work in? | city",
    "School city | city", "City of birth",
    "Email address | address", "Address line 2", "Company address",
    "State of the art experience?", "Country code", "Phone country | country",
    "Start date",                                   # a job's start date
    "Reason for leaving",                           # repeats under every job
    "Do you have a criminal record?",               # broader than a felony
    "Have you ever been convicted of a crime?",
    "Have you ever been convicted of a felony? If yes, please explain",
    "Are you willing to travel up to 25%?",         # wants Yes/No, not a range
    "Are you under 18?",
    "Title",                                        # a job title, not Mr./Ms.
    "First name", "Email", "Phone",                 # the fill pack's own fields
    "",
])
def test_a_saved_answer_is_never_offered_for_another_question(label):
    assert af.answer_for_label(label, af.load(SAVED)) is None


def test_an_empty_answer_is_left_for_the_user():
    assert af.answer_for_label("Middle name", {}) is None
    assert af.answer_for_label("Middle name", af.load({"city": "X"})) is None


def test_an_ambiguous_label_is_nobodys():
    data = af.load({**SAVED, "over_21": "Yes"})
    assert af.answer_for_label("Are you at least 18 (21 in some states) years old?", data) is None


def test_a_start_date_is_written_as_a_person_would_and_never_once_past():
    soon = date.today() + timedelta(days=30)
    data = af.load({"earliest_start_date": soon.isoformat()})
    assert af.answer_for_label("When can you start?", data) == f"{soon.strftime('%B')} {soon.day}, {soon.year}"
    gone = af.load({"earliest_start_date": (date.today() - timedelta(days=3)).isoformat()})
    assert af.answer_for_label("When can you start?", gone) is None


def test_the_overlap_rule_is_the_extensions_own():
    """content.js keywordOverlap, restated: if the two ever differ the server
    can no longer tell which answer the extension would hand to another field."""
    src = (ROOT / "extension" / "content.js").read_text()
    js = src[src.index("function keywordOverlap(a, b)"):]
    js = js[:js.index("\n}\n")]
    stop = js[js.index("new Set([") + 9:js.index("])")]
    assert {w.strip("'\" ") for w in stop.split(",")} == set(af._STOP)
    assert "w.length > 2" in js and "Math.max(wordsA.length, wordsB.length)" in js
    assert ">= 0.8" in src[src.index("async function recallFromMemory"):src.index("// ── API helper")]
    assert af.keyword_overlap("address line 2", "address line 1") == 1.0
    assert af.keyword_overlap("city of birth", "city") == 0.5


def test_an_answer_the_extension_would_copy_into_a_neighbour_is_withheld():
    labels = ["address line 1", "address line 2", "city"]
    answers = {"address line 1": "123 Main St", "city": "Cincinnati"}
    # "address line 2" has no answer and reads as the same words: line 1 is withheld.
    assert af.drop_borrowable(answers, labels) == {"city": "Cincinnati"}
    # With its own answer (from the profile or from memory) nothing is borrowed.
    assert af.drop_borrowable({**answers, "address line 2": "Apt 4"}, labels) == \
        {**answers, "address line 2": "Apt 4"}
    assert af.drop_borrowable(answers, labels, other_answered=["address line 2"]) == answers


# ── the routes ───────────────────────────────────────────────────────────────

def test_the_profile_routes_store_and_return_it(client, restore):
    r = client.put("/api/profile", json={"autofill": {
        "middle_name": " Kumar ", "city": "Cincinnati", "password": "hunter2",
        "languages": [{"language": "English", "proficiency": "Fluent"}],
        "projects": [{"title": "Volta", "description": "Verification platform"}]}})
    assert r.status_code == 200, r.text
    got = client.get("/api/profile").json()["autofill"]
    assert got["middle_name"] == "Kumar" and got["city"] == "Cincinnati"
    assert got["languages"] == [{"language": "English", "proficiency": "Fluent"}]
    assert got["projects"][0]["title"] == "Volta"
    assert "password" not in got


def test_saving_other_fields_keeps_it_and_a_partial_save_merges(client, restore):
    assert client.put("/api/profile", json={"autofill": {"middle_name": "Kumar", "city": "X"}}).status_code == 200
    assert client.put("/api/profile", json={"availability": "2 weeks"}).status_code == 200
    assert client.put("/api/profile", json={"autofill": {"city": "Columbus"}}).status_code == 200
    got = client.get("/api/profile").json()["autofill"]
    assert got["middle_name"] == "Kumar" and got["city"] == "Columbus"
    assert client.put("/api/profile", json={"autofill": {"middle_name": ""}}).status_code == 200
    assert "middle_name" not in client.get("/api/profile").json()["autofill"]


def test_recall_offers_the_saved_answers_and_refuses_the_guarded_questions(client, restore):
    assert client.put("/api/profile", json={"autofill": {
        "middle_name": "Kumar", "postal_code": "45220", "over_18": "Yes",
        "address_line1": "123 Main St"}}).status_code == 200
    labels = ["middle name | middle_name", "zip code", "are you at least 18 years of age?",
              "will you now or in the future require sponsorship?", "gender",
              "how did you hear about us?", "address line 1", "address line 2"]
    got = client.post("/api/recall-answers", json={"labels": labels}).json()["answers"]
    assert got["middle name | middle_name"] == "Kumar"
    assert got["zip code"] == "45220"
    assert got["are you at least 18 years of age?"] == "Yes"
    for refused in labels[3:6]:
        assert refused not in got
    # Line 2 has no answer, so line 1 is withheld rather than copied into it.
    assert "address line 1" not in got and "address line 2" not in got
    # With line 2 saved, both arrive.
    assert client.put("/api/profile", json={"autofill": {"address_line2": "Apt 4"}}).status_code == 200
    got = client.post("/api/recall-answers", json={"labels": labels}).json()["answers"]
    assert got["address line 1"] == "123 Main St" and got["address line 2"] == "Apt 4"


def test_recall_with_nothing_saved_is_unchanged(client, restore):
    assert client.put("/api/profile", json={"autofill": {k: "" for k in af.JSON_FIELDS}}).status_code == 200
    got = client.post("/api/recall-answers", json={"labels": ["middle name", "zip code"]}).json()
    assert got == {"answers": {}}


def test_the_address_country_is_where_the_applicant_lives():
    """The fill pack's residence_country: the Country the user typed in their
    address beats a guess from the location line, and is never defaulted."""
    from app.api import server
    typed = SimpleNamespace(location="Cincinnati, OH", autofill_json=af.dumps({"country": "Canada"}))
    assert server._residence_country(typed) == "Canada"
    assert server._residence_country(SimpleNamespace(location="Cincinnati, OH", autofill_json=None)) \
        == "United States"
    assert server._residence_country(SimpleNamespace(location="", autofill_json=None)) == ""
