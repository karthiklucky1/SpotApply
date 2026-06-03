"""SQLModel schema. Single source of truth for job + application state."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class JobSource(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    WELLFOUND = "wellfound"
    OTTA = "otta"
    MANUAL = "manual"


class ApplicationStatus(str, Enum):
    DISCOVERED = "discovered"           # just scraped
    MATCHED = "matched"                  # passed similarity threshold
    SHORTLISTED = "shortlisted"          # passed Claude rerank
    TAILORED = "tailored"                # resume + cover letter generated
    AUTOFILLED = "autofilled"            # form filled, awaiting user
    AWAITING_USER = "awaiting_user"      # Telegram prompt pending
    READY_TO_SUBMIT = "ready_to_submit"  # all fields filled, preview link sent
    SUBMITTED = "submitted"              # applicant clicked submit
    REJECTED = "rejected"                # heard back: no
    INTERVIEWING = "interviewing"
    SKIPPED = "skipped"                  # user declined
    ERROR = "error"


class Job(SQLModel, table=True):
    """A single job posting. external_id + source uniquely identifies."""
    id: Optional[int] = Field(default=None, primary_key=True)
    source: JobSource
    external_id: str = Field(index=True)
    company: str
    title: str
    location: str = ""
    remote: bool = False
    url: str
    description: str = ""
    posted_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    # Matching outputs
    embedding_id: Optional[int] = Field(default=None, index=True)  # FAISS index position
    similarity_score: Optional[float] = None
    rerank_score: Optional[float] = None
    rerank_reasoning: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


class Application(SQLModel, table=True):
    """One application per (job, attempt). Tracks lifecycle."""
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    status: ApplicationStatus = ApplicationStatus.DISCOVERED
    tailored_resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    apply_url: Optional[str] = None  # may differ from job.url after redirects
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    submitted_at: Optional[datetime] = None
    notes: Optional[str] = None


class PendingQuestion(SQLModel, table=True):
    """A question the autofill agent needs answered via Telegram."""
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    field_label: str            # e.g. "Years of experience with PyTorch"
    field_selector: str         # CSS selector or DOM ref
    field_type: str             # text, select, radio, file, etc.
    options: Optional[str] = None  # JSON list for select/radio
    answer: Optional[str] = None
    asked_at: datetime = Field(default_factory=datetime.utcnow)
    answered_at: Optional[datetime] = None


class AnswerMemory(SQLModel, table=True):
    """Cached answers to common application questions, keyed by normalized label.
    Personal answer memory for repeated application fields.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    label_normalized: str = Field(index=True)
    label_original: str
    answer: str
    last_used_at: datetime = Field(default_factory=datetime.utcnow)
    use_count: int = 1


class CompanyRegistry(SQLModel, table=True):
    """Registry of harvested company slugs for Greenhouse, Lever, and Ashby."""
    __table_args__ = (
        UniqueConstraint("slug", "ats", name="uq_slug_ats"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True)
    ats: JobSource = Field(index=True)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: Optional[datetime] = Field(default=None, index=True)
    is_active: bool = Field(default=True, index=True)
    job_count: int = Field(default=0)
    source: str = Field(default="seed")  # e.g., seed, common_crawl, yc_startup, dork

