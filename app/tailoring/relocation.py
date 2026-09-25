"""An opt-in "Open to relocation to <place>" line for U.S. postings.

WHAT THIS IS NOT. It is not a change of address. The candidate's real city stays
exactly where the master résumé puts it; this adds a separate willingness
statement and nothing else. Writing "Chicago, IL" into the header of someone who
lives in Cincinnati is a factual misstatement on a document an employer relies
on, and no amount of relocation willingness makes it true.

FOUR SEPARATE FACTS, kept separate:

    where they live now        UserProfile.location            never rewritten
    will they move at all     UserProfile.open_to_relocation   a SEARCH filter
    may résumés say so        UserProfile.relocation_resume_optin
    where, and by when        relocation_targets / relocation_timeline

`open_to_relocation` already existed and already drives which jobs are eligible
(`eligibility.decide`). It is NOT consent to print a relocation line on a
document sent to an employer, so it deliberately does not enable this one: the
new flag defaults OFF for everybody, including users whose search preference is
already "yes". Consent to be shown a job is not consent to make a claim.

SPECIFICITY COMES FROM THE POSTING, never from us. A posting that names
"Chicago, IL" yields "Open to relocation to Chicago, IL"; one that names only
Kentucky yields "Open to relocation to Kentucky". We never promote a state to a
city, never guess a city from a state, and never emit a line at all for a
posting with no U.S. destination in it — there is nothing to relocate to on a
fully remote role.

U.S. ONLY. Non-U.S. postings get no line regardless of the setting: the wording,
the approved-destination list and the timeline are all framed around domestic
moves, and a work-authorisation question sits behind an international one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.common.geo import (US_STATE_LABELS, _US_STATE_CODES, detect_us_state,
                            norm_country)

# The literal a user picks instead of listing places.
NATIONWIDE = "nationwide"
_NATIONWIDE_WORDS = frozenset({
    NATIONWIDE, "anywhere", "any", "us", "usa", "united states",
    "anywhere in the us", "anywhere in the united states",
})

_US = "united states"


@dataclass(frozen=True)
class RelocationOffer:
    """One resolved willingness statement, and why it resolved that way."""
    line: str                # "Open to relocation to Chicago, IL" — or ""
    destination: str         # what the posting verified: "Chicago, IL" / "Kentucky"
    reason: str              # machine-readable: why it is or is not offered

    def __bool__(self) -> bool:
        return bool(self.line)


def _clean(value) -> str:
    return (value or "").strip()


def parse_targets(raw) -> list[str]:
    """The approved-destination list, normalised.

    Accepts a comma or newline separated string, or a list. "Nationwide"
    anywhere in it means the whole country and short-circuits the rest.
    """
    if isinstance(raw, (list, tuple, set)):
        items = [_clean(str(x)).lower() for x in raw]
    else:
        items = [_clean(t).lower()
                 for t in str(raw or "").replace("\n", ",").replace(";", ",").split(",")]
    items = [t for t in items if t]

    # REJOIN "city, state". Splitting on commas alone turns the single target
    # "Chicago, IL" into "chicago" AND "il" — and a bare state target approves
    # every city in it, so asking for one city would silently approve the whole
    # state. A fragment that is only a state, following one that is not, belongs
    # to the fragment before it.
    merged: list[str] = []
    skip = False
    for i, t in enumerate(items):
        if skip:
            skip = False
            continue
        nxt = items[i + 1] if i + 1 < len(items) else ""
        # "nationwide" is never half of a city/state pair — merging it would
        # produce "nationwide, texas" and lose the whole-country meaning.
        if (nxt and _is_bare_state(nxt) and not _is_bare_state(t)
                and t not in _NATIONWIDE_WORDS and nxt not in _NATIONWIDE_WORDS):
            merged.append(f"{t}, {nxt}")
            skip = True
        else:
            merged.append(t)

    out: list[str] = []
    for t in merged:
        if t in _NATIONWIDE_WORDS:
            return [NATIONWIDE]
        if t not in out:
            out.append(t)
    return out


def _is_bare_state(token: str) -> bool:
    """Is this fragment a state on its own ("il", "illinois") and nothing else?"""
    t = _clean(token).lower()
    if not t or "," in t:
        return False
    if len(t) == 2 and t in _US_STATE_CODES:
        return True
    code = detect_us_state(t)
    return bool(code) and t == US_STATE_LABELS.get(code, "").lower()


def _site_parts(site: str) -> tuple[str, str]:
    """('chicago', 'il') from a posting site string; either may be ''.

    Mirrors `eligibility._home_parts` on purpose — the same string shape is
    parsed the same way on both sides of the comparison, or a destination could
    be "approved" against a home the eligibility gate read differently.
    """
    parts = [p.strip().lower() for p in (site or "").split(",") if p.strip()]
    if not parts:
        return "", ""
    state = ""
    for p in parts[1:]:
        # The bare two-letter code FIRST: `detect_us_state` matches state NAMES
        # and returns "" for "il"/"tx", so without this branch "Chicago, IL"
        # parses as a city with no state and no state target can ever approve
        # it. `_home_parts` has the same branch for the same reason.
        if len(p) == 2 and p in _US_STATE_CODES:
            state = p
            break
        code = detect_us_state(p)
        if code:
            state = code
            break
    city = parts[0]
    # A single part that is itself a state ("Kentucky", "KY") is a state, not a
    # city — this is what keeps "Open to relocation to Kentucky" from becoming a
    # claim about a city we invented.
    if len(parts) == 1:
        if len(city) == 2 and city in _US_STATE_CODES:
            return "", city
        code = detect_us_state(city)
        if code and city in (US_STATE_LABELS.get(code, "").lower(), code):
            return "", code
    return city, state


def _display(site: str) -> str:
    """The destination exactly as specific as the posting made it."""
    city, state = _site_parts(site)
    if city and state:
        return f"{city.title()}, {state.upper()}"
    if state:
        return US_STATE_LABELS.get(state, state.upper())
    return _clean(site)


def _approved(site: str, targets: list[str]) -> bool:
    if NATIONWIDE in targets:
        return True
    city, state = _site_parts(site)
    state_label = US_STATE_LABELS.get(state, "").lower() if state else ""
    for t in targets:
        tc, ts = _site_parts(t)
        # A state target approves every city in it; a city target approves only
        # that city.
        if ts and not tc:
            if state and ts == state:
                return True
            continue
        if tc and city and tc == city and (not ts or not state or ts == state):
            return True
        if t in (state, state_label) and state:
            return True
    return False


def _us_destinations(sites: Iterable[str]) -> list[str]:
    """Physical U.S. places in the posting — not ways of working."""
    from app.common.eligibility import is_remote_site
    out: list[str] = []
    for s in sites or []:
        s = _clean(s)
        if not s or is_remote_site(s):
            continue
        if s not in out:
            out.append(s)
    return out


def offer_for(profile, geography, *, job_country: str = "") -> RelocationOffer:
    """Should this posting's résumé carry a relocation line, and which one?

    ``geography`` is the VERIFIED evidence (`eligibility.Geography`), not a raw
    location string, so the destination on the document is one the posting
    actually established.
    """
    none = lambda why: RelocationOffer("", "", why)  # noqa: E731

    if not bool(getattr(profile, "relocation_resume_optin", False)):
        return none("optin_off")
    # The search preference is a separate consent. If they will not move at all,
    # a willingness line would contradict the profile.
    if not bool(getattr(profile, "open_to_relocation", False)):
        return none("not_open_to_relocation")

    country = norm_country(job_country or "")
    if not country and geography is not None:
        cs = [norm_country(c) for c in (getattr(geography, "countries", None) or [])]
        country = cs[0] if len(set(cs)) == 1 else ""
    if country != _US:
        return none("not_a_us_posting")

    sites = list(getattr(geography, "sites", None) or []) if geography is not None else []
    destinations = _us_destinations(sites)
    if not destinations:
        # Remote with no office, or a posting that named no place: there is
        # nothing to relocate TO, so the line would be noise at best.
        return none("no_us_destination_in_posting")

    targets = parse_targets(getattr(profile, "relocation_targets", ""))
    if not targets:
        return none("no_approved_destinations_set")

    for site in destinations:
        if _approved(site, targets):
            shown = _display(site)
            if not shown:
                continue
            line = f"Open to relocation to {shown}"
            timeline = _clean(getattr(profile, "relocation_timeline", ""))
            if timeline:
                line = f"{line} ({timeline})"
            return RelocationOffer(line, shown, "approved_destination")
    return RelocationOffer("", _display(destinations[0]), "destination_not_approved")


def prompt_block(offer: Optional[RelocationOffer]) -> str:
    """The instruction handed to the tailor, or "" when there is no offer.

    Spelled out rather than left to inference, because the failure mode is a
    document that misstates where someone lives.
    """
    if not offer or not offer.line:
        return ""
    return (
        "\n\nRELOCATION WILLINGNESS — the candidate has opted in to stating this "
        f"for U.S. roles in approved destinations. Add exactly this line, once, in "
        f"the contact/header area or the summary:\n"
        f"  {offer.line}\n"
        "Rules: do NOT change the candidate's current city, state or contact "
        "details anywhere in the document — they still live where the master "
        "résumé says they live, and this is a separate statement of willingness. "
        "Do not reword the line, do not add a second relocation sentence, and do "
        "not claim any destination other than the one above."
    )
