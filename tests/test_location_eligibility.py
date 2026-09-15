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
from app.common.geo import detect_country, detect_region, location_allowed

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
