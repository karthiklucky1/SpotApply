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

Pure: no database, no network, no LLM. `Geography` is the shape of
`JobGeography` (app/db/models.py) and `GeoPrefs` is read off a UserProfile by
`app.common.tenant_prefs.geo_prefs`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from app.common.geo import _REGION_MEMBERS, _US_STATE_CODES, norm_country

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


@dataclass(frozen=True)
class GeoPrefs:
    """The saved preferences the decision reads. All canonical/lowercase."""
    country: str = ""                  # norm_country() of preferred_country; "" = no gate
    remote_ok: bool = True
    open_to_relocation: bool = False
    home_location: str = ""            # UserProfile.location, verbatim ("Cincinnati, OH")


@dataclass
class Geography:
    """The posting's geography as established for everyone (see JobGeography)."""
    status: str = STATUS_UNKNOWN
    countries: List[str] = field(default_factory=list)       # canonical lowercase
    sites: List[str] = field(default_factory=list)           # every site, untruncated
    work_mode: str = ""                                      # remote | hybrid | onsite | ""
    remote_regions: List[str] = field(default_factory=list)  # country names, REGION_KEYS, WORLDWIDE
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
    """('cincinnati', 'oh') from 'Cincinnati, OH'; either may be ''."""
    parts = [p.strip().lower() for p in (home_location or "").split(",") if p.strip()]
    if not parts:
        return "", ""
    city = parts[0]
    state = ""
    for p in parts[1:]:
        if len(p) == 2 and p in _US_STATE_CODES:
            state = p
            break
    # A one-part home that is itself a state code ("OH").
    if not state and len(city) == 2 and city in _US_STATE_CODES:
        return "", city
    return city, state


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

    # Country check passed. Now the user's actual local-location preference:
    # an on-site or hybrid role outside their home area, when they will not
    # relocate, is not a job they can take — whatever the fit score says.
    if geo.work_mode in ("onsite", "hybrid") and not prefs.open_to_relocation:
        local_sites = [s for s in geo.sites if "remote" not in s.lower()]
        near = home_area_matches(local_sites or geo.sites, prefs.home_location)
        if near is False:
            where = _fmt_places([s for s in (local_sites or geo.sites)][:2]) if (local_sites or geo.sites) else "another city"
            return Decision(INELIGIBLE, "onsite_outside_home_area",
                            f"{geo.work_mode.capitalize()} in {where}; you are not open to relocation")
        if near is True:
            return Decision(ELIGIBLE, "onsite_in_home_area",
                            f"{geo.work_mode.capitalize()} in your area")

    if region_ok and not site_ok:
        return Decision(ELIGIBLE, "remote_region_match",
                        f"Remote role open to {_fmt_places(regions)}")
    if geo.work_mode == "remote":
        return Decision(ELIGIBLE, "remote_in_country",
                        f"Remote role in {_titled(country)}")
    return Decision(ELIGIBLE, "country_match",
                    f"Located in {_titled(country)}"
                    + ("" if geo.work_mode else "; work mode not stated"))
