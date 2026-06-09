import logging
from typing import Dict, Any

log = logging.getLogger(__name__)

class CostDashboard:
    def __init__(self):
        log.info("CostDashboard initialized (stub).")

    def track_run_costs(self, run_id: str, stats: Dict[str, Any]) -> None:
        """Track execution metrics and tokens used for a run."""
        log.info("CostDashboard: tracked run %s stats: %s", run_id, stats)

    def generate_daily_cost_summary(self) -> str:
        """Produce markdown description of today's API costs."""
        return (
            "💰 *JobAgent API Cost Report* 💰\n\n"
            "• Rules Rejected: 0 (Free)\n"
            "• Embedding Rejected: 0 (Free)\n"
            "• LLM Calls: 0\n"
            "• Est. Daily Cost: $0.00\n"
        )
