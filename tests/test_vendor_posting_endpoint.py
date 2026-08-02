"""Endpoint test: GET /application/{id}/sponsorship carries the vendor block.

The unit tests in test_vendor_posting.py prove the classifier. This proves the
classification actually reaches the user — including the STEM OPT caution, which
is the highest-consequence thing the product now says to an F-1 candidate, and
which is gated on the profile rather than shown to everyone.
"""
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import (
    Application,
    ApplicationStatus,
    Job,
    JobSource,
    UserProfile,
)

_VENDOR_JD = (
    "Our client, a leading financial services firm in Charlotte NC, is seeking a "
    "Java Developer. 12 month contract. Rate: $65/hr on W2. C2C also considered."
)
_DIRECT_JD = (
    "Join the Payments platform team. Full-time salaried position with equity. "
    "We build the APIs that move money for millions of businesses worldwide."
)


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("vendor-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        for p in s.exec(select(UserProfile).where(UserProfile.user_id == "local")).all():
            s.delete(p)
        s.commit()


def _seed(ext: str, description: str, work_auth: str | None):
    with get_session() as s:
        j = Job(source=JobSource.MANUAL, external_id=ext, company="Apex Staffing Solutions",
                title="Java Developer", url="http://x.test/1", description=description,
                user_id="local")
        s.add(j); s.commit(); s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, user_id="local")
        s.add(a); s.commit(); s.refresh(a)
        if work_auth is not None:
            s.add(UserProfile(user_id="local", work_authorization=work_auth))
            s.commit()
        return a.id


def test_vendor_block_reaches_the_user():
    _cleanup()
    app_id = _seed("vendor-test-1", _VENDOR_JD, "US Citizen")
    try:
        r = _client().get(f"/application/{app_id}/sponsorship")
        assert r.status_code == 200, r.text
        v = r.json().get("vendor")
        assert v, "vendor block missing from the sponsorship payload"
        assert v["is_vendor_posting"] is True
        assert v["label"] == "Staffing vendor"
        assert v["checklist"], "the before-you-apply checklist must be present"
        # A citizen gets the chain explanation but not the F-1 caution.
        assert v["work_auth_caution"] is None
    finally:
        _cleanup()


def test_stem_opt_caution_is_gated_on_the_profile():
    _cleanup()
    app_id = _seed("vendor-test-2", _VENDOR_JD, "F-1 OPT")
    try:
        r = _client().get(f"/application/{app_id}/sponsorship")
        assert r.status_code == 200, r.text
        v = r.json()["vendor"]
        assert v["work_auth_caution"], "F-1 user must get the I-983 / client-site caution"
        assert "I-983" in v["work_auth_caution"]
    finally:
        _cleanup()


def test_direct_employer_posting_carries_no_vendor_block():
    """No badge on an ordinary posting — a noisy badge is an ignored badge."""
    _cleanup()
    app_id = _seed("vendor-test-3", _DIRECT_JD, "F-1 OPT")
    try:
        r = _client().get(f"/application/{app_id}/sponsorship")
        assert r.status_code == 200, r.text
        assert r.json().get("vendor") is None
    finally:
        _cleanup()
