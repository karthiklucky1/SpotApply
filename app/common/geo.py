"""Country detection from free-text job locations — the single source of truth.

Used by discovery (drop postings outside the user's preferred country), the
rule filter, and retrieval so every stage of the pipeline agrees on what
country a posting belongs to. Detection is intentionally conservative: when a
location is ambiguous/unknown we KEEP it rather than risk dropping good jobs.
"""
from __future__ import annotations

import re

_US_STATE_CODES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
}

# country -> signal tokens (lowercase). US handled separately via state codes too.
_COUNTRY_SIGNALS = {
    "united states": ["united states", "usa", "u.s.a", "u.s.", " us ", "america", "remote us", "us remote"],
    "united kingdom": ["united kingdom", " uk", "u.k", "england", "scotland", "wales",
                        "london", "manchester", "birmingham", "edinburgh", "glasgow", "bristol", "leeds"],
    "canada": ["canada", "ontario", "toronto", "vancouver", "montreal", "québec", "quebec",
               "ottawa", "calgary", "alberta", "british columbia", "winnipeg", "edmonton"],
    "india": ["india", "bangalore", "bengaluru", "hyderabad", "mumbai", "new delhi", "delhi",
              "pune", "chennai", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad"],
    "germany": ["germany", "deutschland", "berlin", "munich", "münchen", "frankfurt", "hamburg", "cologne"],
    "france": ["france", "paris", "lyon", "marseille", "toulouse", "bordeaux"],
    "spain": ["spain", "madrid", "barcelona", "valencia", "seville"],
    "netherlands": ["netherlands", "amsterdam", "rotterdam", "the hague", "utrecht"],
    "ireland": ["ireland", "dublin", "cork", "galway"],
    "australia": ["australia", "sydney", "melbourne", "brisbane", "perth", "canberra"],
    "poland": ["poland", "warsaw", "krakow", "kraków", "wroclaw", "gdansk"],
    "portugal": ["portugal", "lisbon", "porto"],
    "brazil": ["brazil", "brasil", "são paulo", "sao paulo", "rio de janeiro"],
    "mexico": ["mexico", "méxico", "mexico city", "guadalajara", "monterrey"],
    "singapore": ["singapore"],
    "japan": ["japan", "tokyo", "osaka"],
    "philippines": ["philippines", "manila", "cebu", "makati"],
    "ukraine": ["ukraine", "kyiv", "kiev", "lviv"],
    "nigeria": ["nigeria", "lagos", "abuja"],
    "pakistan": ["pakistan", "karachi", "lahore", "islamabad"],
    "argentina": ["argentina", "buenos aires"],
}


# Signals are matched with letter boundaries so "india" can't match "Indiana",
# "us" can't match "status", and "uk" can't match inside another word. Built
# once at import; "u.s." style signals keep working because the boundary is
# letter-based, not \b-based (a trailing "." has no word boundary before space).
_SIGNAL_RES = {
    country: [
        re.compile(rf"(?<![a-z]){re.escape(sig.strip())}(?![a-z])")
        for sig in signals
    ]
    for country, signals in _COUNTRY_SIGNALS.items()
}


def norm_country(name: str) -> str:
    """Normalize a country name/alias to its canonical lowercase form."""
    n = (name or "").strip().lower()
    aliases = {
        "us": "united states", "u.s.": "united states", "usa": "united states",
        "u.s.a": "united states", "america": "united states", "united states of america": "united states",
        "uk": "united kingdom", "u.k.": "united kingdom", "england": "united kingdom",
    }
    return aliases.get(n, n)


def detect_country(location: str) -> str:
    """Best-effort country guess from a free-text location. '' when unknown."""
    loc = " " + (location or "").lower().strip() + " "
    if not loc.strip():
        return ""
    # US: explicit signals or a trailing 2-letter state code (e.g. "Austin, TX").
    if any(r.search(loc) for r in _SIGNAL_RES["united states"]):
        return "united states"
    # only treat a 2-letter token as a state if it looks like "city, XX"
    if re.search(r",\s*[a-z]{2}\b", loc) and any(t in _US_STATE_CODES for t in re.findall(r",\s*([a-z]{2})\b", loc)):
        return "united states"
    # Foreign countries.
    for country, sig_res in _SIGNAL_RES.items():
        if country == "united states":
            continue
        if any(r.search(loc) for r in sig_res):
            return country
    return ""


def location_allowed(location: str, remote: bool, preferred_country: str, remote_ok: bool) -> bool:
    """True if a posting should be kept for a user targeting `preferred_country`."""
    loc = (location or "").lower()
    # Remote-friendly users keep any remote/anywhere posting regardless of country.
    if remote_ok and (remote or "remote" in loc or "anywhere" in loc or "worldwide" in loc):
        return True
    detected = detect_country(loc)
    if not detected:
        return True  # ambiguous/unknown — keep rather than over-filter
    return detected == norm_country(preferred_country)
