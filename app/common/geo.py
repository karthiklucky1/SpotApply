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

Coverage was still short on 2026-09-17: a US user was delivered "Remote,
Tiranë, Albania +27 more" because Albania was in no table, so the string read
as unknown and unknown is kept. The Balkans, the Caucasus, Central Asia, the
Gulf, North Africa and most of Central America were missing the same way.

Adding them met the header's own rule in a new form: some COUNTRY names are
also US place names — Malta NY (a fab town), West Jordan UT, Lebanon NH/PA/TN,
Panama City FL, and Georgia the state. Those countries live in the CITY tier
(where a ", XX" state code beats them) instead of the name tier (which beats
the state code), and bare "georgia" is not a token at all: only Tbilisi is.
Spelled-out US state names are US signals for the same reason — "Lebanon, New
Hampshire" has no state code to win on, and "Albuquerque, New Mexico" used to
resolve to Mexico because the name tier saw "mexico" (a few state names are
excluded, see `_US_STATE_NAMES`).
"""
from __future__ import annotations

import re

_US_STATE_CODES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
    # US territories — a job in San Juan, PR is a US job for work authorization.
    "pr","gu",
}

# Spelled-out US state names — tier-1 US signals. Only the ones with no real
# foreign collision: "california" is left out (Baja California, Mexico),
# "maine" (Maine-et-Loire, France) and "georgia" (the country — "Atlanta,
# Georgia" stays unknown-and-kept rather than making every Tbilisi posting US).
_US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "colorado", "connecticut",
    "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "west virginia", "washington", "wisconsin", "wyoming",
]

# Every state (and DC) by code, for SUB-NATIONAL residence restrictions
# ("Candidates must be based in California"). This table is read only for a
# place named inside a restriction phrase or a user's home location, where the
# phrase itself says it is a place — so it can afford "california" and "maine",
# which `_US_STATE_NAMES` above leaves out of the free-text country signals.
# "georgia" stays out: bare "Georgia" is the country as often as the state on
# the boards we read, and a restriction that names it is left for a person.
_US_STATE_NAME_TO_CODE = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "hawaii": "hi", "idaho": "id", "illinois": "il",
    "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky",
    "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
    "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo",
    "montana": "mt", "nebraska": "ne", "nevada": "nv", "new hampshire": "nh",
    "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri",
    "south carolina": "sc", "south dakota": "sd", "tennessee": "tn",
    "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc", "washington dc": "dc", "washington, dc": "dc",
    "washington d.c": "dc", "washington, d.c": "dc", "puerto rico": "pr", "guam": "gu",
}
US_STATE_LABELS = {code: name.title() for name, code in _US_STATE_NAME_TO_CODE.items()
                   if name not in ("washington dc", "washington, dc", "washington d.c",
                                   "washington, d.c")}
US_STATE_LABELS["dc"] = "Washington, DC"
# Longest names first so "west virginia" is not read as "virginia".
_US_STATE_NAME_RES = [(re.compile(rf"(?<![a-z]){re.escape(n)}(?![a-z])"), c)
                      for n, c in sorted(_US_STATE_NAME_TO_CODE.items(),
                                         key=lambda kv: -len(kv[0]))]

# Explicit US signals. NOTE: bare "america" is deliberately NOT one — "Latin
# America", "South America" and "North America" (which also spans Canada and
# Mexico) are not the United States. "United States of America" still matches
# via "united states"; "USA"/"U.S.A"/"US" cover the abbreviations. Puerto Rico,
# Guam and the US Virgin Islands are US ("U.S. Virgin Islands" already matches
# through "u.s."; "US Virgin Islands" through " us ").
_US_SIGNALS = ["united states", "usa", "u.s.a", "u.s.", " us ", "remote us", "us remote",
               "puerto rico", "guam"] + _US_STATE_NAMES

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
    # ── added 2026-09-17 (the "+27 more" audit) ──
    "albania": ["albania", "shqipëri", "shqiperi"],
    "bosnia and herzegovina": ["bosnia and herzegovina", "bosnia & herzegovina", "bosnia"],
    "north macedonia": ["north macedonia", "macedonia"],
    "montenegro": ["montenegro"],
    "kosovo": ["kosovo"],
    "moldova": ["moldova"],
    "belarus": ["belarus"],
    # NOT bare "georgia" — that is Atlanta's state far more often than Tbilisi's
    # country on the boards we read. The country is detected by its cities.
    "georgia": ["republic of georgia", "sakartvelo"],
    # Ordered AFTER colombia on purpose: Armenia is also a Colombian city, and
    # the first table entry to match wins.
    "armenia": ["armenia"],
    "azerbaijan": ["azerbaijan"],
    "kazakhstan": ["kazakhstan"],
    "uzbekistan": ["uzbekistan"],
    "cyprus": ["cyprus"],
    "luxembourg": ["luxembourg", "luxemburg"],
    "iceland": ["iceland", "ísland"],
    "bangladesh": ["bangladesh"],
    "sri lanka": ["sri lanka"],
    "nepal": ["nepal"],
    "saudi arabia": ["saudi arabia", "saudi", "ksa"],
    "qatar": ["qatar"],
    "morocco": ["morocco", "maroc"],
    "tunisia": ["tunisia", "tunisie"],
    "ghana": ["ghana"],
    "ecuador": ["ecuador"],
    "bolivia": ["bolivia"],
    "paraguay": ["paraguay"],
    "venezuela": ["venezuela"],
    "guatemala": ["guatemala"],
    "dominican republic": ["dominican republic", "república dominicana", "republica dominicana"],
    "el salvador": ["el salvador"],
    "honduras": ["honduras"],
    "cambodia": ["cambodia"],
    # malta, jordan, lebanon and panama are detected in the CITY tier below:
    # each is also a US place name, and only that tier lets a state code win.
}

# country -> city tokens. WEAKER than a country name: a US state code beats a
# city (Dublin OH is in Ohio), unless the state code happens to be that
# country's own ISO-2 code (Toronto, CA).
#
# Deliberately excluded because the US city is the common one: San Jose (CA),
# Columbia (SC/MD — note Colombia is spelled differently and is safe),
# Birmingham (AL) and Manchester (NH) are kept for the UK only because the
# state-code tier now runs first and catches the US forms.
#
# A COUNTRY name appears in this tier when it is also a US place name — Malta
# NY, West Jordan UT, Lebanon NH/PA/TN, Panama City FL. Here "Malta, NY" is New
# York (state code wins) while bare "Malta" and "Valletta, Malta" are Malta; in
# the name tier the country would have beaten the state code every time.
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
    "mexico": ["mexico city", "cdmx", "ciudad de méxico", "ciudad de mexico",
               "guadalajara", "monterrey", "querétaro", "queretaro"],
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
    "vietnam": ["hanoi", "ho chi minh", "saigon", "da nang"],
    "thailand": ["bangkok", "chiang mai"],
    "malaysia": ["kuala lumpur", "penang", "cyberjaya"],
    "south korea": ["seoul", "busan", "incheon"],
    "china": ["shanghai", "beijing", "shenzhen", "guangzhou", "hangzhou", "chengdu"],
    "taiwan": ["taipei", "hsinchu"],
    "hong kong": ["kowloon"],
    "new zealand": ["auckland", "wellington", "christchurch"],
    # Bare "Santiago" is Chile's capital on the boards we read; the Spanish and
    # Dominican Santiagos arrive with their country named, and the name tier
    # runs first.
    "chile": ["santiago de chile", "santiago", "valparaíso", "valparaiso"],
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
    # ── added 2026-09-17 (the "+27 more" audit) ──
    "albania": ["tirana", "tiranë", "tirane", "durrës", "durres"],
    "bosnia and herzegovina": ["sarajevo", "banja luka"],
    "north macedonia": ["skopje"],
    "montenegro": ["podgorica"],
    "kosovo": ["pristina", "prishtina", "priština"],
    "moldova": ["chișinău", "chisinau", "kishinev"],
    "belarus": ["minsk"],
    "georgia": ["tbilisi", "batumi"],
    "armenia": ["yerevan"],
    "azerbaijan": ["baku"],
    "kazakhstan": ["almaty", "astana", "nur-sultan"],
    "uzbekistan": ["tashkent"],
    "cyprus": ["nicosia", "limassol", "larnaca"],
    "malta": ["malta", "valletta", "sliema"],
    "iceland": ["reykjavik", "reykjavík"],
    "bangladesh": ["dhaka", "chittagong"],
    "sri lanka": ["colombo"],
    "nepal": ["kathmandu"],
    "saudi arabia": ["riyadh", "jeddah", "dammam"],
    "qatar": ["doha"],
    "jordan": ["jordan", "amman"],
    "lebanon": ["lebanon", "beirut"],
    "morocco": ["casablanca", "rabat", "marrakech", "marrakesh", "tangier"],
    "tunisia": ["tunis"],
    "ghana": ["accra", "kumasi"],
    "ecuador": ["quito", "guayaquil"],
    "bolivia": ["la paz", "cochabamba"],
    "paraguay": ["asunción", "asuncion"],
    "venezuela": ["caracas", "maracaibo"],
    "panama": ["panama"],
    "dominican republic": ["santo domingo"],
    "el salvador": ["san salvador"],
    "honduras": ["tegucigalpa", "san pedro sula"],
    "cambodia": ["phnom penh"],
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
_EU_MEMBERS = {
    "germany", "france", "spain", "netherlands", "ireland", "poland", "portugal",
    "austria", "italy", "sweden", "denmark", "finland", "belgium", "czechia",
    "romania", "hungary", "greece", "estonia", "latvia", "lithuania", "bulgaria",
    "croatia", "slovakia", "slovenia", "cyprus", "malta", "luxembourg",
}
_EUROPE_MEMBERS = _EU_MEMBERS | {
    "norway", "serbia", "switzerland", "united kingdom", "ukraine", "iceland",
    "albania", "bosnia and herzegovina", "north macedonia", "montenegro", "kosovo",
    "moldova", "belarus",
}
_REGION_MEMBERS = {
    "eu": _EU_MEMBERS,
    "europe": _EUROPE_MEMBERS,
    # EMEA ⊇ Europe by definition. The old set named only twelve European
    # countries, so a Romanian or Czech user was outside "Remote (EMEA)".
    # Turkey and the Caucasus stay EMEA-only, as Turkey always was.
    "emea": _EUROPE_MEMBERS | {
        "turkey", "georgia", "armenia", "azerbaijan",
        "nigeria", "israel", "united arab emirates", "south africa", "kenya",
        "egypt", "saudi arabia", "qatar", "jordan", "lebanon", "morocco",
        "tunisia", "ghana",
    },
    "apac": {"india", "australia", "singapore", "japan", "philippines", "pakistan",
             "indonesia", "vietnam", "thailand", "malaysia", "south korea", "china",
             "taiwan", "hong kong", "new zealand", "bangladesh", "sri lanka", "nepal",
             "cambodia", "kazakhstan", "uzbekistan"},
    "latam": {"brazil", "mexico", "argentina", "chile", "colombia", "peru",
              "costa rica", "uruguay", "ecuador", "bolivia", "paraguay", "venezuela",
              "guatemala", "panama", "dominican republic", "el salvador", "honduras"},
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


# ISO-3166 alpha-2 codes → the canonical names above. Structured ATS fields
# (Lever `country`, Ashby `addressCountry`, SmartRecruiters `location.country`,
# Workable `country`) frequently carry the CODE rather than the name, and a
# code is not a token detect_country() can see — "SE" is not "sweden" to the
# name tier. Only countries the tables above know are listed; a code we cannot
# map resolves to "" (unknown), never to a guess.
ISO2_COUNTRIES = {
    "us": "united states", "gb": "united kingdom", "uk": "united kingdom",
    "ca": "canada", "in": "india", "de": "germany", "fr": "france", "es": "spain",
    "nl": "netherlands", "ie": "ireland", "au": "australia", "pl": "poland",
    "pt": "portugal", "br": "brazil", "mx": "mexico", "sg": "singapore",
    "jp": "japan", "ph": "philippines", "ua": "ukraine", "ng": "nigeria",
    "pk": "pakistan", "ar": "argentina", "ch": "switzerland", "at": "austria",
    "it": "italy", "se": "sweden", "no": "norway", "dk": "denmark", "fi": "finland",
    "be": "belgium", "cz": "czechia", "ro": "romania", "hu": "hungary",
    "gr": "greece", "tr": "turkey", "il": "israel", "ae": "united arab emirates",
    "za": "south africa", "ke": "kenya", "eg": "egypt", "id": "indonesia",
    "vn": "vietnam", "th": "thailand", "my": "malaysia", "kr": "south korea",
    "cn": "china", "tw": "taiwan", "hk": "hong kong", "nz": "new zealand",
    "cl": "chile", "co": "colombia", "pe": "peru", "cr": "costa rica",
    "uy": "uruguay", "ee": "estonia", "lv": "latvia", "lt": "lithuania",
    "bg": "bulgaria", "hr": "croatia", "rs": "serbia", "sk": "slovakia",
    "si": "slovenia", "al": "albania", "ba": "bosnia and herzegovina",
    "mk": "north macedonia", "me": "montenegro", "xk": "kosovo", "md": "moldova",
    "by": "belarus", "ge": "georgia", "am": "armenia", "az": "azerbaijan",
    "kz": "kazakhstan", "uz": "uzbekistan", "cy": "cyprus", "lu": "luxembourg",
    "is": "iceland", "bd": "bangladesh", "lk": "sri lanka", "np": "nepal",
    "sa": "saudi arabia", "qa": "qatar", "ma": "morocco", "tn": "tunisia",
    "gh": "ghana", "ec": "ecuador", "bo": "bolivia", "py": "paraguay",
    "ve": "venezuela", "gt": "guatemala", "do": "dominican republic",
    "sv": "el salvador", "hn": "honduras", "kh": "cambodia", "mt": "malta",
    "jo": "jordan", "lb": "lebanon", "pa": "panama", "pr": "united states",
    "gu": "united states",
}


def known_country(name: str) -> bool:
    """True when `name` (canonical lowercase) is a country the tables know."""
    return name == "united states" or name in _COUNTRY_NAMES or name in _COUNTRY_CITIES


def resolve_country_value(value) -> str:
    """Canonical country from a STRUCTURED field value — an ISO code ("SE"), a
    name ("Sweden"), an alias ("Deutschland") or a full string the ATS chose to
    put there. '' when it names nothing we know; never a guess."""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""
    low = v.lower().rstrip(".")
    if low in ISO2_COUNTRIES:
        return ISO2_COUNTRIES[low]
    n = norm_country(v)
    if known_country(n):
        return n
    return detect_country(v)


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
        "puerto rico": "united states", "guam": "united states",
        "macedonia": "north macedonia", "bosnia": "bosnia and herzegovina",
        "republic of georgia": "georgia", "ksa": "saudi arabia", "saudi": "saudi arabia",
        "maroc": "morocco", "luxemburg": "luxembourg",
        "republica dominicana": "dominican republic",
        "república dominicana": "dominican republic",
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


# Separators between the SITES of a multi-site posting ("Remote · Tiranë,
# Albania · Austin, TX", "Vilnius, Lithuania / Kaunas, Lithuania"). Commas are
# deliberately not one: they separate the parts of ONE address, and splitting
# "Toronto, ON, Canada" would lose exactly the country that names it.
_SITE_SPLIT = re.compile(r"\s*(?:·|\||;|/|\n)\s*")


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


def detect_us_state(text: str) -> str:
    """The US state (lowercase code) a place phrase names, or ''.

    Reads a spelled-out state name ("California", "New York") or a ``, XX``
    state code ("Bay Area, CA", "Austin, TX"). Meant for a phrase that already
    says it is a place — a residence restriction, a user's home location — so
    it may read "california" and "maine", which the free-text country
    detector leaves out. A code alone ("CA") is never enough: it is also
    Canada's ISO-2 and the way a hundred other things are abbreviated.
    """
    loc = " " + (text or "").lower().strip() + " "
    if not loc.strip():
        return ""
    for rx, code in _US_STATE_NAME_RES:
        if rx.search(loc):
            return code
    codes = [c for c in re.findall(r",\s*([a-z]{2})\b", loc) if c in _US_STATE_CODES]
    if codes:
        city = _match_country(loc, _CITY_RES)
        if not (city and _ISO2_US_STATE_COLLISIONS.get(city) in codes):
            return codes[-1]
    return ""


# What a posting calls the people of a country. A model asked to quote its
# evidence may be handed "must live and work within German borders" — a
# sentence that names no country the tables know, yet plainly names Germany.
# The quote validator accepts a claim only when the quote carries the country's
# name, one of its cities, or one of these.
COUNTRY_DEMONYMS = {
    "united states": ["american", "u.s.", "us-based", "us based", "usa", "stateside"],
    "united kingdom": ["british", "uk-based", "uk based", "english", "scottish", "welsh"],
    "canada": ["canadian"], "india": ["indian"], "germany": ["german"],
    "france": ["french"], "spain": ["spanish"], "netherlands": ["dutch"],
    "ireland": ["irish"], "australia": ["australian"], "poland": ["polish"],
    "portugal": ["portuguese"], "brazil": ["brazilian"], "mexico": ["mexican"],
    "singapore": ["singaporean"], "japan": ["japanese"], "philippines": ["filipino", "philippine"],
    "ukraine": ["ukrainian"], "nigeria": ["nigerian"], "pakistan": ["pakistani"],
    "argentina": ["argentine", "argentinian"], "switzerland": ["swiss"],
    "austria": ["austrian"], "italy": ["italian"], "sweden": ["swedish"],
    "norway": ["norwegian"], "denmark": ["danish"], "finland": ["finnish"],
    "belgium": ["belgian"], "czechia": ["czech"], "romania": ["romanian"],
    "hungary": ["hungarian"], "greece": ["greek"], "turkey": ["turkish"],
    "israel": ["israeli"], "united arab emirates": ["emirati"],
    "south africa": ["south african"], "kenya": ["kenyan"], "egypt": ["egyptian"],
    "indonesia": ["indonesian"], "vietnam": ["vietnamese"], "thailand": ["thai"],
    "malaysia": ["malaysian"], "south korea": ["korean"], "china": ["chinese"],
    "taiwan": ["taiwanese"], "new zealand": ["new zealander", "kiwi"],
    "chile": ["chilean"], "colombia": ["colombian"], "peru": ["peruvian"],
    "uruguay": ["uruguayan"], "estonia": ["estonian"], "latvia": ["latvian"],
    "lithuania": ["lithuanian"], "bulgaria": ["bulgarian"], "croatia": ["croatian"],
    "serbia": ["serbian"], "slovakia": ["slovak"], "slovenia": ["slovenian"],
    "albania": ["albanian"], "cyprus": ["cypriot"], "luxembourg": ["luxembourgish"],
    "iceland": ["icelandic"], "bangladesh": ["bangladeshi"], "sri lanka": ["sri lankan"],
    "saudi arabia": ["saudi"], "qatar": ["qatari"], "morocco": ["moroccan"],
    "ghana": ["ghanaian"], "ecuador": ["ecuadorian"], "costa rica": ["costa rican"],
}
_DEMONYM_RES = {c: _compile(toks) for c, toks in COUNTRY_DEMONYMS.items()}


def country_named_in(text: str, country: str) -> bool:
    """Does ``text`` itself name ``country`` (canonical lowercase) — by name,
    alias, ISO code in a ", XX" position, city, state, or demonym? The check
    behind "the quote must support the claim": a sentence about a comfortable
    office supports no country at all."""
    country = norm_country(country)
    if not country or not (text or "").strip():
        return False
    if detect_country(text) == country:
        return True
    if country == "united states" and detect_us_state(text):
        return True
    loc = " " + text.lower().strip() + " "
    if any(r.search(loc) for r in _DEMONYM_RES.get(country, ())):
        return True
    # A second country named beside the detected one ("US or Canada").
    for table in (_NAME_RES, _CITY_RES):
        if any(r.search(loc) for r in table.get(country, ())):
            return True
    return False


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
    # A multi-site posting is judged site by site. Read as one string, "Remote
    # · Tiranë, Albania · Austin, TX" is Albania — a foreign COUNTRY name is a
    # higher tier than the user's own ", TX" — yet the posting has a site in
    # the user's country. Any such site keeps it; none, and the whole-string
    # verdict below stands, remote or not.
    if _SITE_SPLIT.search(loc) and any(
            detect_country(site) == preferred
            for site in _SITE_SPLIT.split(loc) if site.strip()):
        return True
    region = detect_region(loc)
    if region and preferred not in _REGION_MEMBERS[region]:
        return False  # region-locked posting, user outside the region
    if remote_ok and (remote or "remote" in loc or "anywhere" in loc or "worldwide" in loc):
        return (not detected) or detected == preferred
    if not detected:
        return True  # ambiguous/unknown — keep rather than over-filter
    return detected == preferred
