"""ONE location-eligibility decision, applied at every door.

The gate used to be a string comparison (`geo.location_allowed`) that each
door called with whatever it happened to have in hand, and the scorer saw the
posting a third way. This module is the decision every door now shares:

    posting geography (shared, verified once)  ×  user's saved preferences
                                    ↓
              ELIGIBLE  |  INELIGIBLE  |  UNKNOWN   + an evidence-based reason

Three rules the decision is built on:

  * **Evidence, never inference.** "Remote" and "Homeoffice" do not establish a
    country. A department, a board's home country, a company HQ or a salary
    currency are not location evidence at all. A posting with no evidence is
    UNKNOWN — it is never assigned a country to make a verdict possible.
  * **Remote is not borderless.** A remote role anchored to another country,
    or restricted to a region the user is outside, is INELIGIBLE. A remote
    role with an explicit restriction the user satisfies ("US only" for a US
    user) is ELIGIBLE on that restriction.
  * **Country is not the whole answer.** Passing the country check does not
    establish full eligibility: sponsorship / work authorization stay the
    separate checks they already are (rule_filter, sponsorship intelligence),
    and an on-site or hybrid role outside the user's home area is INELIGIBLE
    when they have said they will not relocate.
  * **Narrower than a country is narrower.** "Candidates must be based in
    California" is a restriction on the STATE, not a nationwide US role, and
    is kept as one (`Geography.areas`). A user whose home state is known is
    judged against it; one whose home state is not known is HELD — the
    decision never infers nationwide eligibility from a statewide rule.
  * **The user's remote preference is a preference.** With "Include remote
    roles" off, a remote role is INELIGIBLE unless it also offers an office in
    the user's own area.

Pure: no database, no network, no LLM. `Geography` is the shape of
`JobGeography` (app/db/models.py) and `GeoPrefs` is read off a UserProfile by
`app.common.tenant_prefs.geo_prefs`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from app.common.geo import (
    _REGION_MEMBERS, _US_STATE_CODES, US_STATE_LABELS, detect_us_state, norm_country,
)

ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"
UNKNOWN = "unknown"

# Geography.status values — the same strings as JobGeographyStatus, kept as
# constants so this module stays importable without SQLModel.
RESOLVED = "resolved"
STATUS_UNKNOWN = "unknown"
CONFLICT = "conflict"

# Remote restrictions that mean "no restriction".
WORLDWIDE = "worldwide"

# Region keys that `remote_regions` may carry beside country names.
REGION_KEYS = tuple(_REGION_MEMBERS.keys())


#: Bumped whenever `decide()` can return a different answer for the same
#: inputs. Recorded with every delivery (slate placement events) so a verdict
#: can always be traced to the rules that produced it, and so a stamp written
#: by older rules is recognisably older.
#:   1 — the 2026-09-18 evidence rules
#:   2 — 2026-09-26 audit: relocation DESTINATIONS bind (not just the flag); a
#:       known on-site/hybrid role with no home location on file is held; a
#:       hybrid/on-site role with no physical site is held, never "not near".
RULES_VERSION = 2


@dataclass(frozen=True)
class GeoPrefs:
    """The saved preferences the decision reads. All canonical/lowercase."""
    country: str = ""                  # norm_country() of preferred_country; "" = no gate
    remote_ok: bool = True
    open_to_relocation: bool = False
    home_location: str = ""            # UserProfile.location, verbatim ("Cincinnati, OH")
    # Where they will move to — `relocation.parse_targets` of
    # UserProfile.relocation_targets. () = not stated (read as anywhere in the
    # searched country, the meaning `open_to_relocation` always had);
    # ("nationwide",) = anywhere, said explicitly; otherwise ONLY these
    # cities/states. Willingness to move to Chicago is not willingness to move
    # to Dallas (audit 2026-09-25, finding 3).
    relocation_targets: tuple = ()

    def relocation_covers(self, sites: Iterable[str]) -> bool:
        """Would this user move to (one of) these physical sites?"""
        if not self.open_to_relocation:
            return False
        targets = list(self.relocation_targets or ())
        if not targets:
            return True
        from app.tailoring.relocation import NATIONWIDE, _approved
        if NATIONWIDE in targets:
            return True
        return any(_approved(s, targets) for s in sites if s)


@dataclass
class Geography:
    """The posting's geography as established for everyone (see JobGeography)."""
    status: str = STATUS_UNKNOWN
    countries: List[str] = field(default_factory=list)       # canonical lowercase
    sites: List[str] = field(default_factory=list)           # every site, untruncated
    work_mode: str = ""                                      # remote | hybrid | onsite | ""
    remote_regions: List[str] = field(default_factory=list)  # country names, REGION_KEYS, WORLDWIDE
    # Sub-national residence restrictions, "<country>/<state>[/<city>]" —
    # "united states/ca", "united states/tx/austin". Narrower than the country
    # the same restriction also names in `remote_regions`.
    areas: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    evidence_source: str = "none"
    evidence_field: str = ""
    evidence_quote: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == RESOLVED and bool(self.countries or self.remote_regions)


@dataclass(frozen=True)
class Decision:
    status: str          # ELIGIBLE | INELIGIBLE | UNKNOWN
    code: str            # machine-readable reason
    reason: str          # the sentence a user reads

    @property
    def ineligible(self) -> bool:
        return self.status == INELIGIBLE

    @property
    def unknown(self) -> bool:
        return self.status == UNKNOWN


def _titled(name: str) -> str:
    return " ".join(w.capitalize() if len(w) > 2 else w.upper() for w in name.split()) \
        if name in ("uk", "usa") else name.title()


def _fmt_places(names: Iterable[str]) -> str:
    """Country names and region keys are canonical lowercase and get cased for
    display; a site string ("San Francisco, CA") is shown as the source wrote it."""
    from app.common.geo import known_country
    seen: list = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    labels = [n.upper() if n in REGION_KEYS else _titled(n) if (known_country(n) or n == WORLDWIDE) else n
              for n in seen]
    return ", ".join(labels[:3]) + (" +more" if len(labels) > 3 else "")


def _in_regions(country: str, regions: Iterable[str]) -> bool:
    """Is `country` admitted by an explicit remote restriction list?"""
    for r in regions:
        if r == WORLDWIDE or r == country:
            return True
        if r in _REGION_MEMBERS and country in _REGION_MEMBERS[r]:
            return True
    return False


_STATE_CODE_RE = re.compile(r",\s*([a-z]{2})\b")


def _home_parts(home_location: str) -> tuple[str, str]:
    """('cincinnati', 'oh') from 'Cincinnati, OH' (or 'Cincinnati, Ohio');
    either may be ''."""
    parts = [p.strip().lower() for p in (home_location or "").split(",") if p.strip()]
    if not parts:
        return "", ""
    city = parts[0]
    state = ""
    for p in parts[1:]:
        if len(p) == 2 and p in _US_STATE_CODES:
            state = p
            break
        code = detect_us_state(p)
        if code:
            state = code
            break
    # A one-part home that is itself a state ("OH", "Ohio").
    if not state and len(parts) == 1:
        if len(city) == 2 and city in _US_STATE_CODES:
            return "", city
        code = detect_us_state(city)
        if code and city in (US_STATE_LABELS.get(code, "").lower(),):
            return "", code
    return city, state


def area_token(country: str, state: str = "", city: str = "") -> str:
    """Canonical form of one sub-national restriction ("united states/ca",
    "united states/tx/austin")."""
    parts = [norm_country(country), (state or "").lower()]
    if city:
        parts.append(city.strip().lower())
    return "/".join(p for p in parts if p)


def _area_parts(token: str) -> tuple[str, str, str]:
    p = (token or "").split("/") + ["", "", ""]
    return p[0], p[1], p[2]


def _fmt_areas(areas: Iterable[str]) -> str:
    out: list = []
    for a in areas:
        _c, state, city = _area_parts(a)
        label = US_STATE_LABELS.get(state, state.upper())
        label = f"{city.title()}, {state.upper()}" if city else label
        if label and label not in out:
            out.append(label)
    return ", ".join(out[:3]) + (" +more" if len(out) > 3 else "")


def _area_check(areas: List[str], home_location: str) -> tuple[Optional[bool], str]:
    """Does the user's home satisfy at least one area restriction?

    True/False when it can be decided; None when the profile does not say
    enough (no state, or a city-level restriction and no city). The second
    value names what the user is in, for the sentence they read."""
    city, state = _home_parts(home_location)
    if not state:
        return None, ""
    where = f"{city.title()}, {state.upper()}" if city else US_STATE_LABELS.get(state, state.upper())
    undecided = False
    for a in areas:
        _c, a_state, a_city = _area_parts(a)
        if a_state != state:
            continue
        if not a_city:
            return True, where
        if not city:
            undecided = True
            continue
        if a_city == city:
            return True, where
    return (None if undecided else False), where


def _site_in_home_area(site: str, city: str, state: str) -> bool:
    s = " " + (site or "").lower() + " "
    if city and re.search(rf"(?<![a-z]){re.escape(city)}(?![a-z])", s):
        return True
    if not city and state:
        return state in {m for m in _STATE_CODE_RE.findall(s) if m in _US_STATE_CODES}
    return False


def home_area_matches(sites: Iterable[str], home_location: str) -> Optional[bool]:
    """True/False when the posting's sites can be compared with the user's home
    area; None when the user has no usable home location (no constraint)."""
    city, state = _home_parts(home_location)
    if not city and not state:
        return None
    sites = [s for s in sites if s]
    if not sites:
        return None
    return any(_site_in_home_area(s, city, state) for s in sites)


def _is_bare_country(site: str) -> bool:
    """A site that is only a country name ("United States", "Germany")."""
    from app.common.geo import _COUNTRY_NAMES
    c = norm_country(site or "")
    return c == "united states" or c in _COUNTRY_NAMES


def _specific_sites(sites: List[str]) -> List[str]:
    """Sites more specific than a whole country, for the sentence a user reads
    ("Hybrid in San Francisco", not "Hybrid in United States, San Francisco").
    Falls back to every site when none is narrower."""
    narrow = [s for s in sites if not _is_bare_country(s)]
    return narrow or list(sites)


def decide(geo: Optional[Geography], prefs: GeoPrefs) -> Decision:
    """The decision. Deterministic; the same inputs give the same answer at
    intake, adoption, retrieval, scoring and delivery."""
    country = norm_country(prefs.country or "")
    if not country:
        # No country preference at all: no country gate anywhere (the user has
        # not told us where they are; assuming is the expensive mistake).
        return Decision(ELIGIBLE, "no_country_preference", "No country preference saved")

    if geo is None or geo.status == STATUS_UNKNOWN or not (geo.countries or geo.remote_regions):
        if geo is not None and geo.status == CONFLICT:
            return Decision(UNKNOWN, "conflicting_location_evidence",
                            "Location evidence conflicts: " + ("; ".join(geo.conflicts)[:140]
                                                              or "sources disagree"))
        if geo is not None and (geo.work_mode == "remote" or any(
                "remote" in s.lower() or "homeoffice" in s.lower().replace(" ", "")
                for s in geo.sites)):
            return Decision(UNKNOWN, "remote_no_country",
                            "Remote posting with no stated country or region — pending verification")
        return Decision(UNKNOWN, "no_location_evidence",
                        "The posting states no location — pending verification")

    if geo.status == CONFLICT:
        return Decision(UNKNOWN, "conflicting_location_evidence",
                        "Location evidence conflicts: " + ("; ".join(geo.conflicts)[:140]
                                                          or "sources disagree"))

    countries = [norm_country(c) for c in geo.countries if c]
    regions = list(geo.remote_regions)
    site_ok = country in countries
    region_ok = bool(regions) and _in_regions(country, regions)

    if regions and not region_ok:
        # An explicit remote restriction the user is outside. A site in the
        # user's own country still admits an on-site/hybrid role there — the
        # restriction describes the remote track, not the office.
        if site_ok and geo.work_mode in ("onsite", "hybrid"):
            pass
        else:
            return Decision(INELIGIBLE, "remote_region_excluded",
                            f"Remote role restricted to {_fmt_places(regions)}; "
                            f"you are searching in {_titled(country)}")
    elif not site_ok and not region_ok:
        return Decision(INELIGIBLE, "country_mismatch",
                        f"Located in {_fmt_places(countries)}; "
                        f"you are searching in {_titled(country)}")

    # Country check passed. The sites that are places, not a way of working —
    # and narrower than a whole country: "United States" as a site says where
    # the job is legally, not where anyone has to commute to.
    physical = [s for s in geo.sites if not _is_remote_site(s) and not _is_bare_country(s)]
    # Will they move to where this job is? The flag alone used to answer it, so
    # "relocate to Chicago only" admitted an on-site role in Dallas.
    will_move = prefs.relocation_covers(physical)

    # The user's remote preference ("Include remote roles" off): a remote role
    # is not a job they asked for, unless it also offers an office in their
    # own area — then it is that office.
    if not prefs.remote_ok and geo.work_mode == "remote":
        near = home_area_matches(physical, prefs.home_location) if physical else None
        if near is not True:
            return Decision(INELIGIBLE, "remote_not_wanted",
                            "Remote role; your profile keeps remote roles out of your search")

    # A restriction narrower than the country ("must be based in California").
    # It binds a remote role outright, and an on-site one for a user who will
    # not relocate; a user who will relocate to the office is not held to a
    # residence rule they would satisfy by taking the job. A home the profile
    # does not place well enough to compare is HELD — never read as "anywhere
    # in the country".
    applicable = [a for a in geo.areas if _area_parts(a)[0] == country]
    if applicable and not (physical and will_move):
        ok, where = _area_check(applicable, prefs.home_location)
        if ok is None:
            return Decision(UNKNOWN, "area_restriction_unresolved",
                            f"Restricted to {_fmt_areas(applicable)} residents — add your city "
                            f"and state to your profile to confirm")
        if ok is False:
            return Decision(INELIGIBLE, "area_restriction_excluded",
                            f"Restricted to {_fmt_areas(applicable)} residents; you are in {where}")
        area_note = f"; restricted to {_fmt_areas(applicable)} residents, which you are"
    else:
        area_note = ""

    # The user's actual local-location preference: an on-site or hybrid role
    # outside their home area, when they will not relocate, is not a job they
    # can take — whatever the fit score says.
    if geo.work_mode in ("onsite", "hybrid") and not will_move:
        mode = geo.work_mode.capitalize()
        if not physical:
            # An attendance requirement with no place we can read ("3 days a
            # week in the office", structured site "Remote"). Comparing
            # "Remote" with a home city answered "not near" and so INELIGIBLE —
            # a guess. The office is the missing fact; hold for it.
            return Decision(UNKNOWN, "attendance_place_unresolved",
                            f"{mode} role, but the posting does not say where the office is "
                            f"— pending verification" + area_note)
        near = home_area_matches(physical, prefs.home_location)
        where = _fmt_places(_specific_sites(physical)[:2])
        if near is False:
            if prefs.open_to_relocation:
                return Decision(INELIGIBLE, "relocation_target_excluded",
                                f"{mode} in {where}, which is not one of the places you "
                                f"will relocate to")
            return Decision(INELIGIBLE, "onsite_outside_home_area",
                            f"{mode} in {where}; you are not open to relocation")
        if near is True:
            return Decision(ELIGIBLE, "onsite_in_home_area",
                            f"{mode} in your area" + area_note)
        # near is None: no home location on file. A commute is required and we
        # cannot tell whether this one is feasible — the old answer was
        # ELIGIBLE, which delivered a Dallas office job to a profile with no
        # city and relocation off. Ask for the city instead of guessing.
        return Decision(UNKNOWN, "home_location_missing",
                        f"{mode} in {where} — add your city and state to your profile "
                        f"so we can tell whether it is within reach" + area_note)

    if not prefs.remote_ok and geo.work_mode == "remote":
        return Decision(ELIGIBLE, "office_in_home_area",
                        f"Remote role with an office in your area ({_fmt_places(physical[:2])})" + area_note)
    if region_ok and not site_ok:
        return Decision(ELIGIBLE, "remote_region_match",
                        f"Remote role open to {_fmt_places(regions)}" + area_note)
    if geo.work_mode == "remote":
        return Decision(ELIGIBLE, "remote_in_country",
                        f"Remote role in {_titled(country)}" + area_note)

    # ── WORK MODE UNKNOWN, on a posting with a real place in it ──────────────
    # This branch used to fall straight through to ELIGIBLE with the words
    # "work mode not stated" appended, and Workday populates no work mode at
    # all. Measured on one production board, 2026-09-25: four of five delivered
    # jobs were on-site or hybrid roles in cities the user's profile said they
    # would not move to, and all four read `eligible` for exactly this reason.
    #
    # We genuinely do not know whether a commute applies — the posting may be
    # remote. So the answer is neither ELIGIBLE (the old guess, in the
    # permissive direction) nor INELIGIBLE (a guess in the other): it is
    # UNKNOWN, which HOLDS the job and hands it to `geo_verify.verify_pending`
    # to resolve from the ATS detail or the description. The missing fact is
    # necessary to establish eligibility, which is precisely what holding is
    # for.
    #
    # Only when it MATTERS. A site the user already lives near is fine on
    # either work mode, and a user open to relocation is not blocked by a
    # commute they would move for — both of those stay eligible, so this holds
    # no job whose answer we can already infer.
    # ONLY when the home area is KNOWN and the site is demonstrably not near it.
    # A home the profile does not place (`near is None`) falls through to
    # eligible on purpose: the user has not given us the fact that would make
    # the commute question answerable, and holding their entire board over a
    # missing city is disproportionate — it would empty every board belonging to
    # a user who never typed one. The narrower ask already exists for the case
    # that justifies it, where the POSTING asserts a residence restriction
    # (`area_restriction_unresolved`, which does prompt for the city).
    # A bare country counts as a place HERE (but not for relocation or the
    # home-area match above): "United States" with no work mode is exactly the
    # Workday shape this branch exists for, and treating it as "no place"
    # admitted it for a user who will not move (match review, 2026-09-26).
    placed = physical or [s for s in geo.sites if not _is_remote_site(s)]
    if not geo.work_mode and placed and not will_move:
        if home_area_matches(placed, prefs.home_location) is False:
            where = _fmt_places(placed[:2])
            return Decision(UNKNOWN, "work_mode_unresolved",
                            f"Located in {where}, which is not your area, and the posting "
                            f"does not say whether it is on-site or remote — pending "
                            f"verification" + area_note)

    return Decision(ELIGIBLE, "country_match",
                    f"Located in {_titled(country)}"
                    + ("" if geo.work_mode else "; work mode not stated") + area_note)


def is_remote_site(site: str) -> bool:
    s = (site or "").lower().replace(" ", "")
    return "remote" in s or "homeoffice" in s or "anywhere" in s or "worldwide" in s


# `_is_remote_site` was private but geo_verify needs the same question when it
# decides whether a posting has a real place in it, and the two must agree.
_is_remote_site = is_remote_site
