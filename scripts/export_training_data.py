"""Export (résumé, job, LLM score) triples for distillation training.

Every authoritative LLM final score in the DB is a paid-for training example.
This script pulls them into a JSONL file that scripts/train_local_scorer.py
consumes (typically on a free Colab GPU). See docs/DISTILLATION.md.

    python -m scripts.export_training_data --out data/training/scoring_distill.jsonl

LABEL QUALITY: only genuine LLM finals are exported. Rows stamped by the cheap
gates carry scores a distilled model must NOT learn from:
  - ghost filter          → reasoning starts "Ghost filtered"
  - Tier-1 prescore drain → reasoning starts "Pre-screened (Tier-1"
  - door filter           → reasoning starts "Wrong Door:"
  - embedding gate        → reasoning starts "Embedding filtered:"
  - rule filter           → has a breakdown but every factor note is empty
                            (only _parse_response writes real notes)

Résumés are exported as-is — the file contains user PII and is meant for the
operator's own model training. Keep it out of git (data/ is ignored) and
delete after training.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)

_CHEAP_GATE_PREFIXES = (
    "Ghost filtered", "Pre-screened (Tier-1", "Wrong Door:", "Embedding filtered:",
)


def is_llm_final(reasoning: str | None, breakdown_json: str | None) -> bool:
    """True when a job's stored score came from an authoritative LLM final."""
    r = (reasoning or "").strip()
    if not r:
        return False
    if any(r.startswith(p) for p in _CHEAP_GATE_PREFIXES):
        return False
    if not breakdown_json:
        return False
    try:
        bd = json.loads(breakdown_json)
        # Rule-filter rows synthesize a breakdown with empty notes; a real LLM
        # final carries at least one written factor note.
        return any((f or {}).get("note", "").strip() for f in bd.values()
                   if isinstance(f, dict))
    except Exception:
        return False


_CHUNK = 300  # rows per query — full-table single selects hit Supabase's statement timeout


def _scored_chunks(max_rows: int = 0):
    """Keyset-paginated scored jobs, only the columns the export needs — one
    small indexed query at a time so no statement can hit the DB timeout."""
    from sqlalchemy.orm import load_only
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import Job

    last_id, seen = 0, 0
    while True:
        with get_session() as session:
            chunk = session.exec(
                select(Job)
                .options(load_only(
                    Job.id, Job.user_id, Job.title, Job.company, Job.location,
                    Job.remote, Job.description, Job.rerank_score,
                    Job.rerank_reasoning, Job.rerank_breakdown,
                ))
                .where(Job.rerank_score != None, Job.id > last_id)  # noqa: E711
                .order_by(Job.id)
                .limit(_CHUNK)
            ).all()
        if not chunk:
            return
        for job in chunk:
            yield job
            seen += 1
            if max_rows and seen >= max_rows:
                return
        last_id = chunk[-1].id


def export(out_path: str, max_rows: int = 0) -> dict:
    from app.matching.pipeline import _load_resume

    stats: Counter = Counter()
    resumes: dict = {}

    def _resume_for(uid) -> str | None:
        if uid not in resumes:
            try:
                uid_arg = None if (not uid or uid == "local") else uid
                resumes[uid] = (_load_resume(user_id=uid_arg) or "").strip() or None
            except Exception:
                resumes[uid] = None
        return resumes[uid]

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w") as fh:
        for job in _scored_chunks(max_rows=0):
            stats["scored_rows"] += 1
            if written % 500 == 0 and written:
                log.info("  ...%d exported so far (%d rows scanned)", written, stats["scored_rows"])
            if not is_llm_final(job.rerank_reasoning, job.rerank_breakdown):
                stats["skipped_cheap_gate"] += 1
                continue
            resume = _resume_for(job.user_id)
            if not resume:
                stats["skipped_no_resume"] += 1
                continue
            fh.write(json.dumps({
                # Rich slices — training decides how much to feed the model.
                "resume": resume[:16000],
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "remote": bool(job.remote),
                "description": (job.description or "")[:5000],
                "score": float(job.rerank_score),
                "breakdown": job.rerank_breakdown,
                "user": hashlib.sha256(str(job.user_id or "local").encode()).hexdigest()[:12],
                "job_id": job.id,
            }) + "\n")
            written += 1
            if max_rows and written >= max_rows:
                break
    stats["exported"] = written
    return dict(stats)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/training/scoring_distill.jsonl")
    ap.add_argument("--max-rows", type=int, default=0, help="0 = all")
    args = ap.parse_args()
    stats = export(args.out, args.max_rows)
    log.info("Export complete → %s", args.out)
    for k, v in sorted(stats.items()):
        log.info("  %s: %s", k, v)
    if stats.get("exported", 0) < 2000:
        log.info("NOTE: <2K examples — consider padding with the free HF datasets "
                 "(see docs/DISTILLATION.md) before training.")


if __name__ == "__main__":
    main()
