import logging
from enum import Enum

log = logging.getLogger(__name__)

class SponsorshipLikelihood(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

class SponsorshipChecker:
    def __init__(self):
        # Future: load DOL disclosure CSV data or cache it in SQLite
        log.info("SponsorshipChecker initialized (stub).")

    def check_company(self, company_name: str) -> SponsorshipLikelihood:
        """Query DOL disclosure data or rules to check company sponsorship history."""
        name = company_name.lower().strip()
        
        # High likelihood list mock
        opt_friendly = ["google", "meta", "amazon", "apple", "microsoft", "netflix", "stripe", "uber", "airbnb"]
        for brand in opt_friendly:
            if brand in name:
                return SponsorshipLikelihood.HIGH
                
        # Low likelihood list mock (e.g. government contractors, defense)
        low_likelihood = ["lockheed", "raytheon", "boeing", "defense", "military", "federal"]
        for brand in low_likelihood:
            if brand in name:
                return SponsorshipLikelihood.LOW
                
        return SponsorshipLikelihood.UNKNOWN
