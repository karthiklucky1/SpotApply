"""Every field the profile form edits must come back from GET /api/profile.

The form ticks/fills only what the read returns, then submits every field.
A field the read omits therefore loads blank and the next save WIPES it —
"Open to relocation" (2026-09-26) and the articulation video URL (2026-09-27)
were both lost that way. This fails for the next one before a user finds it.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()

# Form-only controls that the JS converts before saving.
_CONVERTED = {"_history_state", "resume_location_mode"}


def _form_names() -> set:
    start = DASH.index('id="profile-form"')
    end = DASH.index("</form>", start)
    return set(re.findall(r'name="([a-z_]+)"', DASH[start:end])) - _CONVERTED


def test_every_profile_form_field_is_returned_by_the_read():
    from app.api.server import app
    data = TestClient(app).get("/api/profile").json()
    missing = sorted(n for n in _form_names() if n not in data)
    assert not missing, f"form fields the read does not return (a save would wipe them): {missing}"


def test_every_profile_form_field_is_accepted_by_the_update():
    from app.api.server import ProfileUpdate
    accepted = set(ProfileUpdate.model_fields)
    missing = sorted(n for n in _form_names() if n not in accepted)
    assert not missing, f"form fields the update silently drops: {missing}"


def test_self_identification_never_sends_the_old_affirmative_defaults():
    from app.api.server import _eeo_answer
    assert _eeo_answer("I am not a protected veteran") == "Decline to self-identify"
    assert _eeo_answer("No, I do not have a disability, or history/record of having a disability") \
        == "Decline to self-identify"
    assert _eeo_answer("") == "Decline to self-identify"
    assert _eeo_answer("Female") == "Female"


def test_opt_save_no_longer_overwrites_work_authorization():
    block = DASH.split("async function saveOptClock")[1].split("async function loadOptStrip")[0]
    assert "work_authorization: 'F-1 OPT'," not in block
    assert "if (!String(pr.work_authorization || '').trim()) body.work_authorization = 'F-1 OPT';" in block
