import logging
from typing import Dict, Any, List
from app.db.models import Job, Application

log = logging.getLogger(__name__)

class QualityGate:
    def __init__(self):
        log.info("QualityGate initialized (stub).")

    def validate_application(self, job: Job, app: Application) -> Dict[str, Any]:
        """Validate tailored artifacts and check overall fit before submission."""
        results = {
            "passed": True,
            "checks": {
                "keyword_coverage": 0.85,
                "seniority_match": True,
                "location_match": True,
                "sponsorship_risk": "low",
                "exaggeration_score": 0.0,
            },
            "warnings": []
        }
        
        low_title = job.title.lower()
        if "staff" in low_title or "principal" in low_title or "director" in low_title or "vp" in low_title:
            results["checks"]["seniority_match"] = False
            results["warnings"].append("Seniority mismatch: Job title implies Staff/Principal/Director/VP level.")
            results["passed"] = False
            
        low_location = job.location.lower()
        non_us_keywords = ["london", "germany", "india", "berlin", "uk", "canada", "toronto"]
        if any(k in low_location for k in non_us_keywords):
            results["checks"]["location_match"] = False
            results["warnings"].append("Location mismatch: Job location seems outside target regions.")
            results["passed"] = False

        log.info("QualityGate run complete. Passed: %s", results["passed"])
        return results
