"""Seed the LOCAL demo database with FICTIONAL data only (SpotApply ad capture).

    . live/env.sh && live/venv/bin/python live/seed.py

Everything here is invented: Maya Chen, every employer, every posting, every
address (example.com). Nothing is copied from production or from a real person.
Rows are written with the app's own models; derived fields (salary/sponsorship
facets, hiring context, the tailored DOCX/PDF) are produced by the app's own
deterministic code (job_facets.compute, hiring_context.capture, render.*), so
the pages read exactly what a real run would have stored. The only values that
would normally come from an LLM are written by hand below and listed in
SEEDED_LLM_FIELDS: rerank_score / rerank_reasoning / rerank_breakdown (the
Claude final), prescore, corporate_insights (salary + work model only, both
stated in the posting), and the Resume Doctor numbers in report.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _stubs  # noqa: E402,F401
import _netguard  # noqa: E402,F401

LIVE = os.environ["LIVE_DIR"]
DB = os.environ["SQLITE_PATH"]
DATA = os.environ["DATA_DIR"]

SEEDED_LLM_FIELDS = ["Job.rerank_score", "Job.rerank_reasoning", "Job.rerank_breakdown",
                     "Job.prescore", "Job.corporate_insights (salary, work_model)",
                     "report.json doctor_score/human_score/verdict"]

# ── fresh database ──────────────────────────────────────────────────────────
for suffix in ("", "-wal", "-shm", "-journal"):
    try:
        os.remove(DB + suffix)
    except FileNotFoundError:
        pass

MASTER_MD = """# Maya Chen
Backend Engineer
Austin, TX | maya.chen@example.com | (555) 014-2290

## Summary
Backend engineer with six years of Python and FastAPI experience, including a Kafka event pipeline for order updates.

## Experience
### Backend Engineer | Brightwave Logistics
Aug 2020 – Present
- Built FastAPI APIs supporting 50k requests/day
- Ran a Kafka event pipeline for order updates

## Skills
Python, FastAPI, Kafka, PostgreSQL

## Education
### B.S. Computer Science
2019
"""
with open(os.path.join(DATA, "resume_master.md"), "w", encoding="utf-8") as f:
    f.write(MASTER_MD)

from sqlmodel import select  # noqa: E402

from app.db.init_db import get_session, init_db  # noqa: E402
from app.db.models import (Application, ApplicationStatus, Job, JobSource,  # noqa: E402
                           OutreachMessage, UserProfile)

init_db()

NOW = datetime.utcnow()


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


# ── the candidate ───────────────────────────────────────────────────────────
with get_session() as s:
    for p in s.exec(select(UserProfile)).all():
        s.delete(p)
    s.commit()
    s.add(UserProfile(
        user_id=None,                      # local single-user mode ("local")
        first_name="Maya", last_name="Chen",
        email="maya.chen@example.com", phone="(555) 014-2290",
        location="Austin, TX",
        work_authorization="Citizen", work_auth_status="Citizen", requires_sponsorship=False,
        current_title="Backend Engineer", years_experience=6,
        degree="B.S. Computer Science", university="", graduation_year=2019,
        education_json=json.dumps([{"degree": "B.S.", "field": "Computer Science", "university": "",
                                    "start_year": None, "end_year": 2019, "gpa": ""}]),
        experience_json=json.dumps([{"title": "Backend Engineer", "company": "Brightwave Logistics",
                                     "location": "Austin, TX", "start": "Aug 2020", "end": "Present",
                                     "summary": "Built FastAPI APIs supporting 50k requests/day. "
                                                "Ran a Kafka event pipeline for order updates."}]),
        professional_summary=("Backend engineer with six years of Python and FastAPI experience, "
                              "including a Kafka event pipeline for order updates."),
        key_skills="Python, FastAPI, Kafka, PostgreSQL",
        target_roles="Backend Engineer", target_roles_auto=False,
        job_type_preference="full_time",
        preferred_country="United States", remote_ok=True,
        open_to_relocation=True, relocation_targets="Seattle, WA",
        availability="2 weeks",
        one_click_submit=False,
        resume_uploaded_at=ago(days=3, hours=2), feed_started_at=ago(days=3, hours=2),
        last_active_at=NOW, last_meaningful_activity_at=NOW,
        created_at=ago(days=3, hours=2), updated_at=NOW,
    ))
    s.commit()

# ── postings ────────────────────────────────────────────────────────────────
LUMEN_JD = """Lumen Data builds real-time analytics for logistics teams. As a Senior Backend Engineer, you will own the Python services behind the product.

Location: Remote (US).

What you'll do
- Build Python APIs with FastAPI
- Own the Kafka event pipelines that carry shipment and order updates
- Tune PostgreSQL queries for low latency
- Review code, mentor engineers and help set backend standards

What we're looking for
- 5+ years of backend engineering experience
- 3+ years of experience with FastAPI or a similar Python framework
- 2+ years of experience with Kafka in production
- Solid PostgreSQL skills

Compensation: $165,000 - $195,000 per year, plus equity and benefits.

Lumen Data is an equal opportunity employer."""


def jd(company, title, intro, location_line, todo, need, pay):
    return (f"{company} is hiring a {title}. {intro}\n\nLocation: {location_line}\n\n"
            "What you'll do\n" + "\n".join(f"- {t}" for t in todo) +
            "\n\nWhat we're looking for\n" + "\n".join(f"- {t}" for t in need) +
            f"\n\nCompensation: {pay} per year, plus benefits.\n\n"
            f"{company} is an equal opportunity employer.")


JOBS = [
    dict(key="lumen", source=JobSource.GREENHOUSE, external_id="4821093", company="Lumen Data",
         title="Senior Backend Engineer", location="Remote (US)", remote=True,
         url="https://boards.greenhouse.io/lumendata/jobs/4821093",
         description=LUMEN_JD,
         posted=ago(hours=2, minutes=5), found=ago(hours=1, minutes=58), last_seen=ago(minutes=4),
         score=94.0, prescore=91.0, hire=0.82,
         reason=("Six years of Python/FastAPI against their core requirement, plus the Kafka event "
                 "pipeline experience this role centres on. Seniority and location match."),
         breakdown={"skills": {"score": 95, "note": "Python, FastAPI, Kafka and PostgreSQL all match the core stack"},
                    "experience": {"score": 92, "note": "Six years of backend work against a 5+ year ask"},
                    "location": {"score": 100, "note": "Remote (US); you are in Austin, TX"},
                    "work_auth": {"score": 100, "note": "US citizen: no sponsorship needed"}},
         signals=["fresh_posting_0d", "high_velocity_6_openings", "funding_language:series b"],
         insights={"salary": {"text": "$165,000 - $195,000 per year"}, "work_model": "Remote (US)"},
         context={"department": "Platform Engineering", "requisition_id": "REQ-1042"}),
    dict(key="harbor", source=JobSource.LEVER, external_id="b7f2c1e0-harbor-platform", company="Harbor Analytics",
         title="Backend Engineer, Platform", location="Remote (US)", remote=True,
         url="https://jobs.lever.co/harboranalytics/b7f2c1e0-harbor-platform",
         description=jd("Harbor Analytics", "Backend Engineer, Platform",
                        "You'll build the internal platform our product teams ship on.", "Remote (US)",
                        ["Build and run Python services behind our public API",
                         "Own event ingestion with Kafka", "Improve reliability and on-call tooling"],
                        ["4+ years building backend services", "Python and a modern web framework such as FastAPI",
                         "Experience with Kafka or similar streaming systems", "PostgreSQL"],
                        "$150,000 - $175,000"),
         posted=ago(hours=9, minutes=20), found=ago(hours=9, minutes=5), last_seen=ago(minutes=22),
         score=88.0, prescore=86.0, hire=0.74,
         reason=("Your FastAPI APIs and Kafka pipeline line up with the platform's Python services and "
                 "event ingestion. Experience level is a clean fit."),
         breakdown={"skills": {"score": 90, "note": "Python, FastAPI and Kafka match"},
                    "experience": {"score": 88, "note": "Six years against a 4+ year ask"},
                    "location": {"score": 100, "note": "Remote (US)"},
                    "work_auth": {"score": 100, "note": "No sponsorship needed"}},
         signals=["fresh_posting_0d", "med_velocity_3_openings"]),
    dict(key="vector", source=JobSource.ASHBY, external_id="9c1d-vector-apis", company="Vector Health",
         title="Senior Software Engineer, APIs", location="Austin, TX (Hybrid)", remote=False,
         url="https://jobs.ashbyhq.com/vectorhealth/9c1d-vector-apis",
         description=jd("Vector Health", "Senior Software Engineer, APIs",
                        "Our API team connects clinics to the scheduling data they rely on.",
                        "Austin, TX (Hybrid, 2 days a week in office)",
                        ["Design REST APIs in Python", "Scale our PostgreSQL data layer",
                         "Partner with product on new integrations"],
                        ["5+ years of software engineering experience", "Python, ideally FastAPI",
                         "PostgreSQL", "Experience with event-driven systems such as Kafka"],
                        "$160,000 - $185,000"),
         posted=ago(hours=20, minutes=10), found=ago(hours=19, minutes=50), last_seen=ago(minutes=35),
         score=84.0, prescore=82.0, hire=0.70,
         reason=("Strong Python/FastAPI and PostgreSQL overlap with their API work, and the office is "
                 "in your city. Healthcare domain is new to you."),
         breakdown={"skills": {"score": 86, "note": "Python, FastAPI, PostgreSQL match"},
                    "experience": {"score": 85, "note": "Six years against a 5+ year ask"},
                    "location": {"score": 95, "note": "Hybrid in Austin, TX"},
                    "work_auth": {"score": 100, "note": "No sponsorship needed"}},
         signals=["fresh_posting_0d"]),
    dict(key="northpoint", source=JobSource.WORKDAY, external_id="R-20417", company="Northpoint Labs",
         title="Staff Engineer, Infrastructure", location="Remote (US)", remote=True,
         url="https://northpointlabs.wd1.myworkdayjobs.com/en-US/careers/job/R-20417",
         description=jd("Northpoint Labs", "Staff Engineer, Infrastructure",
                        "You'll lead the systems that move data between our services.", "Remote (US)",
                        ["Lead design of shared backend infrastructure", "Run Kafka at scale",
                         "Mentor senior engineers"],
                        ["8+ years of engineering experience", "Python or Go", "Kafka in production",
                         "Kubernetes"],
                        "$180,000 - $210,000"),
         posted=None, found=ago(days=2, hours=7), last_seen=ago(hours=1, minutes=10),
         score=79.0, prescore=76.0, hire=0.66,
         reason=("Your Kafka pipeline work maps to their infrastructure scope. Staff level asks for more "
                 "years than your six."),
         breakdown={"skills": {"score": 80, "note": "Python and Kafka match"},
                    "experience": {"score": 68, "note": "Staff level; the posting asks for 8+ years"},
                    "location": {"score": 100, "note": "Remote (US)"},
                    "work_auth": {"score": 100, "note": "No sponsorship needed"}},
         signals=["fresh_posting_2d"]),
    dict(key="cadence", source=JobSource.SMARTRECRUITERS, external_id="744000012345678", company="Cadence Robotics",
         title="Backend Engineer", location="Seattle, WA", remote=False,
         url="https://jobs.smartrecruiters.com/CadenceRobotics/744000012345678-backend-engineer",
         description=jd("Cadence Robotics", "Backend Engineer",
                        "Our fleet software coordinates warehouse robots in real time.",
                        "Seattle, WA (on-site)",
                        ["Build Python services for fleet coordination", "Stream robot telemetry through Kafka",
                         "Store and query telemetry in PostgreSQL"],
                        ["3+ years of backend development", "Python", "Kafka or another message broker",
                         "PostgreSQL"],
                        "$140,000 - $165,000"),
         posted=ago(days=3, hours=2), found=ago(days=3, hours=1, minutes=40), last_seen=ago(hours=1, minutes=40),
         score=76.0, prescore=74.0, hire=0.62,
         reason=("Python, Kafka and PostgreSQL all appear in their stack. The role is on-site in Seattle, "
                 "which you listed as a relocation target."),
         breakdown={"skills": {"score": 84, "note": "Python, Kafka, PostgreSQL match"},
                    "experience": {"score": 82, "note": "Six years against a 3+ year ask"},
                    "location": {"score": 60, "note": "On-site in Seattle, WA (relocation target)"},
                    "work_auth": {"score": 100, "note": "No sponsorship needed"}},
         signals=["fresh_posting_3d"]),
    dict(key="meridian", source=JobSource.WORKABLE, external_id="5E8A1C2B90", company="Meridian Freight",
         title="Senior Python Engineer", location="Remote (US)", remote=True,
         url="https://apply.workable.com/meridian-freight/j/5E8A1C2B90/",
         description=jd("Meridian Freight", "Senior Python Engineer",
                        "We build pricing and tracking tools for freight brokers.", "Remote (US)",
                        ["Ship Python services for shipment tracking", "Build FastAPI endpoints for partners",
                         "Work with PostgreSQL and Redis"],
                        ["5+ years of Python", "FastAPI or Django", "PostgreSQL", "Redis"],
                        "$145,000 - $170,000"),
         posted=ago(days=3, hours=20), found=ago(days=3, hours=19, minutes=30), last_seen=ago(hours=2, minutes=30),
         score=73.0, prescore=72.0, hire=0.60,
         reason=("Logistics domain and Python/FastAPI match your Brightwave work. Redis is not on your "
                 "resume."),
         breakdown={"skills": {"score": 76, "note": "Python, FastAPI, PostgreSQL match; Redis missing"},
                    "experience": {"score": 85, "note": "Six years against a 5+ year ask"},
                    "location": {"score": 100, "note": "Remote (US)"},
                    "work_auth": {"score": 100, "note": "No sponsorship needed"}},
         signals=["fresh_posting_3d"]),
    # The SUBMITTED application (Submitted tab + earliness receipt + outreach kit).
    dict(key="orchard", source=JobSource.GREENHOUSE, external_id="5530021", company="Orchard Systems",
         title="Backend Engineer, Payments", location="Remote (US)", remote=True,
         url="https://boards.greenhouse.io/orchardsystems/jobs/5530021",
         description=jd("Orchard Systems", "Backend Engineer, Payments",
                        "Our payments team moves money for thousands of small businesses.", "Remote (US)",
                        ["Build Python APIs for payment flows", "Publish payment events to Kafka",
                         "Keep PostgreSQL ledgers correct"],
                        ["4+ years of backend experience", "Python and FastAPI", "Kafka",
                         "PostgreSQL"],
                        "$155,000 - $180,000"),
         posted=ago(hours=30), found=ago(hours=29, minutes=49), last_seen=ago(minutes=50),
         score=86.0, prescore=84.0, hire=0.71,
         reason=("FastAPI APIs at 50k requests/day and a Kafka event pipeline map directly to their "
                 "payment APIs and events."),
         breakdown={"skills": {"score": 90, "note": "Python, FastAPI, Kafka, PostgreSQL match"},
                    "experience": {"score": 88, "note": "Six years against a 4+ year ask"},
                    "location": {"score": 100, "note": "Remote (US)"},
                    "work_auth": {"score": 100, "note": "No sponsorship needed"}},
         signals=["fresh_posting_1d"]),
]

from app.strategy.job_facets import compute as facets  # noqa: E402

ids: dict = {}
with get_session() as s:
    for j in JOBS:
        sal, spons, cap = facets(j["title"], j["description"], j["company"], j["url"], j["location"])
        blended = round(0.65 * j["score"] + 0.35 * j["hire"] * 100, 1)
        row = Job(
            user_id=None, source=j["source"], origin=j["source"].value, external_id=j["external_id"],
            company=j["company"], title=j["title"], location=j["location"], remote=j["remote"],
            url=j["url"], description=j["description"],
            posted_at=j["posted"], discovered_at=j["found"], first_seen=j["found"], last_seen=j["last_seen"],
            rerank_score=j["score"], rerank_reasoning=j["reason"],
            rerank_breakdown=json.dumps(j["breakdown"]),
            prescore=j["prescore"], prescored_at=j["found"] + timedelta(minutes=1),
            scored_at=j["found"] + timedelta(minutes=2),
            similarity_score=0.71, ghost_score=0.0, ghost_flags=None,
            hire_probability_score=j["hire"], hire_probability_signals=json.dumps(j["signals"]),
            blended_score=blended, on_role=True, job_type="full_time",
            salary_text=sal, sponsorship_json=spons, is_cap_exempt=cap,
            corporate_insights=json.dumps(j["insights"]) if j.get("insights") else None,
            eligibility="eligible",
        )
        s.add(row)
        s.flush()
        ids[j["key"]] = row.id
    s.commit()

# Applications: six on the board (SHORTLISTED), one SUBMITTED.
app_ids: dict = {}
with get_session() as s:
    for j in JOBS:
        delivered = j["found"] + timedelta(minutes=6)
        a = Application(user_id=None, job_id=ids[j["key"]], status=ApplicationStatus.SHORTLISTED,
                        apply_track="autofill", apply_url=j["url"],
                        created_at=delivered, updated_at=delivered)
        if j["key"] == "orchard":
            submitted = j["posted"] + timedelta(hours=3, minutes=12)
            a.status = ApplicationStatus.SUBMITTED
            a.submitted_at = submitted
            a.updated_at = submitted
            a.viewed_at = j["found"] + timedelta(minutes=20)
            a.tailored_at = j["posted"] + timedelta(hours=2, minutes=50)
            from app.strategy import earliness
            jrow = s.get(Job, ids["orchard"])
            earliness.stamp(a, jrow, now=submitted)      # the app's own receipt writer
        s.add(a)
        s.flush()
        app_ids[j["key"]] = a.id
    s.commit()

# ── background pool: obviously-synthetic postings that only feed the COUNTS ──
# (Pool tile, All Jobs badge, "N new postings (24h)"). Named "Sample Employer
# NNNN" at example.com so nothing can be mistaken for a real employer; none of
# them has an application, so none appears on the board, drawer or kit screens.
import random  # noqa: E402
from app.discovery.pipeline import SHARED_POOL_USER  # noqa: E402

POOL_TOTAL = int(os.environ.get("DEMO_POOL_TOTAL", "4800"))
POOL_24H = int(os.environ.get("DEMO_POOL_24H", "1240"))
rnd = random.Random(20261014)
_titles = ["Backend Engineer", "Software Engineer", "Senior Software Engineer", "Platform Engineer",
           "Data Engineer", "Site Reliability Engineer", "Full Stack Engineer", "Python Developer",
           "API Engineer", "Infrastructure Engineer"]
_locs = [("Remote (US)", True), ("Austin, TX", False), ("Denver, CO", False), ("Chicago, IL", False),
         ("New York, NY", False), ("Seattle, WA", False), ("Remote (US)", True)]
_srcs = [JobSource.GREENHOUSE, JobSource.LEVER, JobSource.ASHBY, JobSource.WORKDAY,
         JobSource.SMARTRECRUITERS, JobSource.WORKABLE]
with get_session() as s:
    batch = []
    for i in range(POOL_TOTAL):
        found = ago(minutes=rnd.randint(5, 24 * 60 - 5)) if i < POOL_24H else \
            ago(minutes=rnd.randint(24 * 60 + 5, 5 * 24 * 60 - 60))
        loc, remote = rnd.choice(_locs)
        batch.append(Job(user_id=SHARED_POOL_USER, source=rnd.choice(_srcs), external_id=f"demo-{i:05d}",
                         company=f"Sample Employer {i:04d}", title=rnd.choice(_titles), location=loc,
                         remote=remote, url=f"https://example.com/jobs/{i:05d}",
                         description="Synthetic background posting for the local demo pool.",
                         posted_at=found - timedelta(minutes=rnd.randint(3, 90)), discovered_at=found,
                         first_seen=found, last_seen=found, ghost_score=0.0))
        if len(batch) >= 1000:
            s.add_all(batch); s.commit(); batch = []
    if batch:
        s.add_all(batch); s.commit()
print("background pool rows:", POOL_TOTAL, "of which in the last 24h:", POOL_24H)

# ── hiring context for the hero posting (the app's own ingest code) ───────────
from app.discovery.base import EVIDENCE_TEAM_OR_DEPARTMENT, RawJob  # noqa: E402
from app.discovery import hiring_context as hc  # noqa: E402

lumen = next(j for j in JOBS if j["key"] == "lumen")
rj = RawJob(source="greenhouse", external_id=lumen["external_id"], company=lumen["company"],
            title=lumen["title"], location=lumen["location"], remote=True, url=lumen["url"],
            description=lumen["description"], posted_at=lumen["posted"])
hc.put(rj.context, "department", lumen["context"]["department"], EVIDENCE_TEAM_OR_DEPARTMENT,
       "departments[0].name")
hc.put(rj.context, "requisition_id", lumen["context"]["requisition_id"], EVIDENCE_TEAM_OR_DEPARTMENT,
       "requisition_id")
hc.put(rj.context, "ats", "greenhouse", EVIDENCE_TEAM_OR_DEPARTMENT, "source")
print("hiring context capture:", hc.capture([rj]))

# ── tailored documents for the hero application (written now, linked by phase.py) ──
TAILORED_MD = """# Maya Chen
Backend Engineer
Austin, TX | maya.chen@example.com | (555) 014-2290

## Summary
Backend engineer with six years of Python and FastAPI experience: APIs supporting 50k requests/day and a Kafka event pipeline for order updates.

## Experience
### Backend Engineer | Brightwave Logistics
Aug 2020 – Present
- Built FastAPI APIs in Python supporting 50k requests/day
- Ran a Kafka event pipeline for order updates

## Skills
Python, FastAPI, Kafka, PostgreSQL

## Education
### B.S. Computer Science
2019
"""

COVER = """Dear Lumen Data hiring team,

I'm applying for the Senior Backend Engineer role on your Platform Engineering team. For the past six years I've been a backend engineer at Brightwave Logistics, where I built FastAPI APIs supporting 50k requests a day and ran the Kafka event pipeline for our order updates.

Your posting centres on Python services and Kafka event pipelines. That matches my day-to-day work: building APIs in Python and FastAPI, and running an event pipeline on Kafka. PostgreSQL is part of my core toolkit as well.

I'd welcome the chance to talk about how I could help your platform team. Thank you for your time.

Maya Chen
maya.chen@example.com | (555) 014-2290
"""

from app.common.user_files import tailored_dir  # noqa: E402
from app.tailoring.render import TIERS, document_filename, fit_one_page, write_docx  # noqa: E402

out_dir = tailored_dir(app_ids["lumen"], DATA)
out_dir.mkdir(parents=True, exist_ok=True)
resume_path = out_dir / document_filename("Maya", "Chen", "", kind="Resume", ext="docx")
cover_path = out_dir / document_filename("Maya", "Chen", "", kind="Cover_Letter", ext="txt")
pdf_path = resume_path.with_suffix(".pdf")
fit = fit_one_page(TAILORED_MD, jd_text=LUMEN_JD, author="Maya Chen", title="Maya Chen Resume")
pdf_path.write_bytes(fit.pdf_bytes)
write_docx(fit.md, resume_path, TIERS[fit.tier_index], author="Maya Chen", title="Maya Chen Resume")
(out_dir / "resume.md").write_text(fit.md, encoding="utf-8")
cover_path.write_text(
    f"Company:   Lumen Data\nRole:      Senior Backend Engineer\n"
    f"Posted:    {lumen['posted'].strftime('%B %d, %Y')}\nURL:       {lumen['url']}\n\n---COVER---\n\n" + COVER,
    encoding="utf-8")

from app.tailoring.requirements import review as build_review  # noqa: E402
rv = build_review(MASTER_MD, fit.md, LUMEN_JD, company="Lumen Data")
(out_dir / "report.json").write_text(json.dumps({
    # SEEDED (no LLM ran): the Resume Doctor's numbers and read.
    "doctor_score": 91, "doctor_passed": True,
    "grounding_passed": True, "grounding_status": "passed",
    "attempts": 1, "variant": None, "ats_coverage_pct": None,
    "verdict": ("A focused one-page resume: the FastAPI and Kafka bullets answer the posting's two "
                "core asks directly, and every line traces to your master resume."),
    "weak_bullets": [], "banned_words": [], "integrity_issues": [],
    "human_score": 88, "human_passed": True, "fingerprint_flags": [], "fabrications": [],
    "locked_fields": ["B.S. Computer Science (2019)", "Brightwave Logistics, Aug 2020 – Present"],
    "removed_claims": [], "skills_to_learn": list(rv.missing_skills),
    "resume_pdf": pdf_path.name, "page_count": fit.pages, "fit_tier": fit.tier_index,
    "trimmed_bullets": list(fit.trimmed), "em_dashes_removed": 0,
    "headline": "Backend Engineer", "headline_source": "resume", "email_status": "kept",
    "generated_at": ago(minutes=40).isoformat(),
}), encoding="utf-8")

# ── outreach the user recorded on the SUBMITTED application (fictional) ─────
from app.intelligence.outreach import subject_for, subject_key  # noqa: E402
orchard = next(j for j in JOBS if j["key"] == "orchard")
subj = subject_for("hiring_manager", orchard["title"], orchard["company"])
with get_session() as s:
    s.add(OutreachMessage(
        user_id=None, application_id=app_ids["orchard"], channel="email", kind="hiring_manager",
        recipient_label="Payments engineering manager", recipient_email="payments-hiring@example.com",
        subject=subj, subject_key=subject_key(subj),
        body=("Hi, I'm reaching out about the Backend Engineer, Payments role at Orchard Systems. "
              "I build FastAPI APIs and run a Kafka event pipeline at Brightwave Logistics. "
              "Would you be up for a quick chat this week?"),
        created_at=ago(hours=25), sent_at=ago(hours=25),
        replied_at=ago(hours=3), reply_how="email_subject", reply_subject="Re: " + subj))
    s.add(OutreachMessage(
        user_id=None, application_id=app_ids["orchard"], channel="linkedin", kind="referral_who_owns",
        recipient_label="Platform engineer at Orchard Systems",
        body=("Hi, quick question if you have a second: do you know who owns the Backend Engineer, "
              "Payments req at Orchard Systems? Happy to take it from there myself."),
        created_at=ago(hours=24, minutes=30), sent_at=ago(hours=24, minutes=30)))
    s.commit()

state = {"app_ids": app_ids, "job_ids": ids, "seeded_at": NOW.isoformat(),
         "lumen_docs": {"resume": str(resume_path), "cover": str(cover_path), "pdf": str(pdf_path)},
         "seeded_llm_fields": SEEDED_LLM_FIELDS}
with open(os.path.join(LIVE, "seed_state.json"), "w") as f:
    json.dump(state, f, indent=2)

# ── self-check: what the pages will read ────────────────────────────────────
from app.tailoring.export_gate import evaluate  # noqa: E402
from app.autofill.answer_pack import _load_resume_text_from_path  # noqa: E402
from app.tailoring.ats_keywords import analyze  # noqa: E402
draft = _load_resume_text_from_path(str(resume_path))
v = evaluate(grounding_rejected=False, grounding_reason="", master=MASTER_MD, tailored=draft,
             jd=LUMEN_JD, company="Lumen Data")
print("export verdict:", v.code, v.reason, v.unconfirmed_claims)
print("review:", json.dumps(rv.as_dict(), indent=1)[:2500])
rep = analyze(LUMEN_JD, MASTER_MD, company="Lumen Data")
print("keywords matched:", rep.matched, "missing:", rep.missing, "cov:", rep.coverage_pct)
with get_session() as s:
    for j in s.exec(select(Job).where(Job.user_id.is_(None))).all():
        print(j.id, j.company, "|", j.salary_text, "|", (json.loads(j.sponsorship_json or "{}") or {}).get("badge"))
    for a in s.exec(select(Application)).all():
        print("app", a.id, a.job_id, a.status.value, a.applied_after_min, a.applied_ref)
print("fit pages", fit.pages, "tier", fit.tier_index, "pdf bytes", len(fit.pdf_bytes))
print("state:", state)
