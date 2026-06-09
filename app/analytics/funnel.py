import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import FunnelEvent, Application, Job

log = logging.getLogger(__name__)

class FunnelTracker:
    @staticmethod
    def record(job_id: Optional[int], stage: str, passed: bool, reason: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Record an event in the application funnel."""
        try:
            metadata_json = json.dumps(metadata) if metadata else None
            event = FunnelEvent(
                job_id=job_id,
                stage=stage,
                passed=passed,
                reason=reason,
                metadata_json=metadata_json,
                created_at=datetime.utcnow()
            )
            with get_session() as session:
                session.add(event)
                session.commit()
            log.debug("Funnel: Recorded event stage=%s job_id=%s passed=%s", stage, job_id, passed)
        except Exception as e:
            log.error("Funnel: Failed to record event: %s", e)

    @staticmethod
    def get_summary(days: int = 30) -> Dict[str, Any]:
        """Get funnel performance summary for the last N days."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        stages = [
            "discovered", 
            "rule_filtered", 
            "embedding_filtered", 
            "scored", 
            "shortlisted", 
            "tailored", 
            "applied", 
            "responded"
        ]
        
        summary: Dict[str, Any] = {}
        with get_session() as session:
            for s in stages:
                if s in ["rule_filtered", "embedding_filtered"]:
                    # Filter stages track passed and failed counts
                    passed_cnt = len(session.exec(
                        select(FunnelEvent).where(FunnelEvent.stage == s).where(FunnelEvent.passed == True).where(FunnelEvent.created_at >= cutoff)
                    ).all())
                    failed_cnt = len(session.exec(
                        select(FunnelEvent).where(FunnelEvent.stage == s).where(FunnelEvent.passed == False).where(FunnelEvent.created_at >= cutoff)
                    ).all())
                    summary[s] = {"passed": passed_cnt, "failed": failed_cnt}
                else:
                    cnt = len(session.exec(
                        select(FunnelEvent).where(FunnelEvent.stage == s).where(FunnelEvent.created_at >= cutoff)
                    ).all())
                    summary[s] = cnt
                    
            # Let's also include per-source breakdown for discovered jobs
            events = session.exec(
                select(FunnelEvent).where(FunnelEvent.stage == "discovered").where(FunnelEvent.created_at >= cutoff)
            ).all()
            sources: Dict[str, int] = {}
            for e in events:
                if e.job_id:
                    job = session.get(Job, e.job_id)
                    if job:
                        src = job.source.value if hasattr(job.source, "value") else str(job.source)
                        sources[src] = sources.get(src, 0) + 1
            summary["sources"] = sources
            
        return summary

    @staticmethod
    def get_variant_performance() -> Dict[str, Dict[str, int]]:
        """Get response rates by resume variant for A/B testing."""
        with get_session() as session:
            apps = session.exec(
                select(Application).where(Application.resume_variant != None)
            ).all()
            
            variants: Dict[str, Dict[str, int]] = {}
            for app in apps:
                var = app.resume_variant
                if not var:
                    continue
                if var not in variants:
                    variants[var] = {"applied": 0, "responded": 0, "interviewing": 0, "rejected": 0}
                
                variants[var]["applied"] += 1
                if app.response_type != "none":
                    variants[var]["responded"] += 1
                if app.response_type == "interview":
                    variants[var]["interviewing"] += 1
                elif app.response_type in ["auto_rejected", "rejected"]:
                    variants[var]["rejected"] += 1
                    
            return variants
