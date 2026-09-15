"""Country detection from free-text job locations — the single source of truth.

Used by discovery (drop postings outside the user's preferred country), the
rule filter, and retrieval so every stage of the pipeline agrees on what
country a posting belongs to. Detection is intentionally conservative: when a
location is ambiguous/unknown we KEEP it rather than risk dropping good jobs.

RESOLUTION ORDER MATTERS, and getting it wrong is expensive in both directions
(2026-09-12). City names were tested BEFORE the ``, XX`` US state code, so every
US market named after a foreign city was classified foreign and dropped for US
users: Dublin OH, Melbourne FL, Vancouver WA, Paris TX, Athens GA, Rome GA,
Lima OH, Cairo IL, Wellington FL, Vienna VA. The tiers below fix that without
losing "Toronto, CA", because a US state code only wins when it is NOT the ISO-2
code of a country the string's own city names point at.

The second half of the same bug was coverage: 21 countries meant most of Europe
resolved to "unknown", and unknown is KEPT. A US user's queue filled with
Zurich, Milan, Stockholm and Tel Aviv postings that Tier-1 then paid to reject.
"""
from __future__ import annotations

import re

_US_STATE_CODES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
}

# Explicit US signals. NOTE: bare "america" is deliberately NOT one — "Latin
# America", "South America" and "North America" (which also spans Canada and
# Mexico) are not the United States. "United States of America" still matches
# via "united states"; "USA"/"U.S.A"/"US" cover the abbreviations.
_US_SIGNALS = ["united states", "usa", "u.s.a", "u.s.", " us ", "remote us", "us remote"]

# country -> the country's OWN names/aliases. These are unambiguous: a string
# containing them names that country no matter what else it contains, so they
# are tested before the US state-code heuristic ("Toronto, ON, Canada").
#
# NOTE: the "Search jobs in country" select in app/templates/dashboard.html
# mirrors these keys — when adding a country here, add its <option> there too.
_COUNTRY_NAMES = {
    "united kingdom": ["united kingdom", " uk", "u.k", "england", "scotland", "wales",
                       "great britain", "britain"],
    "canada": ["canada", "ontario", "british columbia", "alberta", "québec", "quebec"],
    "india": ["india", "bharat"],
    "germany": ["germany", "deutschland"],
    "france": ["france"],
    "spain": ["spain", "españa"],
    "netherlands": ["netherlands", "holland"],
    "ireland": ["ireland", "éire"],
    "australia": ["australia"],
    "poland": ["poland", "polska"],
    "portugal": ["portugal"],
    "brazil": ["brazil", "brasil"],
    "mexico": ["mexico", "méxico"],
    "singapore": ["singapore"],
    "japan": ["japan"],
    "philippines": ["philippines"],
    "ukraine": ["ukraine"],
    "nigeria": ["nigeria"],
    "pakistan": ["pakistan"],
    "argentina": ["argentina"],
    "switzerland": ["switzerland", "schweiz", "suisse"],
    "austria": ["austria", "österreich"],
    "italy": ["italy", "italia"],
    "sweden": ["sweden", "sverige"],
    "norway": ["norway", "norge"],
    "denmark": ["denmark", "danmark"],
    "finland": ["finland", "suomi"],
    "belgium": ["belgium", "belgique", "belgië"],
    "czechia": ["czechia", "czech republic"],
    "romania": ["romania", "românia"],
    "hungary": ["hungary", "magyarország"],
    "greece": ["greece", "hellas"],
    "turkey": ["turkey", "türkiye", "turkiye"],
    "israel": ["israel"],
    "united arab emirates": ["united arab emirates", "uae"],
    "south africa": ["south africa"],
    "kenya": ["kenya"],
    "egypt": ["egypt"],
    "indonesia": ["indonesia"],
    "vietnam": ["vietnam", "viet nam"],
    "thailand": ["thailand"],
    "malaysia": ["malaysia"],
    "south korea": ["south korea", "korea"],
    "china": ["china"],
    "taiwan": ["taiwan"],
    "hong kong": ["hong kong"],
    "new zealand": ["new zealand"],
    "chile": ["chile"],
    "colombia": ["colombia"],
    "peru": ["peru"],
    "costa rica": ["costa rica"],
    "uruguay": ["uruguay"],
    "estonia": ["estonia"],
    "latvia": ["latvia"],
    "lithuania": ["lithuania"],
    "bulgaria": ["bulgaria"],
    "croatia": ["croatia"],
    "serbia": ["serbia"],
    "slovakia": ["slovakia"],
    "slovenia": ["slovenia"],
}

# country -> city tokens. WEAKER than a country name: a US state code beats a
# city (Dublin OH is in Ohio), unless the state code happens to be that
# country's own ISO-2 code (Toronto, CA).
#
# Deliberately excluded because the US city is the common one: San Jose (CA),
# Columbia (SC/MD — note Colombia is spelled differently and is safe),
# Birmingham (AL) and Manchester (NH) are kept for the UK only because the
# state-code tier now runs first and catches the US forms.
_COUNTRY_CITIES = {
    "united kingdom": ["london", "manchester", "birmingham", "edinburgh", "glasgow",
                       "bristol", "leeds", "cambridge", "oxford", "belfast", "cardiff"],
    "canada": ["toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary",
               "winnipeg", "edmonton", "mississauga", "waterloo"],
    "india": ["bangalore", "bengaluru", "hyderabad", "mumbai", "new delhi", "delhi",
              "pune", "chennai", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad"],
    "germany": ["berlin", "munich", "münchen", "frankfurt", "hamburg", "cologne", "köln",
                "stuttgart", "düsseldorf", "dusseldorf", "leipzig"],
    "france": ["paris", "lyon", "marseille", "toulouse", "bordeaux", "nantes", "lille"],
    "spain": ["madrid", "barcelona", "valencia", "seville", "sevilla", "málaga", "malaga"],
    "netherlands": ["amsterdam", "rotterdam", "the hague", "den haag", "utrecht", "eindhoven"],
    "ireland": ["dublin", "cork", "galway", "limerick"],
    "australia": ["sydney", "melbourne", "brisbane", "perth", "canberra", "adelaide"],
    "poland": ["warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk", "gdańsk", "poznan"],
    "portugal": ["lisbon", "lisboa", "porto", "braga"],
    "brazil": ["são paulo", "sao paulo", "rio de janeiro", "belo horizonte", "curitiba"],
    "mexico": ["mexico city", "guadalajara", "monterrey", "querétaro", "queretaro"],
    "japan": ["tokyo", "osaka", "kyoto", "yokohama"],
    "philippines": ["manila", "cebu", "makati", "taguig"],
    "ukraine": ["kyiv", "kiev", "lviv", "kharkiv", "odesa"],
    "nigeria": ["lagos", "abuja"],
    "pakistan": ["karachi", "lahore", "islamabad"],
    "argentina": ["buenos aires", "córdoba", "cordoba"],
    "switzerland": ["zurich", "zürich", "geneva", "genève", "basel", "lausanne", "bern", "zug"],
    "austria": ["vienna", "wien", "graz", "linz", "salzburg"],
    "italy": ["milan", "milano", "rome", "roma", "turin", "torino", "bologna", "naples", "napoli"],
    "sweden": ["stockholm", "gothenburg", "göteborg", "goteborg", "malmö", "malmo", "uppsala"],
    "norway": ["oslo", "bergen", "trondheim", "stavanger"],
    "denmark": ["copenhagen", "københavn", "kobenhavn", "aarhus", "odense"],
    "finland": ["helsinki", "espoo", "tampere", "oulu"],
    "belgium": ["brussels", "bruxelles", "antwerp", "antwerpen", "ghent", "gent", "leuven"],
    "czechia": ["prague", "praha", "brno", "ostrava"],
    "romania": ["bucharest", "bucurești", "bucuresti", "cluj", "cluj-napoca", "timisoara", "timișoara", "iasi", "iași"],
    "hungary": ["budapest", "debrecen", "szeged"],
    "greece": ["athens", "thessaloniki", "patras"],
    "turkey": ["istanbul", "ankara", "izmir", "bursa"],
    "israel": ["tel aviv", "tel-aviv", "jerusalem", "haifa", "herzliya", "ra'anana"],
    "united arab emirates": ["dubai", "abu dhabi", "sharjah"],
    "south africa": ["johannesburg", "cape town", "pretoria", "durban"],
    "kenya": ["nairobi", "mombasa"],
    "egypt": ["cairo", "giza", "alexandria"],
    "indonesia": ["jakarta", "bandung", "surabaya"],
    "vietnam": ["hanoi", "ho chi minh", "da nang"],
    "thailand": ["bangkok", "chiang mai"],
    "malaysia": ["kuala lumpur", "penang", "cyberjaya"],
    "south korea": ["seoul", "busan", "incheon"],
    "china": ["shanghai", "beijing", "shenzhen", "guangzhou", "hangzhou", "chengdu"],
    "taiwan": ["taipei", "hsinchu"],
    "hong kong": ["kowloon"],
    "new zealand": ["auckland", "wellington", "christchurch"],
    "chile": ["santiago de chile", "valparaíso", "valparaiso"],
    "colombia": ["bogota", "bogotá", "medellin", "medellín", "cali"],
    "peru": ["lima"],
    "uruguay": ["montevideo"],
    "estonia": ["tallinn", "tartu"],
    "latvia": ["riga"],
    "lithuania": ["vilnius", "kaunas"],
    "bulgaria": ["sofia", "plovdiv"],
    "croatia": ["zagreb", "split"],
    "serbia": ["belgrade", "beograd", "novi sad"],
    "slovakia": ["bratislava", "košice", "kosice"],
    "slovenia": ["ljubljana", "maribor"],
}

# ISO-2 codes that COLLIDE with a US state code. Only these matter: when a
# location ends in ", XX" and XX is a US state code, the state wins UNLESS the
# string's own city names point at the country whose ISO-2 code is exactly XX.
#   "Toronto, CA"    -> city says canada, canada's ISO-2 is CA  -> canada
#   "Vancouver, WA"  -> city says canada, canada's ISO-2 is CA  -> united states
#   "Bengaluru, IN"  -> city says india,  india's  ISO-2 is IN  -> india
#   "Dublin, OH"     -> city says ireland, ireland's ISO-2 is IE -> united states
_ISO2_US_STATE_COLLISIONS = {
    "canada": "ca",
    "india": "in",
    "germany": "de",
    "indonesia": "id",
    "argentina": "ar",
    "colombia": "co",
    "israel": "il",
}


def _compile(tokens) -> list:
    # Letter boundaries so "india" can't match "Indiana", "us" can't match
    # "status", and "uk" can't match inside another word. The boundary is
    # letter-based rather than \b so "u.s." style signals keep working.
    return [re.compile(rf"(?<![a-z]){re.escape(t.strip())}(?![a-z])") for t in tokens]


_US_RES = _compile(_US_SIGNALS)
_NAME_RES = {c: _compile(toks) for c, toks in _COUNTRY_NAMES.items()}
_CITY_RES = {c: _compile(toks) for c, toks in _COUNTRY_CITIES.items()}


# Region anchors ("Remote — EU only", "EMEA", "APAC") → member countries we know.
# A region-locked posting is kept only for users whose country is in the region.
_REGION_MEMBERS = {
    "eu": {"germany", "france", "spain", "netherlands", "ireland", "poland", "portugal",
           "austria", "italy", "sweden", "denmark", "finland", "belgium", "czechia",
           "romania", "hungary", "greece", "estonia", "latvia", "lithuania", "bulgaria",
           "croatia", "slovakia", "slovenia"},
    "europe": {"germany", "france", "spain", "netherlands", "ireland", "poland", "portugal",
               "austria", "italy", "sweden", "norway", "denmark", "finland", "belgium",
               "czechia", "romania", "hungary", "greece", "estonia", "latvia", "lithuania",
               "bulgaria", "croatia", "serbia", "slovakia", "slovenia", "switzerland",
               "united kingdom", "ukraine"},
    "emea": {"germany", "france", "spain", "netherlands", "ireland", "poland",
             "portugal", "united kingdom", "ukraine", "nigeria", "switzerland",
             "austria", "italy", "sweden", "norway", "denmark", "finland", "belgium",
             "israel", "united arab emirates", "south africa", "kenya", "egypt", "turkey"},
    "apac": {"india", "australia", "singapore", "japan", "philippines", "pakistan",
             "indonesia", "vietnam", "thailand", "malaysia", "south korea", "china",
             "taiwan", "hong kong", "new zealand"},
    "latam": {"brazil", "mexico", "argentina", "chile", "colombia", "peru",
              "costa rica", "uruguay"},
}
_REGION_RES = {
    region: _compile(tokens)
    for region, tokens in {
        # Bare "europe"/"eu" were missing, and "Remote (Europe)" is the single
        # most common phrasing on European boards — without them the posting
        # read as location-unknown and was KEPT for a US user.
        "eu": ["eu only", "eu-only", "european union", "eea", "eu", "eu remote", "remote eu"],
        "europe": ["europe", "europe only", "europe-only", "european",
                   "within europe", "european residents", "european timezone",
                   "cet", "cest"],
        "emea": ["emea"],
        "apac": ["apac", "asia-pacific", "asia pacific"],
        "latam": ["latam", "latin america"],
    }.items()
}
# Most specific first: "EU only" should win over the looser "europe" anchor.
_REGION_ORDER = ("eu", "emea", "apac", "latam", "europe")


def norm_country(name: str) -> str:
    """Normalize a country name/alias to its canonical lowercase form."""
    n = (name or "").strip().lower().rstrip(".")
    aliases = {
        "us": "united states", "u.s": "united states", "usa": "united states",
        "u.s.a": "united states", "america": "united states", "united states of america": "united states",
        "uk": "united kingdom", "u.k": "united kingdom", "england": "united kingdom",
        "great britain": "united kingdom", "britain": "united kingdom",
        "deutschland": "germany", "holland": "netherlands", "the netherlands": "netherlands",
        "bharat": "india", "republic of india": "india", "brasil": "brazil",
        "méxico": "mexico", "españa": "spain", "republic of ireland": "ireland",
        "aus": "australia", "ca": "canada", "can": "canada",
        "uae": "united arab emirates", "korea": "south korea",
        "czech republic": "czechia", "türkiye": "turkey", "turkiye": "turkey",
        "schweiz": "switzerland", "suisse": "switzerland", "österreich": "austria",
        "italia": "italy", "sverige": "sweden", "norge": "norway", "danmark": "denmark",
        "suomi": "finland", "polska": "poland",
    }
    return aliases.get(n, n)


def detect_region(location: str) -> str:
    """Region anchor ('eu', 'europe', 'emea', 'apac', 'latam'), '' if none."""
    loc = " " + (location or "").lower().strip() + " "
    for region in _REGION_ORDER:
        if any(r.search(loc) for r in _REGION_RES[region]):
            return region
    return ""


def _match_country(loc: str, table: dict) -> str:
    for country, res in table.items():
        if any(r.search(loc) for r in res):
            return country
    return ""


def detect_country(location: str) -> str:
    """Best-effort country guess from a free-text location. '' when unknown.

    Tiers, most-specific first — see the module docstring for why the order is
    load-bearing:

      1. explicit US signals            "Remote, USA"        -> united states
      2. explicit foreign COUNTRY name  "Toronto, ON, Canada"-> canada
      3. ", XX" US state code           "Dublin, OH"         -> united states
         (unless XX is the ISO-2 of a country this string's cities name)
      4. foreign CITY name              "Dublin"             -> ireland
      5. unknown
    """
    loc = " " + (location or "").lower().strip() + " "
    if not loc.strip():
        return ""

    if any(r.search(loc) for r in _US_RES):
        return "united states"

    named = _match_country(loc, _NAME_RES)
    if named:
        return named

    city = _match_country(loc, _CITY_RES)

    state_codes = {c for c in re.findall(r",\s*([a-z]{2})\b", loc) if c in _US_STATE_CODES}
    if state_codes:
        # A US state code wins over a city name, unless the code IS that
        # country's ISO-2 ("Toronto, CA").
        if not (city and _ISO2_US_STATE_COLLISIONS.get(city) in state_codes):
            return "united states"

    return city


def location_allowed(location: str, remote: bool, preferred_country: str, remote_ok: bool) -> bool:
    """True if a posting should be kept for a user targeting `preferred_country`.

    Remote is NOT borderless: a remote role anchored to another country
    ("Remote — Berlin") or region ("Remote, EU only", "EMEA") still requires
    work authorization there, so it is treated like an on-site role in that
    country/region. Remote is kept only when it matches the user's own country,
    is truly global, or is unspecified.

    An EMPTY ``preferred_country`` means the user hasn't chosen one — no
    country gate is applied (better to show everything than to silently
    assume the wrong country).
    """
    preferred = norm_country(preferred_country)
    if not preferred:
        return True
    loc = (location or "").lower()
    detected = detect_country(loc)
    # An explicit match on the user's OWN country beats any region anchor:
    # "Remote - US/EU" names the user's country and must not be dropped by the
    # "EU" token, and "Berlin, Germany (EU)" is fine for a German user.
    if detected and detected == preferred:
        return True
    region = detect_region(loc)
    if region and preferred not in _REGION_MEMBERS[region]:
        return False  # region-locked posting, user outside the region
    if remote_ok and (remote or "remote" in loc or "anywhere" in loc or "worldwide" in loc):
        return (not detected) or detected == preferred
    if not detected:
        return True  # ambiguous/unknown — keep rather than over-filter
    return detected == preferred
