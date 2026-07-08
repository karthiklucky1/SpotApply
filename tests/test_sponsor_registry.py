"""Multi-country sponsor registry: per-country register ingestion (UK / Canada /
generic name lists), country-scoped lookup, and the licensed-sponsor badges that
assess() derives from them. US H-1B behavior must stay unchanged."""
import pytest

from app.intelligence import h1b_data
from app.intelligence.h1b_data import ingest_csv, ingest_register, lookup, has_country_data
from app.intelligence.sponsorship import assess, SponsorshipLikelihood


UK_REGISTER = """Organisation Name,Town/City,County,Type & Rating,Route
Monzo Bank Limited,London,,Worker (A rating),Skilled Worker
Monzo Bank Limited,London,,Worker (A rating),Global Business Mobility: Senior or Specialist Worker
DeepMind Technologies Limited,London,,Worker (A rating),Skilled Worker
"""

CA_LMIA = """Province/Territory,Program Stream,Employer,Address,Occupation,Approved LMIAs,Approved Positions
Ontario,High-wage,Shopify Inc,Ottawa ON,Software Developer,2,5
Ontario,High-wage,Shopify Inc,Ottawa ON,Data Engineer,1,3
British Columbia,Global Talent Stream,Hootsuite Media Inc,Vancouver BC,Developer,1,2
"""

US_STATS = """Fiscal Year,Employer (Petitioner) Name,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial
2024,Globex Corporation,10,1,20,2
2024,Initech LLC,1,3,0,1
"""


@pytest.fixture(autouse=True)
def _fresh_state():
    """Reset the in-process cache AND the shared test DB table around each test
    so registry rows never leak into other tests (assess() reads this table)."""
    def _wipe():
        from app.db.init_db import get_session, init_db
        from app.db.models import H1BSponsor
        from sqlmodel import delete
        init_db()
        with get_session() as session:
            session.exec(delete(H1BSponsor))
            session.commit()
        h1b_data.refresh_cache()
    _wipe()
    yield
    _wipe()


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_uk_register_ingest_and_scoped_lookup(tmp_path):
    n = ingest_register(_write(tmp_path, "uk.csv", UK_REGISTER), "united kingdom")
    assert n == 2  # rows aggregate per employer
    rec = lookup("Monzo Bank", country="united kingdom")
    assert rec and rec["record_type"] == "license"
    assert "Skilled Worker" in rec["detail"]
    # Country alias resolves through geo.norm_country.
    assert lookup("Monzo Bank", country="UK")
    # Country scoping: a UK license never leaks into the US (default) lookup.
    assert lookup("Monzo Bank") is None
    assert has_country_data("united kingdom") is True
    assert has_country_data("germany") is False


def test_uk_reupload_replaces_not_duplicates(tmp_path):
    path = _write(tmp_path, "uk.csv", UK_REGISTER)
    assert ingest_register(path, "united kingdom") == 2
    assert ingest_register(path, "united kingdom") == 2  # idempotent replace


def test_canada_lmia_positions_and_stream(tmp_path):
    ingest_register(_write(tmp_path, "ca.csv", CA_LMIA), "canada")
    rec = lookup("Shopify", country="canada")
    assert rec and rec["record_type"] == "license"
    assert rec["approvals"] == 8  # 5 + 3 approved positions aggregated
    assert "High-wage" in rec["detail"]


def test_generic_single_column_name_list(tmp_path):
    ingest_register(_write(tmp_path, "nl.csv", "Acme Robotics\nBeta Farms\n"), "netherlands")
    assert lookup("Acme Robotics", country="netherlands")
    assert lookup("Beta Farms", country="netherlands")


def test_ingest_register_rejects_us():
    with pytest.raises(ValueError):
        ingest_register("/nonexistent.csv", "united states")


def _write_xlsx(tmp_path, name, rows, title_rows=0):
    """Build an .xlsx like the official downloads (Canada's LMIA workbooks
    put title/notes rows above the real header)."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for i in range(title_rows):
        ws.append([f"Temporary Foreign Worker Program note {i + 1}"])
    for row in rows:
        ws.append(row)
    p = tmp_path / name
    wb.save(str(p))
    return str(p)


def test_canada_lmia_xlsx_with_title_rows(tmp_path):
    path = _write_xlsx(tmp_path, "ca.xlsx", [
        ["Province/Territory", "Program Stream", "Employer", "Address",
         "Occupation", "Approved LMIAs", "Approved Positions"],
        ["Ontario", "High-wage", "Shopify Inc", "Ottawa ON",
         "Software Developer", 2, 5.0],   # Excel stores numbers as floats
        ["Ontario", "High-wage", "Shopify Inc", "Ottawa ON",
         "Data Engineer", 1, 3],
    ], title_rows=2)
    n = ingest_register(path, "canada")
    assert n == 1
    rec = lookup("Shopify", country="canada")
    assert rec and rec["record_type"] == "license"
    assert rec["approvals"] == 8  # 5.0 + 3, not 50 + 3
    assert "High-wage" in rec["detail"]


def test_us_stats_xlsx(tmp_path):
    path = _write_xlsx(tmp_path, "us.xlsx", [
        ["Fiscal Year", "Employer (Petitioner) Name",
         "Initial Approval", "Initial Denial"],
        [2024.0, "Globex Corporation", 10, 1],
    ])
    ingest_csv(path)
    rec = lookup("Globex")
    assert rec and rec["approvals"] == 10 and rec["year"] == 2024


def test_legacy_xls_gives_clear_error(tmp_path):
    p = tmp_path / "old.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(ValueError, match="save it as .xlsx or CSV"):
        ingest_register(str(p), "canada")


def test_us_stats_path_unchanged(tmp_path):
    ingest_csv(_write(tmp_path, "us.csv", US_STATS))
    rec = lookup("Globex")  # default country = US
    assert rec and rec["record_type"] == "stats"
    assert rec["approvals"] == 30 and rec["denials"] == 3
    a = assess(company="Globex Corporation", description="Engineer role.",
               location="Austin, TX")
    assert a.likelihood == SponsorshipLikelihood.HIGH
    assert a.badge == "Sponsors H-1B"


def test_assess_uk_licensed_sponsor_badge(tmp_path):
    ingest_register(_write(tmp_path, "uk.csv", UK_REGISTER), "united kingdom")
    a = assess(company="Monzo Bank", description="Backend engineer.",
               location="London, United Kingdom")
    assert a.likelihood == SponsorshipLikelihood.HIGH
    assert a.badge == "Licensed sponsor"
    assert "sponsor register" in a.reason.lower()


def test_assess_uk_not_on_register_is_low_with_caveat(tmp_path):
    ingest_register(_write(tmp_path, "uk.csv", UK_REGISTER), "united kingdom")
    a = assess(company="Tiny Startup", description="Backend engineer.",
               location="Leeds, United Kingdom")
    assert a.likelihood == SponsorshipLikelihood.LOW
    assert a.badge == "Not on register"
    assert "registered name" in a.reason  # verify-the-legal-name caveat


def test_assess_country_without_register_stays_unknown(tmp_path):
    ingest_register(_write(tmp_path, "uk.csv", UK_REGISTER), "united kingdom")
    a = assess(company="Siemens", description="Engineer.", location="Berlin, Germany")
    assert a.likelihood == SponsorshipLikelihood.UNKNOWN
    assert a.badge == "Check visa policy"


def test_assess_refusal_beats_register(tmp_path):
    ingest_register(_write(tmp_path, "uk.csv", UK_REGISTER), "united kingdom")
    a = assess(company="Monzo Bank",
               description="You must have the right to work in the UK. No sponsorship.",
               location="London, United Kingdom")
    assert a.likelihood == SponsorshipLikelihood.LOW
    assert a.explicitly_refuses is True
