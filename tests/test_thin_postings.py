"""A posting with almost no text is not judged, and never reaches a board.

Audit of delivered jobs, 2026-11-20: "CPT-11716 Executive & Business Operations
Virtual Assistant" was on a board at 78 with a description of zero characters
(a Breezy row stored from the listing alone). The cheap pass advanced it at 60
("no description": it must not REJECT on nothing) and the final scorer, handed
a title, called it a strong match. Every cap that keeps a final honest reads
the posting's words — the years it asks for, a sponsorship refusal, the work
mode — so with no words there was nothing to hold the 78 to.

Pinned here:
  * ONE definition (`posting_text.too_thin`, `settings.min_posting_chars`):
    readable characters, markup and white space not counted;
  * the free pre-filter stamps such a posting BEFORE any model is asked, in
    both tiers, and takes no unit of the user's Tier-1 allowance;
  * `slate.place()` refuses one that was scored before the rule existed;
  * a short but real posting (a few sentences) is read as before.

The suite runs with the rule OFF (conftest: hundreds of fixtures use a
one-letter description); every test here turns it on. Rows are prefixed
``thin_`` and cleaned by that prefix.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest
from sqlmodel import delete, select

import app.analytics.spend as spend
import app.matching.reranker as rr
from app.common import posting_text as pt
from app.config import Settings, settings
from app.db.init_db import get_session
from app.db.models import Application, FunnelEvent, Job, JobSource
from app.matching import tier1_budget as t1
from app.matching.filters.rule_filter import THIN_POSTING_SCORE, RuleFilter
from app.strategy import slate

_P = "thin_"
UID = "thin_user"

#: A short posting that still says what the job is: 4 sentences, 330 characters.
SHORT_BUT_REAL = (
    "We are hiring a Backend Engineer to build payment APIs in Python and Go. "
    "You will own services end to end, from design review to on-call. "
    "We ask for 3+ years of backend experience and solid SQL. "
    "Remote in the United States, $140,000 to $165,000, with health cover and equity.")


@pytest.fixture(autouse=True)
def _rule_on(monkeypatch):
    monkeypatch.setattr(settings, "min_posting_chars",
                        Settings.model_fields["min_posting_chars"].default)
    yield


def test_the_production_default():
    assert Settings.model_fields["min_posting_chars"].default == 200
    assert pt.visible_chars(SHORT_BUT_REAL) > 200, "the short posting here must be a readable one"


# ── the one definition ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    None, "", "   \n\t  ", "Great role. Apply now!",
    "<p></p><p><br/></p>" * 40,                           # markup with nothing in it
    "&nbsp;&nbsp;&amp;&#160;" * 60,
    "Apply\n\n\n" + " " * 600 + "today.",                  # white space is not text
    "<div class=\"job\"><strong>Virtual Assistant</strong> wanted, apply now.</div>",
    "x" * 199,
])
def test_too_little_to_judge(text):
    assert pt.too_thin(text)
    assert pt.thin_reason(text).startswith("Posting text filtered: ")


@pytest.mark.parametrize("text", [
    SHORT_BUT_REAL,
    "<ul>" + "".join(f"<li>{line}</li>" for line in SHORT_BUT_REAL.split(". ")) + "</ul>",
    "x" * 200,
    "word " * 3000,
])
def test_enough_to_judge(text):
    assert not pt.too_thin(text)


def test_nothing_says_no_description_and_a_line_says_how_much():
    assert pt.thin_reason("") == "Posting text filtered: no description to judge the match from"
    assert "only 22 characters" in pt.thin_reason("Great role. Apply now!")


def test_zero_turns_the_rule_off(monkeypatch):
    monkeypatch.setattr(settings, "min_posting_chars", 0)
    assert not pt.too_thin("") and not pt.too_thin(None)


def test_a_long_posting_costs_no_more_to_measure_than_a_short_one():
    big = ("<p>" + "requirements " * 40 + "</p>") * 400          # ~215 KB of markup
    assert not pt.too_thin(big)
    assert pt.visible_chars(big) <= pt._MEASURED_CHARS
    started = time.perf_counter()
    for _ in range(200):
        pt.too_thin(big)
    assert time.perf_counter() - started < 0.5


# ── judged by nobody: the free pre-filter ────────────────────────────────────

def _posting(description, title="CPT-11716 Executive & Business Operations Virtual Assistant"):
    return Job(title=title, company="Thin Co", location="Worldwide", remote=True,
               description=description, source=JobSource.BREEZY,
               external_id=_P + "x1", url="https://thinco.breezy.hr/p/x1")


@pytest.mark.parametrize("description", ["", "Great role. Apply now!"])
def test_the_rule_filter_stamps_it_as_not_judged(description):
    res = RuleFilter(profile=None).filter(_posting(description))
    assert not res.passed and res.score_override == THIN_POSTING_SCORE
    assert res.reason.startswith("Posting text filtered: ")


def test_the_rule_filter_reads_a_short_but_real_posting():
    res = RuleFilter(profile=None).filter(_posting(SHORT_BUT_REAL, title="Backend Engineer"))
    assert not res.reason.startswith("Posting text filtered")


def test_off_means_the_old_path(monkeypatch):
    monkeypatch.setattr(settings, "min_posting_chars", 0)
    res = RuleFilter(profile=None).filter(_posting("", title="Backend Engineer"))
    assert not res.reason.startswith("Posting text filtered")


class _NeverCalled:
    """A client whose every call is a test failure."""
    def __init__(self):
        self.chat = self
        self.completions = self
        self.messages = self

    def create(self, **kw):
        raise AssertionError("no model may be asked about a posting with no text")


def _reranker(uid):
    rk = rr.Reranker.__new__(rr.Reranker)
    rk._profile = None
    rk._feedback = ""
    rk._user_id = uid
    rk._anthropic_client = _NeverCalled()
    rk._openai_client = _NeverCalled()
    rk._active_backend = "anthropic"
    return rk


def test_neither_tier_asks_a_model_and_nothing_is_spent():
    uid = _P + "spend"
    spend._BUFFER.clear()
    rk = _reranker(uid)
    job = _posting("")

    pre = rk.prescore("resume text", job)
    assert pre[0] == float(THIN_POSTING_SCORE) and pre[1].startswith("Posting text filtered")
    assert pre[0] < settings.shortlist_score_threshold, "the stamp is never an advance"

    score, reason, _concerns, _breakdown, meta = rk.score_with_meta("resume text", job)
    assert score == float(THIN_POSTING_SCORE) and reason.startswith("Posting text filtered")
    assert meta["provider"] == "rule"

    assert spend.pending_spend() == {}, "a call that did not happen records nothing"
    assert t1.used_today(uid) is None, "and takes no unit of the Tier-1 day"


# ── the board ────────────────────────────────────────────────────────────────

@pytest.fixture
def board(monkeypatch):
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 10)
    _wipe()
    yield
    _wipe()


def _wipe():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _scored(ext, description, score=78.0) -> int:
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.BREEZY, external_id=_P + ext, company=f"Thin {ext}",
                title="Executive & Business Operations Virtual Assistant", location="Worldwide",
                remote=True, url=f"https://thinco.breezy.hr/p/{ext}", description=description,
                rerank_score=score, first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _place(jid, score=78.0):
    with get_session() as s:
        p = slate.place(s, s.get(Job, jid), score, user_id=UID)
        s.commit()
        return p


def test_a_posting_scored_from_its_title_never_reaches_the_board(board):
    """The production row: no description, 78."""
    jid = _scored("blank", "")
    p = _place(jid)
    assert not p.created and p.outcome == slate.OUTCOME_THIN_POSTING
    with get_session() as s:
        assert s.exec(select(Application).where(Application.job_id == jid)).first() is None
        reasons = [e.reason for e in s.exec(select(FunnelEvent).where(
            FunnelEvent.job_id == jid, FunnelEvent.stage == slate.PLACEMENT_STAGE)).all()]
        assert reasons == ["thin_posting"]
        # Refusing is not closing: it keeps its score for All Jobs.
        j = s.get(Job, jid)
        assert not j.is_closed and j.rerank_score == 78.0


def test_the_board_never_fetches_a_description_to_run_this_check(board):
    """The check is free only where the text is already in hand. A row loaded
    without its description (the re-shortlist backstop's) is not checked:
    asking for it would pull a whole description per re-offer, every pass."""
    from sqlalchemy import event
    from sqlalchemy.orm import load_only

    from app.db.init_db import engine
    jid = _scored("partial", "")
    statements: list = []

    def _st(conn, cursor, statement, *a, **k):
        statements.append(statement.lower())

    with get_session() as s:
        job = s.exec(select(Job).options(load_only(
            Job.id, Job.url, Job.source, Job.company, Job.title, Job.rerank_score,
            Job.user_id, Job.external_id)).where(Job.id == jid)).first()
        event.listen(engine, "before_cursor_execute", _st)
        try:
            p = slate.place(s, job, 78.0, user_id=UID)
            s.commit()
        finally:
            event.remove(engine, "before_cursor_execute", _st)
    assert p.outcome != slate.OUTCOME_THIN_POSTING
    assert not any("job.description" in st for st in statements)


def test_a_short_but_real_posting_is_placed(board):
    assert _place(_scored("real", SHORT_BUT_REAL)).created


def test_off_restores_placing_them(board, monkeypatch):
    monkeypatch.setattr(settings, "min_posting_chars", 0)
    assert _place(_scored("off", "")).created
