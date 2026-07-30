"""Source-level guards for the three incidents whose unit tests don't prevent them.

Each of these has a good unit-tested helper AND a post-mortem, and the gap
between them is the same every time: the helper works, but nothing stops the next
piece of code from simply not using it.

  * test_memory_guards.py proves browser_slot serializes correctly — it cannot
    prove every Chromium launch goes through it (docs/MEMORY.md: unbounded
    concurrent headless browsers, ~400MB each, OOM kill).
  * The matcher's _MODEL_CACHE holds one MiniLM — nothing stopped
    GroundingChecker.__init__ from building a second one per tailor request.
  * test_retrieval_egress.py proves _candidate_columns truncates — it cannot
    prove a hot path uses it instead of select(Job) (docs/CAPACITY.md: full
    descriptions put Supabase at 205% of egress quota on 2 MB of stored data).

These are greps, and they all pass today, so they lock in the current state
rather than describing an aspiration.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"


def _py_files():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _hits(pattern: str, *, skip: set[str] = frozenset()) -> list[tuple[Path, int, str]]:
    rx = re.compile(pattern)
    out = []
    for p in _py_files():
        if str(p.relative_to(APP.parent)) in skip:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if rx.search(line):
                out.append((p, n, line.strip()))
    return out


# ── OOM: every Playwright launch is bounded ──────────────────────────────────

def test_every_browser_launch_goes_through_the_bounded_slot():
    """docs/MEMORY.md. Each headless Chromium is a ~400MB child process charged to
    the container but invisible in our own RSS, so unbounded concurrency read as
    'plenty of memory' right up to the OOM kill. app/common/browser.py owns the
    semaphore; every other launch site must be inside a browser_slot block."""
    launches = _hits(r"\b(chromium|firefox|webkit)\.launch\s*\(",
                     skip={"app/common/browser.py"})
    unguarded = []
    for path, lineno, line in launches:
        lines = path.read_text(encoding="utf-8").splitlines()
        window = "\n".join(lines[max(0, lineno - 12):lineno + 2])
        if "browser_slot" not in window:
            unguarded.append(f"{path.relative_to(APP.parent)}:{lineno}: {line}")
    assert not unguarded, (
        "Playwright launch(es) outside browser_slot — each is ~400MB of "
        "container memory that our RSS metrics cannot see:\n  "
        + "\n  ".join(unguarded)
        + "\nWrap in `async with browser_slot():` (app/common/browser.py), or for "
          "stateless render/search use app.common.browser_client instead."
    )


def test_there_is_at_least_one_launch_site_to_check():
    """Guard the guard: a renamed API must not make the test above vacuous."""
    assert _hits(r"\b(chromium|firefox|webkit)\.launch\s*\("), (
        "no Playwright launch calls found at all — either the API changed or this "
        "test is now checking nothing")


# ── OOM: exactly one embedding model per process ─────────────────────────────

def test_ml_models_are_only_constructed_in_the_matchers_cache():
    """The defect this encodes: GroundingChecker.__init__ built a fresh
    SentenceTransformer on every user-triggered tailor — model weights plus a
    torch graph, ~150-200MB of transient allocation each, concurrent with the
    matcher's own copy. matcher._MODEL_CACHE is the single owner."""
    allowed = {"app/matching/matcher.py"}
    hits = [f"{p.relative_to(APP.parent)}:{n}: {ln}"
            for p, n, ln in _hits(r"\b(SentenceTransformer|CrossEncoder)\s*\(")
            if str(p.relative_to(APP.parent)) not in allowed]
    assert not hits, (
        "ML model constructed outside matcher._MODEL_CACHE:\n  " + "\n  ".join(hits)
        + "\nUse matcher._get_embed_model() / the matcher's cross-encoder accessor — "
          "a second copy is 150-200MB in a container that also holds FAISS, all "
          "lanes and Chromium."
    )


# ── egress: no whole-entity Job reads on a hot path ──────────────────────────

_HOT_PATHS = (
    "app/matching/matcher.py",
    "app/matching/pipeline.py",
    "app/strategy/scoring_lane.py",
    "app/strategy/pulse_lane.py",
    "app/strategy/hot_lane.py",
    "app/strategy/adoption.py",
)

# Whole-entity reads that are legitimately bounded (single row by id, or a write
# path that must mutate the ORM object). Each entry is file:line-ish context, kept
# as an explicit list so adding one is a decision rather than an accident.
_ALLOWED_SELECT_JOB: set[str] = set()


def _unprojected_job_selects(path: Path, rel: str) -> list[str]:
    """AST, not grep: find `select(Job)` statements with no column projection.

    A line-based match cannot tell three things apart that all contain the same
    characters — prose in a docstring explaining the old bug, `select(Job.id)`
    (already a projection), and `select(Job).options(load_only(...))` (also a
    projection, just spelled differently). Only the last form is what the incident
    was about, so the check has to understand the expression, not the line.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []

    def _is_bare_select_job(node) -> bool:
        return (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "select"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "Job")

    for node in ast.walk(tree):
        if not _is_bare_select_job(node):
            continue
        # Walk the enclosing statement and accept any explicit projection.
        stmt = _enclosing_statement(tree, node)
        projected = any(
            getattr(c.func, "attr", None) == "load_only"
            or getattr(c.func, "id", None) == "load_only"
            for c in ast.walk(stmt) if isinstance(c, ast.Call)
        )
        key = f"{rel}:{node.lineno}"
        if not projected and key not in _ALLOWED_SELECT_JOB:
            offenders.append(f"{key}: select(Job) with no load_only projection")
    return offenders


def _enclosing_statement(tree, target):
    """The innermost ast.stmt containing `target` — i.e. the whole select chain."""
    import ast as _ast
    best = tree
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.stmt):
            continue
        if any(sub is target for sub in _ast.walk(node)):
            if best is tree or getattr(node, "lineno", 0) >= getattr(best, "lineno", 0):
                best = node
    return best


@pytest.mark.parametrize("rel", _HOT_PATHS)
def test_hot_paths_never_select_whole_job_rows(rel):
    """docs/CAPACITY.md. Job.description is the biggest column in the schema and
    nothing downstream reads past ~800 chars, but `select(Job)` ships all of it
    for every candidate row, on every pass, for every user. That is how 2 MB of
    stored data became 205% of the egress quota. Retrieval and the FAISS rebuild
    project matcher._candidate_columns() instead; other hot-path reads use
    .options(load_only(...))."""
    path = APP.parent / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    offenders = _unprojected_job_selects(path, rel)
    assert not offenders, (
        "whole-entity Job read on a hot path — this ships every description byte "
        "on every pass:\n  " + "\n  ".join(offenders)
        + "\nProject the columns you need — select(Job.id, ...) or "
          "select(Job).options(load_only(...)) — or add an explicit entry to "
          "_ALLOWED_SELECT_JOB with a reason."
    )


def test_the_projection_detector_actually_detects():
    """Guard the guard, since a silently-broken AST walk would pass everything."""
    import tempfile
    src = (
        "from sqlmodel import select\n"
        "from sqlalchemy.orm import load_only\n"
        "class Job: pass\n"
        "a = select(Job).where(1)\n"                                # offender
        "b = select(Job).options(load_only(Job.id)).where(1)\n"     # projected
        "c = select(Job.id).where(1)\n"                             # projected
        "d = 'a docstring mentioning select(Job) in prose'\n"       # prose
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src)
        p = Path(f.name)
    found = _unprojected_job_selects(p, "probe.py")
    assert len(found) == 1, f"expected exactly the line-4 offender, got {found}"
    assert ":4:" in found[0]


def test_the_candidate_column_projection_still_exists():
    """If the helper is renamed, the test above silently stops meaning anything."""
    from app.matching import matcher
    assert hasattr(matcher, "_candidate_columns"), (
        "matcher._candidate_columns is gone — the egress guard above references a "
        "helper that no longer exists")


# ── tenancy: the fail-open scoping idiom stays out of new lane code ──────────

def test_lane_code_never_scopes_on_a_possibly_none_user_without_saying_so():
    """`if uid and uid != "local"` is fail-OPEN: a None uid drops the filter and
    the query spans every tenant. It is legitimate in request handlers that have
    already refused anonymous callers (see tests/test_route_auth_inventory.py), but
    background lanes have no request to refuse — they must handle None explicitly."""
    lanes = [p for p in _py_files()
             if p.parent.name == "strategy" or p.name in ("pipeline.py", "adoption.py")]
    offenders = []
    for p in lanes:
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if re.search(r'uid and uid != ["\']local["\']', s):
                offenders.append(f"{p.relative_to(APP.parent)}:{n}: {s}")
    assert not offenders, (
        "fail-open tenant scoping in background-lane code, which has no request to "
        "reject:\n  " + "\n  ".join(offenders))
