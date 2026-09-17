"""Location eligibility: the two ways the country gate used to be wrong.

Both directions cost real jobs, in opposite ways:

  * a US market named after a foreign city ("Dublin, OH") was classified
    foreign and DROPPED for a US user — a good job the user never saw;
  * most of Europe was not in the signal table at all, so "Zurich" resolved to
    unknown, unknown is KEPT, and Tier-1 then paid to reject it — a bad job
    that consumed the budget meant for good ones.

These tests pin the tier order (US signal > foreign country name > US state
code > foreign city) and the region anchors.
"""
import pytest

from app.common.geo import detect_country, detect_region, location_allowed, norm_country

US = "United States"


# ── The US markets that read as foreign ──────────────────────────────────────

def test_us_city_named_after_a_foreign_city_is_us():
    # Every one of these is a real US market with a real tech employer base,
    # and every one of them resolved to the foreign country before the state
    # code was ever consulted.
    for loc in ("Dublin, OH", "Melbourne, FL", "Vancouver, WA", "Paris, TX",
                "Athens, GA", "Rome, GA", "Lima, OH", "Cairo, IL",
                "Wellington, FL", "Vienna, VA", "Manchester, NH",
                "Birmingham, AL", "Milan, MI", "Naples, FL"):
        assert detect_country(loc) == "united states", loc


def test_us_city_named_after_a_foreign_city_is_kept_for_a_us_user():
    assert location_allowed("Dublin, OH", False, US, True)
    assert location_allowed("Melbourne, FL", False, US, True)


def test_state_code_does_not_beat_the_country_s_own_iso2():
    # "Toronto, CA" is Canadian even though CA is also California: the city
    # names Canada and CA is Canada's ISO-2.
    assert detect_country("Toronto, CA") == "canada"
    assert detect_country("Bengaluru, IN") == "india"
    assert detect_country("Berlin, DE") == "germany"
    # ...but a US state code that is NOT that country's ISO-2 still wins.
    assert detect_country("Vancouver, WA") == "united states"
    assert detect_country("Dublin, OH") == "united states"


def test_explicit_country_name_always_wins():
    assert detect_country("Toronto, ON, Canada") == "canada"
    assert detect_country("Dublin, Ireland") == "ireland"
    # Even when a US state code is present, a spelled-out country name wins.
    assert detect_country("Vancouver, BC, Canada") == "canada"


def test_bare_city_still_resolves_when_there_is_no_state_code():
    assert detect_country("Dublin") == "ireland"
    assert detect_country("Melbourne") == "australia"


# ── The countries that were missing entirely ─────────────────────────────────

def test_european_and_middle_eastern_markets_are_detected():
    for loc, want in (("Zurich, Switzerland", "switzerland"),
                      ("Zürich", "switzerland"),
                      ("Milan", "italy"),
                      ("Stockholm", "sweden"),
                      ("Copenhagen", "denmark"),
                      ("Helsinki", "finland"),
                      ("Brussels", "belgium"),
                      ("Prague", "czechia"),
                      ("Bucharest", "romania"),
                      ("Budapest", "hungary"),
                      ("Tel Aviv", "israel"),
                      ("Dubai", "united arab emirates"),
                      ("Istanbul", "turkey"),
                      ("Vienna, Austria", "austria"),
                      ("Oslo", "norway")):
        assert detect_country(loc) == want, loc


def test_those_markets_are_now_filtered_out_for_a_us_user():
    for loc in ("Zurich, Switzerland", "Milan, Italy", "Stockholm", "Tel Aviv",
                "Dubai", "Prague", "Budapest"):
        assert not location_allowed(loc, False, US, True), loc


# ── Region anchors ───────────────────────────────────────────────────────────

def test_plain_europe_remote_is_a_region_anchor():
    # "Remote (Europe)" is the commonest phrasing on European boards and was
    # not an anchor, so it read as location-unknown and was kept for US users.
    assert detect_region("Remote (Europe)") == "europe"
    assert not location_allowed("Remote (Europe)", True, US, True)
    assert not location_allowed("Remote - EU", True, US, True)
    assert not location_allowed("Remote, EU only", True, US, True)
    assert not location_allowed("Remote (CET timezone)", True, US, True)


def test_region_anchor_does_not_drop_a_posting_that_names_the_user_s_country():
    # "US/EU remote" names the user's own country: the EU token must not win.
    assert location_allowed("Remote - US/EU", True, US, True)
    assert location_allowed("Remote (US)", True, US, True)


def test_european_user_keeps_european_remote():
    assert location_allowed("Remote (Europe)", True, "Germany", True)
    assert location_allowed("Remote, EU only", True, "Ireland", True)
    # ...and a UK user is inside "Europe" but outside "EU only".
    assert location_allowed("Remote (Europe)", True, "United Kingdom", True)
    assert not location_allowed("Remote, EU only", True, "United Kingdom", True)


# ── The conservative default is preserved ────────────────────────────────────

def test_unknown_location_is_still_kept():
    assert detect_country("Somewhere Else") == ""
    assert location_allowed("Somewhere Else", False, US, True)
    assert location_allowed("", False, US, True)


def test_no_preferred_country_means_no_gate():
    assert location_allowed("Zurich, Switzerland", False, "", True)


# ── 2026-09-17 audit: 7 of 15 opened cards were in the wrong country ─────────
# All but one were Ashby postings whose location had been stored BLANK (the
# scraper read a key the API does not emit — tests/test_ashby_scraper.py). With
# the location present, these are the strings the gate sees. The Albania case
# also needed the table: "Tiranë, Albania" resolved to unknown, and unknown is
# kept — even for a remote posting anchored 27 countries away from the user.

UK = "United Kingdom"


@pytest.mark.parametrize("location, remote", [
    ("Warsaw, Poland (Hybrid)", False),                              # Snowflake
    ("Remote, Tiranë, Albania +27 more", True),
    ("London, England, United Kingdom (Hybrid)", False),             # Lendable, Magentic
    ("Vilnius, Lithuania / Kaunas, Lithuania (hybrid)", False),     # Nord Security
    ("Remote - Poland", True),                                       # Addepto
])
def test_audit_wrong_country_postings_are_dropped_for_a_us_user(location, remote):
    assert not location_allowed(location, remote, US, True), location


def test_audit_philippines_remote_is_dropped_for_a_uk_user():
    # Paddle, scored 90 for a UK user: remote is not borderless.
    assert not location_allowed("Philippines (Remote)", True, UK, True)


def test_audit_positives_are_still_kept():
    assert location_allowed("San Francisco, CA (Hybrid)", False, US, True)
    assert location_allowed("Remote - US", True, US, True)
    assert location_allowed("London, UK", False, UK, True)


def test_multi_location_string_is_dropped_when_no_site_is_in_the_user_s_country():
    # A remote posting listing many sites is anchored to ALL of them; none is
    # the user's, so it goes — remote or not.
    loc = "Remote · Tiranë, Albania · Lisbon, Portugal · Madrid · Berlin +2 more"
    assert not location_allowed(loc, True, US, True)
    # ...but a site in the user's country anywhere in it keeps it — judged
    # site by site, because read whole the string is Albania (a foreign
    # country NAME outranks the user's own ", TX" state code).
    assert location_allowed("Remote · Tiranë, Albania · Austin, TX", True, US, True)
    assert location_allowed("Remote · Tiranë, Albania · Lisbon, Portugal", True, "Portugal", True)
    assert location_allowed("Vilnius, Lithuania / Kaunas, Lithuania / New York, NY", False, US, True)
    # Commas are NOT site separators: one address keeps the country that names it.
    assert not location_allowed("Toronto, ON, Canada", False, US, True)
    assert not location_allowed("Dublin, Ireland / Cork, Ireland", False, US, True)


# ── The countries that were still missing ───────────────────────────────────

def test_balkan_caucasus_central_asian_gulf_and_latam_markets_are_detected():
    for loc, want in (("Tiranë, Albania", "albania"), ("Tirana", "albania"),
                      ("Sarajevo", "bosnia and herzegovina"),
                      ("Skopje, North Macedonia", "north macedonia"),
                      ("Podgorica", "montenegro"), ("Pristina, Kosovo", "kosovo"),
                      ("Chișinău", "moldova"), ("Minsk, Belarus", "belarus"),
                      ("Tbilisi", "georgia"), ("Yerevan, Armenia", "armenia"),
                      ("Baku", "azerbaijan"), ("Almaty, Kazakhstan", "kazakhstan"),
                      ("Tashkent", "uzbekistan"), ("Nicosia, Cyprus", "cyprus"),
                      ("Valletta, Malta", "malta"), ("Luxembourg", "luxembourg"),
                      ("Reykjavik, Iceland", "iceland"), ("Dhaka", "bangladesh"),
                      ("Colombo, Sri Lanka", "sri lanka"), ("Kathmandu", "nepal"),
                      ("Riyadh, Saudi Arabia", "saudi arabia"), ("Doha", "qatar"),
                      ("Amman, Jordan", "jordan"), ("Beirut, Lebanon", "lebanon"),
                      ("Casablanca", "morocco"), ("Tunis", "tunisia"),
                      ("Accra, Ghana", "ghana"), ("Quito", "ecuador"),
                      ("La Paz, Bolivia", "bolivia"), ("Asunción", "paraguay"),
                      ("Caracas", "venezuela"), ("Guatemala City", "guatemala"),
                      ("Panama City, Panama", "panama"),
                      ("Santo Domingo", "dominican republic"),
                      ("San Salvador", "el salvador"), ("Tegucigalpa", "honduras"),
                      ("Phnom Penh", "cambodia"),
                      ("Santiago", "chile"), ("CDMX", "mexico")):
        assert detect_country(loc) == want, loc


def test_those_markets_are_filtered_out_for_a_us_user():
    for loc in ("Tirana", "Minsk", "Tbilisi", "Riyadh", "Doha", "Casablanca",
                "Quito", "Caracas", "Dhaka", "Valletta, Malta", "Amman, Jordan"):
        assert not location_allowed(loc, False, US, True), loc


# ── Country names that are also US place names ───────────────────────────────
# These live in the CITY tier so a ", XX" state code beats them; in the name
# tier the country would have won and dropped a real US job.

def test_us_places_named_after_countries_are_us():
    for loc in ("Malta, NY", "West Jordan, UT", "Lebanon, NH", "Lebanon, PA",
                "Lebanon, TN", "Panama City, FL", "Panama City Beach, FL"):
        assert detect_country(loc) == "united states", loc
        assert location_allowed(loc, False, US, True), loc


def test_the_same_names_alone_or_abroad_are_the_country():
    assert detect_country("Malta") == "malta"
    assert detect_country("Jordan") == "jordan"
    assert detect_country("Lebanon") == "lebanon"
    assert detect_country("Panama") == "panama"


def test_georgia_is_never_a_country_by_name_alone():
    # "Atlanta, Georgia" is far commoner than the country on the boards we read.
    # Unknown is kept, which is the status quo — and Tbilisi still resolves.
    assert detect_country("Atlanta, Georgia") in ("", "united states")
    assert location_allowed("Atlanta, Georgia", False, US, True)
    assert detect_country("Tbilisi, Georgia") == "georgia"
    assert not location_allowed("Tbilisi, Georgia", False, US, True)


def test_armenia_the_colombian_city_is_colombia():
    assert detect_country("Armenia, Colombia") == "colombia"


# ── Spelled-out US states and territories ────────────────────────────────────

def test_spelled_out_state_names_are_us_signals():
    # No state code to win on, and "New Mexico" used to resolve to MEXICO.
    for loc in ("Lebanon, New Hampshire", "Albuquerque, New Mexico",
                "Jordan, Minnesota", "Austin, Texas", "Seattle, Washington"):
        assert detect_country(loc) == "united states", loc
    # The three deliberate exclusions: a spelled-out state that is also a
    # foreign place must not turn that place into the US.
    assert detect_country("Tijuana, Baja California, Mexico") == "mexico"
    assert detect_country("Angers, Maine-et-Loire, France") == "france"


def test_us_territories_are_us():
    for loc in ("San Juan, PR", "San Juan, Puerto Rico", "Hagåtña, Guam",
                "St. Thomas, U.S. Virgin Islands", "Remote - US Virgin Islands"):
        assert detect_country(loc) == "united states", loc
        assert location_allowed(loc, False, US, True), loc
    assert norm_country("Puerto Rico") == "united states"


# ── Region membership follows the new countries ─────────────────────────────

def test_new_countries_are_inside_their_regions():
    assert location_allowed("Remote (Europe)", True, "Albania", True)
    assert location_allowed("Remote, EU only", True, "Malta", True)
    assert not location_allowed("Remote, EU only", True, "Albania", True)
    assert location_allowed("Remote (EMEA)", True, "Saudi Arabia", True)
    # EMEA ⊇ Europe: a Romanian user was outside "Remote (EMEA)" before.
    assert location_allowed("Remote (EMEA)", True, "Romania", True)
    assert location_allowed("Remote (LATAM)", True, "Ecuador", True)
    assert location_allowed("Remote (APAC)", True, "Bangladesh", True)
    assert not location_allowed("Remote (APAC)", True, "Albania", True)
