import logging
from datetime import datetime, timedelta
from typing import List
from app.db.models import Application

log = logging.getLogger(__name__)

class OutcomeTracker:
    def __init__(self):
        log.info("OutcomeTracker initialized (stub).")

    def update_status(self, application_id: int, status: str) -> None:
        """Update job application status in CRM tracker."""
        log.info("CRM: Application %d status updated to %s", application_id, status)

    def get_silent_applications(self, days_threshold: int = 14) -> List[Application]:
        """Find applications that have had no status updates for a certain number of days."""
        return []
