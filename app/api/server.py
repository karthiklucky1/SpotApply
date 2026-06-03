"""Small FastAPI surface for status + manual triggers.

Endpoints:
  GET  /health           — liveness
  GET  /stats            — counts by status
  POST /run/discovery    — kick off discovery
  POST /run/matching     — kick off matching
  POST /run/tailor       — tailor all SHORTLISTED
  POST /run/autofill/{id} — autofill one application
"""
from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlalchemy import func, desc

from app.autofill.agent import autofill, preview
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job
from app.discovery.pipeline import run_discovery
from app.matching.pipeline import run_matching
from app.tailoring.tailor import tailor_all_shortlisted

app = FastAPI(title="JobAgent")


@app.on_event("startup")
async def startup_event():
    import asyncio
    from app.autofill.agent import set_main_loop
    set_main_loop(asyncio.get_running_loop())


@app.get("/")
def index_redirect():
    """Redirect to the dashboard by default."""
    return RedirectResponse(url="/dashboard")

try:
    templates = Jinja2Templates(directory="app/templates")
except Exception:
    # Fallback if running from a different working directory
    import os
    templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/stats")
def stats() -> dict:
    with get_session() as session:
        total_jobs = len(session.exec(select(Job)).all())
        by_status = {}
        for st in ApplicationStatus:
            apps = session.exec(select(Application).where(Application.status == st)).all()
            if apps:
                by_status[st.value] = len(apps)
    return {"total_jobs": total_jobs, "applications": by_status}


@app.get("/shortlist")
def shortlist():
    """Top-scored jobs not yet processed."""
    with get_session() as session:
        jobs = session.exec(
            select(Job).where(Job.rerank_score >= 70).order_by(Job.rerank_score.desc())
        ).all()
    return [
        {
            "id": j.id,
            "company": j.company,
            "title": j.title,
            "location": j.location,
            "url": j.url,
            "similarity": j.similarity_score,
            "rerank": j.rerank_score,
            "reason": j.rerank_reasoning,
        }
        for j in jobs
    ]


@app.get("/api/stats")
def api_stats() -> dict:
    with get_session() as session:
        # Total jobs in db
        total_jobs = session.exec(select(func.count(Job.id))).first() or 0
        
        # Unique companies in Job db
        total_companies = session.exec(select(func.count(func.distinct(Job.company)))).first() or 0
        
        # Funnel metrics
        cross_encoder_passed = session.exec(
            select(func.count(Job.id)).where(Job.similarity_score.is_not(None))
        ).first() or 0
        
        reranker_scored = session.exec(
            select(func.count(Job.id)).where(Job.rerank_score.is_not(None))
        ).first() or 0
        
        # Application counts by status
        app_counts = {}
        for status in ApplicationStatus:
            count = session.exec(
                select(func.count(Application.id)).where(Application.status == status)
            ).first() or 0
            app_counts[status.value] = count
            
        shortlisted = app_counts[ApplicationStatus.SHORTLISTED.value] + app_counts[ApplicationStatus.TAILORED.value]
            
        # Score distribution
        band_85_100 = session.exec(select(func.count(Job.id)).where(Job.rerank_score >= 85)).first() or 0
        band_60_84 = session.exec(select(func.count(Job.id)).where((Job.rerank_score >= 60) & (Job.rerank_score < 85))).first() or 0
        band_40_59 = session.exec(select(func.count(Job.id)).where((Job.rerank_score >= 40) & (Job.rerank_score < 60))).first() or 0
        band_0_39 = session.exec(select(func.count(Job.id)).where((Job.rerank_score >= 0) & (Job.rerank_score < 40))).first() or 0
        unranked = session.exec(select(func.count(Job.id)).where(Job.rerank_score.is_(None))).first() or 0
        
        # Top companies
        top_companies_res = session.exec(
            select(Job.company, func.count(Job.id))
            .group_by(Job.company)
            .order_by(desc(func.count(Job.id)))
            .limit(10)
        ).all()
        top_companies = [{"company": company, "count": count} for company, count in top_companies_res]
        
        # Company registry stats
        from app.db.models import CompanyRegistry
        total_boards = session.exec(select(func.count(CompanyRegistry.id))).first() or 0
        active_boards = session.exec(select(func.count(CompanyRegistry.id)).where(CompanyRegistry.is_active == True)).first() or 0
        total_validated_jobs = session.exec(select(func.sum(CompanyRegistry.job_count))).first() or 0
        
    return {
        "total_jobs": total_jobs,
        "total_companies": total_companies,
        "funnel": {
            "total_pool": total_jobs,
            "cross_encoder_passed": cross_encoder_passed,
            "reranker_scored": reranker_scored,
            "shortlisted": shortlisted,
        },
        "applications": app_counts,
        "scores": {
            "band_85_100": band_85_100,
            "band_60_84": band_60_84,
            "band_40_59": band_40_59,
            "band_0_39": band_0_39,
            "unranked": unranked,
        },
        "top_companies": top_companies,
        "registry": {
            "total_boards": total_boards,
            "active_boards": active_boards,
            "total_validated_jobs": total_validated_jobs,
        }
    }


@app.get("/api/jobs")
def api_jobs(
    page: int = 1,
    limit: int = 50,
    search: str = None,
    company: str = None,
    status: str = None,
    min_score: int = None,
    max_score: int = None,
    remote: str = None
) -> dict:
    offset = (page - 1) * limit
    
    with get_session() as session:
        # Base query
        query = select(Job, Application).outerjoin(Application, Application.job_id == Job.id)
        
        # Apply filters
        if search:
            search_pattern = f"%{search}%"
            query = query.where(Job.title.like(search_pattern) | Job.company.like(search_pattern) | Job.location.like(search_pattern))
        
        if company:
            query = query.where(Job.company == company)
            
        if status:
            if status == "unprocessed":
                query = query.where(Application.id.is_(None))
            else:
                query = query.where(Application.status == status)
                
        if min_score is not None:
            query = query.where(Job.rerank_score >= min_score)
            
        if max_score is not None:
            query = query.where(Job.rerank_score <= max_score)
            
        if remote is not None:
            is_remote = remote.lower() == "true"
            query = query.where(Job.remote == is_remote)
            
        # Get total count (for pagination)
        count_query = select(func.count(Job.id))
        if search:
            search_pattern = f"%{search}%"
            count_query = count_query.where(Job.title.like(search_pattern) | Job.company.like(search_pattern) | Job.location.like(search_pattern))
        if company:
            count_query = count_query.where(Job.company == company)
        if status:
            if status == "unprocessed":
                count_query = count_query.select_from(Job).outerjoin(Application, Application.job_id == Job.id).where(Application.id.is_(None))
            else:
                count_query = count_query.select_from(Job).join(Application, Application.job_id == Job.id).where(Application.status == status)
        if min_score is not None:
            count_query = count_query.where(Job.rerank_score >= min_score)
        if max_score is not None:
            count_query = count_query.where(Job.rerank_score <= max_score)
        if remote is not None:
            is_remote = remote.lower() == "true"
            count_query = count_query.where(Job.remote == is_remote)
            
        total = session.exec(count_query).first() or 0
        
        # Apply pagination and sorting
        query = query.order_by(desc(Job.rerank_score), desc(Job.similarity_score), desc(Job.id)).offset(offset).limit(limit)
        
        results = session.exec(query).all()
        
        jobs_list = []
        for job, app in results:
            jobs_list.append({
                "id": job.id,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "remote": job.remote,
                "url": job.url,
                "similarity": job.similarity_score,
                "rerank": job.rerank_score,
                "reason": job.rerank_reasoning,
                "application": {
                    "id": app.id,
                    "status": app.status.value,
                    "created_at": app.created_at.isoformat() if app.created_at else None,
                    "updated_at": app.updated_at.isoformat() if app.updated_at else None,
                } if app else None
            })
            
        import math
        pages = math.ceil(total / limit) if total else 0
        
        return {
            "jobs": jobs_list,
            "total": total,
            "page": page,
            "pages": pages,
            "limit": limit
        }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """Kanban board UI for tracking application progress."""
    with get_session() as session:
        results = session.exec(
            select(Application, Job)
            .join(Job)
            .order_by(Application.updated_at.desc())
        ).all()
        
    shortlisted = []
    bot_filled = []
    submitted = []
    skipped = []
    
    for app_model, job_model in results:
        if app_model.status in [ApplicationStatus.SHORTLISTED, ApplicationStatus.TAILORED]:
            shortlisted.append((app_model, job_model))
        elif app_model.status in [ApplicationStatus.AUTOFILLED, ApplicationStatus.AWAITING_USER, ApplicationStatus.READY_TO_SUBMIT]:
            bot_filled.append((app_model, job_model))
        elif app_model.status in [ApplicationStatus.SUBMITTED, ApplicationStatus.INTERVIEWING]:
            submitted.append((app_model, job_model))
        elif app_model.status == ApplicationStatus.SKIPPED:
            skipped.append((app_model, job_model))
            
    # Sort shortlisted by rerank_score descending (highest score at top)
    shortlisted.sort(key=lambda x: x[1].rerank_score or 0, reverse=True)
            
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "shortlisted": shortlisted,
            "bot_filled": bot_filled,
            "submitted": submitted,
            "skipped": skipped,
        }
    )


@app.get("/application/{application_id}/details")
def application_details(application_id: int) -> dict:
    """Return tailored resume + cover letter text for modal preview."""
    from pathlib import Path
    with get_session() as session:
        application = session.get(Application, application_id)
        if not application:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Application not found")
        job = session.get(Job, application.job_id)

    resume_text = ""
    cover_text = ""

    if application.tailored_resume_path:
        try:
            from docx import Document
            doc = Document(application.tailored_resume_path)
            resume_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            resume_text = f"(Could not read resume: {e})"

    if application.cover_letter_path:
        try:
            cover_text = Path(application.cover_letter_path).read_text(encoding="utf-8")
        except Exception as e:
            cover_text = f"(Could not read cover letter: {e})"

    return {
        "id": application_id,
        "company": job.company,
        "title": job.title,
        "apply_url": application.apply_url or job.url,
        "status": application.status.value,
        "resume": resume_text,
        "cover_letter": cover_text,
    }


@app.post("/application/{application_id}/submit")
def mark_as_submitted(application_id: int) -> dict:
    from datetime import datetime
    with get_session() as session:
        application = session.get(Application, application_id)
        if not application:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Application not found")
        application.status = ApplicationStatus.SUBMITTED
        application.submitted_at = datetime.utcnow()
        session.add(application)
        session.commit()
    return {"success": True, "application_id": application_id}


@app.post("/application/{application_id}/skip")
def skip_application(application_id: int) -> dict:
    with get_session() as session:
        application = session.get(Application, application_id)
        if not application:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Application not found")
        application.status = ApplicationStatus.SKIPPED
        session.add(application)
        session.commit()
    return {"success": True, "application_id": application_id}



@app.post("/run/discovery")
def trigger_discovery(bg: BackgroundTasks) -> dict:
    bg.add_task(run_discovery)
    return {"started": "discovery"}


@app.post("/run/matching")
def trigger_matching(bg: BackgroundTasks) -> dict:
    bg.add_task(run_matching)
    return {"started": "matching"}


@app.post("/run/tailor")
def trigger_tailor(bg: BackgroundTasks) -> dict:
    bg.add_task(tailor_all_shortlisted)
    return {"started": "tailoring"}


@app.post("/run/autofill/{application_id}")
def trigger_autofill(application_id: int, bg: BackgroundTasks) -> dict:
    bg.add_task(autofill, application_id)
    return {"started": "autofill", "application_id": application_id}


@app.post("/run/preview/{application_id}")
def trigger_preview(application_id: int, bg: BackgroundTasks) -> dict:
    """Re-open the filled form in a visible Playwright browser for user review."""
    bg.add_task(preview, application_id)
    return {"started": "preview", "application_id": application_id}


from pydantic import BaseModel

class ExtractLinkRequest(BaseModel):
    url: str


@app.post("/run/extract-link")
async def trigger_extract_link(req: ExtractLinkRequest, bg: BackgroundTasks) -> dict:
    from app.discovery.extractor import extract_and_rank_job
    from app.tailoring.tailor import tailor_for_application
    
    log.info("Extracting manual link: %s", req.url)
    app_id = await extract_and_rank_job(req.url)
    
    # Tailor in the background
    bg.add_task(tailor_for_application, app_id)
    
    return {"success": True, "application_id": app_id}
