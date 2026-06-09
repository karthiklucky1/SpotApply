import logging
import os
import re
from pathlib import Path
import yaml
from app.db.models import Job

log = logging.getLogger(__name__)

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
            If confidence < 0.7, the caller should prompt the user (Telegram).
        """
        low = question_text.lower().strip()

        # 0. Check human routing blocklist
        always_ask = self.data.get("always_ask_human", [])
        if any(pattern.lower() in low for pattern in always_ask):
            log.info("QAResolver: Human route match for '%s'", question_text)
            return None, 0.0

        # 1. Social links & Identity
        if "linkedin" in low:
            return self._get_nested(["identity", "linkedin"], "https://www.linkedin.com/in/amruthaluri/"), 0.95
        if "github" in low:
            return self._get_nested(["identity", "github"], "https://github.com/karthiklucky1"), 0.95
        if "website" in low or "portfolio" in low:
            return self._get_nested(["identity", "github"], "https://github.com/karthiklucky1"), 0.95
        if "preferred name" in low or "preferred  name" in low:
            return self._get_nested(["identity", "first_name"], "Karthik"), 0.95
        if "full name" in low:
            first = self._get_nested(["identity", "first_name"], "Karthik")
            last = self._get_nested(["identity", "last_name"], "Amruthaluri")
            return f"{first} {last}".strip(), 0.95
        if "first name" in low:
            return self._get_nested(["identity", "first_name"], "Karthik"), 0.95
        if "last name" in low:
            return self._get_nested(["identity", "last_name"], "Amruthaluri"), 0.95
        if "email" in low:
            return self._get_nested(["identity", "email"], "karthikamruthaluri2002@gmail.com"), 0.95
        if "phone" in low or "mobile" in low:
            return self._get_nested(["identity", "phone"], "(513) 276-3950"), 0.95

        # 2. Work Authorization
        auth_kws = ["authorized to work", "legally authorized", "legal right to work", "lawful right to work", "eligible to work in", "right to work", "work authorization", "work permit", "unrestricted work"]
        if any(kw in low for kw in auth_kws):
            auth = self._get_nested(["work_authorization", "authorized_to_work_us"], True)
            return "Yes" if auth else "No", 0.95

        spons_kws = ["require sponsorship", "sponsorship now or in the future", "visa sponsorship", "sponsorship in the future", "require visa", "sponsorship requirements"]
        if any(kw in low for kw in spons_kws):
            spons = self._get_nested(["work_authorization", "requires_sponsorship"], False)
            return "Yes" if spons else "No", 0.95

        # 3. Security Clearance
        clearance_kws = ["security clearance", "active clearance", "clearance level", "government clearance", "active security clearance"]
        if any(kw in low for kw in clearance_kws):
            req_clearance = self._get_nested(["general", "requires_clearance"], False)
            return "Yes" if req_clearance else "No", 0.95

        # 4. Relocation
        reloc_kws = ["willing to relocate", "willingness to relocate", "willing to move"]
        if any(kw in low for kw in reloc_kws):
            reloc = self._get_nested(["preferences", "willing_to_relocate"], True)
            return "Yes" if reloc else "No", 0.95

        reloc_pref_kws = ["where in the united states will you be working from", "working location preference"]
        if any(kw in low for kw in reloc_pref_kws):
            reloc_details = self._get_nested(["preferences", "relocation_details"], "Open to relocation")
            # Fallback to current hardcoded answer if we want:
            return "I do not currently live in New York, San Francisco - but I am willing to relocate within 6 months", 0.95

        # 5. Salary expectation
        salary_kws = ["salary expectation", "desired salary", "salary requirement", "compensation expectation", "target salary", "salary requirements"]
        if any(kw in low for kw in salary_kws):
            return self._get_nested(["preferences", "salary_range"], "Negotiable"), 0.95

        # 6. US Based
        us_based_kws = [
            "based in the united states", "reside in the united states", "living in the united states",
            "based in the us", "currently based in the us", "currently based in the united states",
            "located in the us", "located in the united states", "currently in the us", "currently in the united states",
            "residing in the us", "residing in the united states", "are you based in the united states", "are you based in the us"
        ]
        if any(kw in low for kw in us_based_kws):
            return "Yes", 0.95

        # 7. AI Policy Compliance
        # Note: greenhouse has EEO/compliance dropdowns where "No" or "Yes" are selected
        # If the question contains 'policy', return 'Yes' (to agree with the policy)
        if "policy" in low and ("ai" in low or "artificial intelligence" in low):
            return "Yes", 0.95

        # 8. Referral Source / Hear about us
        hear_kws = ["how did you hear", "hear about us", "source of referral"]
        if any(kw in low for kw in hear_kws):
            return self._get_nested(["general", "how_did_you_hear"], "LinkedIn"), 0.95

        # 9. State Reside
        state_kws = ["which state do you currently reside in", "what state do you currently reside in", "state of residence"]
        if any(kw in low for kw in state_kws):
            loc = self._get_nested(["identity", "location"], "Cincinnati, OH")
            state_name = self._get_state_from_location(loc)
            if state_name:
                return state_name, 0.95
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
            previous_co = self._get_nested(["employment", "previous_employer"], "Home Depot")
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
            yoe = self._get_nested(["experience", "total_yoe"], 3)
            return str(yoe), 0.95

        # 13. Education
        edu_grad_date_kws = ["graduation date", "date of graduation", "when did you graduate", "when did you complete your degree", "graduation month/year"]
        if any(kw in low for kw in edu_grad_date_kws):
            return self._get_nested(["education", "graduation_date"], "April 30, 2026"), 0.95

        edu_grad_year_kws = ["graduation year", "year of graduation", "grad year"]
        if any(kw in low for kw in edu_grad_year_kws):
            return str(self._get_nested(["education", "graduation_year"], 2026)), 0.95

        edu_degree_kws = ["degree", "highest degree", "education level", "level of education", "type of degree"]
        if any(kw in low for kw in edu_degree_kws):
            return self._get_nested(["education", "degree"], "Master of Engineering"), 0.95

        edu_uni_kws = ["university", "school", "college", "undergrad", "graduate school"]
        if any(kw in low for kw in edu_uni_kws):
            return self._get_nested(["education", "university"], "University of Cincinnati"), 0.95

        edu_major_kws = ["major", "field of study", "specialization", "program"]
        if any(kw in low for kw in edu_major_kws):
            return "Engineering", 0.95

        # EEO Mappings (Greenhouse EEO/Voluntary disclosure values)
        # Note: Check these before general Yes/No checks
        if "gender" in low:
            return self._get_nested(["eeo", "gender"], "Male"), 0.95
        if "hispanic" in low or "latino" in low:
            hispanic = self._get_nested(["eeo", "hispanic_latino"], False)
            return "No, I am not Hispanic/Latino" if not hispanic else "Yes", 0.95
        if "race" in low or "ethnicity" in low or "racial" in low:
            return self._get_nested(["eeo", "race"], "Asian"), 0.95
        if "veteran" in low:
            return self._get_nested(["eeo", "veteran_status"], "I am not a protected veteran"), 0.95
        if "disability" in low:
            return self._get_nested(["eeo", "disability_status"], "No, I do not have a disability, or history/record of having a disability"), 0.95

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
            # Re-check visa sponsorship and security clearance as safety fallback
            if any(kw in low for kw in spons_kws):
                spons = self._get_nested(["work_authorization", "requires_sponsorship"], False)
                return "Yes" if spons else "No", 0.95
            if any(kw in low for kw in clearance_kws):
                req_clearance = self._get_nested(["general", "requires_clearance"], False)
                return "Yes" if req_clearance else "No", 0.95
                
            # Under 18 / minor checks -> No
            if any(kw in low for kw in ["under 18", "less than 18", "under the age of 18", "a minor"]):
                return "No", 0.95
                
            # Criminal record check
            if any(kw in low for kw in ["convicted", "felony", "criminal record", "criminal history", "misdemeanor"]):
                has_record = self._get_nested(["general", "has_criminal_record"], False)
                return "Yes" if has_record else "No", 0.95
            # Previous employment / interviewed with this company
            if any(kw in low for kw in [
                "previously worked", "previously been employed", "former employee", "former contractor",
                "employed by us", "worked for us", "worked at this company", "prior employee", "worked here before",
                "previously interviewed", "interviewed before", "interviewed at", "applied before", "previously applied"
            ]):
                prev = self._get_nested(["general", "previously_employed_here"], False)
                return "Yes" if prev else "No", 0.95
            if any(kw in low for kw in ["terminated for cause", "discharged from employment", "fired"]):
                return "No", 0.95

            # Non-compete/conflict
            if any(kw in low for kw in ["non-compete", "conflict of interest", "subject to a non-compete", "restrictive covenant"]):
                has_noncomp = self._get_nested(["general", "has_noncompete"], False)
                return "Yes" if has_noncomp else "No", 0.95

            # Remove blanket "Yes" default.
            # Instead, return None with 0.0 confidence so we prompt the user via Telegram.
            log.info("QAResolver: Unknown Yes/No question '%s' — routing to human", question_text)
            return None, 0.0

        return None, 0.0
