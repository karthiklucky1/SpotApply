import logging
import re
from pathlib import Path
import yaml
from app.db.models import Job

log = logging.getLogger(__name__)

# Phrasings of the future-sponsorship knockout question. This is deliberately a
# module-level constant so the primary branch and the yes/no fallback in
# resolve() can never drift apart and leave one path auto-answering it.
_SPONSORSHIP_KWS = (
    "require sponsorship",
    "requires sponsorship",
    "sponsorship now or in the future",
    "visa sponsorship",
    "sponsorship in the future",
    "require visa",
    "sponsorship requirements",
    "need sponsorship",
    "immigration sponsorship",
    "employment visa status",
    "require employer sponsorship",
)

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"
}

class QAResolver:
    def __init__(self, yaml_path: str | Path | None = None):
        if yaml_path is None:
            # Default path relative to this file
            yaml_path = Path(__file__).parent / "answers.yaml"
        self.yaml_path = Path(yaml_path)
        self.data = {}
        self.load_answers()

    def load_answers(self):
        try:
            if self.yaml_path.exists():
                with open(self.yaml_path, "r", encoding="utf-8") as f:
                    self.data = yaml.safe_load(f) or {}
                log.info("Loaded canonical answers from %s", self.yaml_path)
            else:
                log.warning("answers.yaml not found at %s. Using empty dict.", self.yaml_path)
                self.data = {}
        except Exception as e:
            log.error("Failed to load answers.yaml: %s", e)
            self.data = {}

    def _get_nested(self, keys: list[str], default=None):
        curr = self.data
        for k in keys:
            if isinstance(curr, dict) and k in curr:
                curr = curr[k]
            else:
                return default
        return curr

    def _stored(self, keys: list[str]) -> tuple[str | None, float]:
        """Store value → (value, 0.95); missing/empty → (None, 0.0) so the
        caller routes the question to the human. NEVER a hardcoded person as
        the fallback: these defaults used to be the founder's own identity,
        which any deployment (or code path) missing a store key would have
        typed into a stranger's application form."""
        v = self._get_nested(keys, None)
        if v is None or (isinstance(v, str) and not v.strip()):
            return None, 0.0
        return v, 0.95

    def _get_state_from_location(self, location: str | None) -> str | None:
        if not location:
            return None
        match = re.search(r'\b([A-Z]{2})\b', location)
        if match:
            state_code = match.group(1)
            if state_code in US_STATES:
                return US_STATES[state_code]
        for name in US_STATES.values():
            if name.lower() in location.lower():
                return name
        return None

    def resolve(self, question_text: str, job: Job | None = None) -> tuple[str | None, float]:
        """Resolve a screening question using answers.yaml.
        
        Returns:
            (answer_text, confidence)
            If confidence < 0.7, the caller should ask the user.
        """
        low = question_text.lower().strip()

        # 0. Check human routing blocklist
        always_ask = self.data.get("always_ask_human", [])
        if any(pattern.lower() in low for pattern in always_ask):
            log.info("QAResolver: Human route match for '%s'", question_text)
            return None, 0.0

        # 1. Social links & Identity — STORE-ONLY. Every branch here used to
        # carry the founder's real PII as its literal default, so a missing
        # store key answered identity questions AS the founder for whoever's
        # form was being filled. No stored value → route to the human; an
        # identity is never guessed.
        if "linkedin" in low:
            return self._stored(["identity", "linkedin"])
        if "github" in low:
            return self._stored(["identity", "github"])
        if "website" in low or "portfolio" in low:
            return self._stored(["identity", "github"])
        if "preferred name" in low or "preferred  name" in low:
            return self._stored(["identity", "first_name"])
        if "full name" in low:
            first = self._get_nested(["identity", "first_name"], None)
            last = self._get_nested(["identity", "last_name"], None)
            if not first:
                return None, 0.0
            return f"{first} {last or ''}".strip(), 0.95
        if "first name" in low:
            return self._stored(["identity", "first_name"])
        if "last name" in low:
            return self._stored(["identity", "last_name"])
        if "email" in low:
            return self._stored(["identity", "email"])
        if "phone" in low or "mobile" in low:
            return self._stored(["identity", "phone"])

        # 2. Work Authorization
        #
        # Sponsorship is checked FIRST, and on a deliberately broad "sponsor"
        # substring, because the most common real phrasing is COMPOUND:
        #   "Are you authorized to work in the US *without sponsorship*?"
        # That matches the work-auth keywords below, and answering it from
        # authorized_to_work_us returns "Yes" — which for an F-1 OPT holder is
        # false. They are authorized now, but not without future sponsorship.
        #
        # The sponsorship question is the ONE genuine auto-reject gate in
        # Greenhouse/Ashby (auto-reject fires only on yes/no, single- and
        # multi-select custom questions — never on resume keywords), and its
        # truthful answer depends on the applicant's real plans, not on a stored
        # default. Answering it falsely is wilful misrepresentation under
        # INA 212(a)(6)(C)(i) — a PERMANENT inadmissibility bar with no time
        # limit — and the standard is wilful misrepresentation by the APPLICANT,
        # so "the tool answered for me" is not a defence.
        #
        # We therefore never resolve any question that mentions sponsorship:
        # return (None, 0.0) so the caller routes it to the human, exactly as
        # answer_pack.py already does ("auto": False). Over-routing costs the
        # user one click; under-routing can cost them the right to be here.
        # See docs/research/hiring-machine-2026-08.md §1.3 and §1.7.
        if "sponsor" in low or any(kw in low for kw in _SPONSORSHIP_KWS):
            log.info("QAResolver: refusing to auto-answer sponsorship question '%s'", question_text)
            return None, 0.0

        auth_kws = ["authorized to work", "legally authorized", "legal right to work", "lawful right to work", "eligible to work in", "right to work", "work authorization", "work permit", "unrestricted work"]
        if any(kw in low for kw in auth_kws):
            # A missing store value is NOT "authorized" — the old default True
            # attested work authorization for anyone without the key.
            auth = self._get_nested(["work_authorization", "authorized_to_work_us"], None)
            if auth is None:
                return None, 0.0
            return "Yes" if auth else "No", 0.95

        # 3. Security Clearance
        clearance_kws = ["security clearance", "active clearance", "clearance level", "government clearance", "active security clearance"]
        if any(kw in low for kw in clearance_kws):
            req_clearance = self._get_nested(["general", "requires_clearance"], None)
            if req_clearance is None:
                return None, 0.0
            return "Yes" if req_clearance else "No", 0.95

        # 4. Relocation
        reloc_kws = ["willing to relocate", "willingness to relocate", "willing to move"]
        if any(kw in low for kw in reloc_kws):
            reloc = self._get_nested(["preferences", "willing_to_relocate"], None)
            if reloc is None:
                return None, 0.0
            return "Yes" if reloc else "No", 0.95

        reloc_pref_kws = ["where in the united states will you be working from", "working location preference"]
        if any(kw in low for kw in reloc_pref_kws):
            # Answer from THIS user's stored preference. The resolved value was
            # previously computed and then discarded in favour of a sentence
            # hardcoded from the single-user era ("I do not currently live in
            # New York, San Francisco…"), which every tenant's form received
            # regardless of where they actually live.
            return self._stored(["preferences", "relocation_details"])

        # 5. Salary expectation — "Negotiable" is a deliberate generic (it
        # states no fact about the person), so it may remain a default.
        salary_kws = ["salary expectation", "desired salary", "salary requirement", "compensation expectation", "target salary", "salary requirements"]
        if any(kw in low for kw in salary_kws):
            return self._get_nested(["preferences", "salary_range"], "Negotiable"), 0.95

        # 6. US Based — from the store, never an unconditional "Yes" (which
        # was false for any non-US tenant).
        us_based_kws = [
            "based in the united states", "reside in the united states", "living in the united states",
            "based in the us", "currently based in the us", "currently based in the united states",
            "located in the us", "located in the united states", "currently in the us", "currently in the united states",
            "residing in the us", "residing in the united states", "are you based in the united states", "are you based in the us"
        ]
        if any(kw in low for kw in us_based_kws):
            us_based = self._get_nested(["general", "us_based"], None)
            if us_based is None:
                return None, 0.0
            return "Yes" if us_based else "No", 0.95

        # 7. AI Policy Compliance
        # Note: greenhouse has EEO/compliance dropdowns where "No" or "Yes" are selected.
        # Agreement comes from the store — auto-agreeing to an arbitrary policy
        # on someone's behalf is not a default.
        if "policy" in low and ("ai" in low or "artificial intelligence" in low):
            agrees = self._get_nested(["general", "agrees_ai_policy"], None)
            if agrees is None:
                return None, 0.0
            return "Yes" if agrees else "No", 0.95

        # 8. Referral Source / Hear about us — "LinkedIn" is a deliberate
        # generic marketing answer, not an identity fact; it may remain.
        hear_kws = ["how did you hear", "hear about us", "source of referral",
                    "referral source", "how did you find", "hear about this"]
        if any(kw in low for kw in hear_kws):
            return self._get_nested(["general", "how_did_you_hear"], "LinkedIn"), 0.95

        # 9. State Reside — derived from the STORED location only (the old
        # default was the founder's own city, so an empty store answered with
        # the founder's state for every tenant — as did the pre-resolver
        # _EEOC_DEFAULTS dict).
        state_kws = ["which state do you currently reside in", "what state do you currently reside in",
                     "state of residence", "u.s state"]
        label_exact = low.replace("?", "").rstrip("*").strip()
        if any(kw in low for kw in state_kws) or label_exact in ("state", "current state"):
            loc = self._get_nested(["identity", "location"], None)
            state_name = self._get_state_from_location(loc)
            if state_name:
                return state_name, 0.95
            return None, 0.0
        # 10. Office Location (Job Context)
        if job and (low == "location" or low == "city" or any(kw in low for kw in [
            "office location", "preferred location", "work location", "location you are interested in",
            "location preference", "which office", "interest in working from", "working location"
        ])):
            # Look for city in parentheses, e.g. "Deployed Engineer (Charlotte)"
            match = re.search(r'\(([^)]+)\)', job.title)
            if match:
                city_query = match.group(1).strip().lower()
                if "charlotte" in city_query:
                    return "Charlotte, NC", 0.95
                if "denver" in city_query:
                    return "Denver, CO", 0.95
                if "san francisco" in city_query or "sf" in city_query:
                    return "San Francisco, CA", 0.95
                if "new york" in city_query or "nyc" in city_query:
                    return "New York, NY", 0.95
            
            # Headquarters mapping
            co_low = job.company.lower()
            if any(name in co_low for name in ["perplexity", "baseten", "cohere", "openai"]):
                return "San Francisco, CA", 0.95
                
            if job.location:
                return job.location, 0.95

        # 11. Current / Previous Company / Title
        company_kws = ["current company", "current employer", "company you work for", "employer name", "most recent employer", "most recent company"]
        if any(kw in low for kw in company_kws) or low == "company" or low == "employer":
            current_co = self._get_nested(["employment", "current_employer"], "")
            previous_co = self._get_nested(["employment", "previous_employer"], "")
            if current_co:
                return current_co, 0.95
            elif previous_co:
                return f"Open to work. Previously at {previous_co}.", 0.95
            return "Open to work", 0.95

        title_kws = ["current title", "most recent title", "job title"]
        if any(kw in low for kw in title_kws):
            current_title = self._get_nested(["employment", "current_title"], "")
            previous_title = self._get_nested(["employment", "previous_title"], "")
            if current_title:
                return current_title, 0.95
            elif previous_title:
                return previous_title, 0.95

        # 12. Years of Experience
        exp_kws = ["years of experience", "years experience", "years of relevant experience",
                   "years of professional experience", "years of work experience",
                   "how many years", "total years", "number of years"]
        if any(kw in low for kw in exp_kws):
            yoe = self._get_nested(["experience", "total_yoe"], None)
            if yoe is None:
                return None, 0.0
            return str(yoe), 0.95

        # 13. Education — store-only; the old defaults were the founder's own
        # degree, school and graduation date.
        edu_grad_date_kws = ["graduation date", "date of graduation", "when did you graduate", "when did you complete your degree", "graduation month/year"]
        if any(kw in low for kw in edu_grad_date_kws):
            return self._stored(["education", "graduation_date"])

        edu_grad_year_kws = ["graduation year", "year of graduation", "grad year"]
        if any(kw in low for kw in edu_grad_year_kws):
            year = self._get_nested(["education", "graduation_year"], None)
            if year is None:
                return None, 0.0
            return str(year), 0.95

        edu_degree_kws = ["degree", "highest degree", "education level", "level of education", "type of degree"]
        if any(kw in low for kw in edu_degree_kws):
            return self._stored(["education", "degree"])

        edu_uni_kws = ["university", "school", "college", "undergrad", "graduate school"]
        if any(kw in low for kw in edu_uni_kws):
            return self._stored(["education", "university"])

        edu_major_kws = ["major", "field of study", "specialization", "program"]
        if any(kw in low for kw in edu_major_kws):
            return self._stored(["education", "field"])

        # EEO Mappings (Greenhouse EEO/Voluntary disclosure values)
        # Note: Check these before general Yes/No checks. STORE-ONLY: the old
        # defaults asserted one specific person's demographics ("Male",
        # "Asian", not-a-veteran, no-disability) for anyone whose store lacked
        # the keys. Unstored → (None, 0.0); the autofill layer then falls back
        # to neutral decline-to-self-identify answers, which are truthful for
        # everyone (see agent._EEO_SAFE_DECLINES).
        if "gender" in low:
            return self._stored(["eeo", "gender"])
        if "hispanic" in low or "latino" in low:
            hispanic = self._get_nested(["eeo", "hispanic_latino"], None)
            if hispanic is None:
                return None, 0.0
            return "No, I am not Hispanic/Latino" if not hispanic else "Yes", 0.95
        if "race" in low or "ethnicity" in low or "racial" in low:
            return self._stored(["eeo", "race"])
        if "veteran" in low:
            return self._stored(["eeo", "veteran_status"])
        if "disability" in low:
            return self._stored(["eeo", "disability_status"])

        # 13. General Yes/No Safety Checks
        yes_no_prefix = [
            "do you", "are you", "have you", "can you", "will you", "would you", "did you", 
            "is your", "are your", "should you", "please confirm", "confirm you", "do you agree", "agree to"
        ]
        non_yesno_prefix = [
            "how many", "how much", "what is", "what are", "what's", "which", "when", "where",
            "how long", "how often", "describe", "explain", "list", "tell us", "provide",
            "please describe", "please explain", "please provide", "please list",
            "what", "how"
        ]
        low_clean = low.replace("?", "").strip()
        is_non_yesno = any(low_clean.startswith(prefix) for prefix in non_yesno_prefix)
        is_yes_no = (
            not is_non_yesno and (
                any(low_clean.startswith(prefix) for prefix in yes_no_prefix) or 
                "yes/no" in low or
                "yes or no" in low
            )
        )

        if is_yes_no:
            # Re-check visa sponsorship and security clearance as safety fallback.
            # Sponsorship stays human-routed here too — a yes/no-shaped phrasing
            # we did not match above is exactly the case where guessing is worst.
            if "sponsor" in low or any(kw in low for kw in _SPONSORSHIP_KWS):
                log.info("QAResolver: refusing to auto-answer sponsorship question '%s'", question_text)
                return None, 0.0
            if any(kw in low for kw in clearance_kws):
                req_clearance = self._get_nested(["general", "requires_clearance"], None)
                if req_clearance is None:
                    return None, 0.0
                return "Yes" if req_clearance else "No", 0.95
                
            # Under 18 / minor checks — from the store (an attestation about
            # the person, not a universal fact)
            if any(kw in low for kw in ["under 18", "less than 18", "under the age of 18", "a minor"]):
                minor = self._get_nested(["general", "under_18"], None)
                if minor is None:
                    return None, 0.0
                return "Yes" if minor else "No", 0.95

            # Criminal record check — never guessed; unstored routes to human
            if any(kw in low for kw in ["convicted", "felony", "criminal record", "criminal history", "misdemeanor"]):
                has_record = self._get_nested(["general", "has_criminal_record"], None)
                if has_record is None:
                    return None, 0.0
                return "Yes" if has_record else "No", 0.95
            # Previous employment / interviewed with this company
            if any(kw in low for kw in [
                "previously worked", "previously been employed", "former employee", "former contractor",
                "employed by us", "worked for us", "worked at this company", "prior employee", "worked here before",
                "previously interviewed", "interviewed before", "interviewed at", "applied before", "previously applied"
            ]):
                prev = self._get_nested(["general", "previously_employed_here"], None)
                if prev is None:
                    return None, 0.0
                return "Yes" if prev else "No", 0.95
            if any(kw in low for kw in ["terminated for cause", "discharged from employment", "fired"]):
                term = self._get_nested(["general", "terminated_for_cause"], None)
                if term is None:
                    return None, 0.0
                return "Yes" if term else "No", 0.95

            # Non-compete/conflict
            if any(kw in low for kw in ["non-compete", "conflict of interest", "subject to a non-compete", "restrictive covenant"]):
                has_noncomp = self._get_nested(["general", "has_noncompete"], None)
                if has_noncomp is None:
                    return None, 0.0
                return "Yes" if has_noncomp else "No", 0.95

            # Remove blanket "Yes" default.
            # Instead, return None with 0.0 confidence so we ask the user.
            log.info("QAResolver: Unknown Yes/No question '%s' — routing to human", question_text)
            return None, 0.0

        return None, 0.0
