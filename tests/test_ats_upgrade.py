"""Guard: the aggregator -> direct-ATS upgrade keeps the right link AND date.

Two silent bugs lived here. The eligible-source set named only five of the ATSs
we scrape directly, so a Workable/Recruitee/BambooHR posting could never displace
an aggregator row — we kept the aggregator's link even while holding the
employer's own. And the upgrade copied the URL and description but not
``posted_at``, so it discarded the ATS's unfalsifiable publish date at the exact
moment we finally had it, leaving the aggregator's reset-on-repost date to drive
freshness ranking.

See docs/research/hiring-machine-2026-08.md §1.1 and §1.2.
"""
from app.db.models import JobSource


def _direct_ats_sources():
    """The set as the pipeline builds it, read back out of the module source.

    The set is a local inside upsert_jobs, so it cannot be imported. Parsing it
    is deliberate: it keeps this guard honest about the ACTUAL literal rather
    than a copy that could drift.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/discovery/pipeline.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "direct_ats_sources":
            return {n.attr for n in ast.walk(node.value) if isinstance(n, ast.Attribute)}
    raise AssertionError("direct_ats_sources literal not found in pipeline.py")


# Sources that are aggregators/boards, not an employer's own ATS. Everything in
# JobSource that is not one of these (and not MANUAL) is a direct ATS.
_AGGREGATORS = {
    "LINKEDIN", "INDEED", "INDEEDRSS", "SERPAPI", "REMOTIVE", "REMOTEOK", "THEMUSE",
    "ARBEITNOW", "JOBICY", "WEWORKREMOTELY", "ADZUNA", "JOOBLE", "REED",
    "WELLFOUND", "OTTA", "MANUAL",
    # Not an aggregator, but not an employer's ATS either: postings captured by
    # the browser extension, whose URL is whatever page the user was on.
    "CROWDSOURCED",
}


def test_every_directly_scraped_ats_can_upgrade_an_aggregator_row():
    declared = _direct_ats_sources()
    missing = []
    for src in JobSource:
        name = src.name
        if name in _AGGREGATORS or name in declared:
            continue
        missing.append(name)
    assert not missing, (
        "these ATSs are scraped directly but cannot upgrade an aggregator row: "
        f"{sorted(missing)} — add them to direct_ats_sources in pipeline.py"
    )


def test_the_upgrade_copies_the_ats_posting_date():
    """The ATS date must win, because the aggregator's is reset on every repost."""
    import pathlib
    import re

    src = pathlib.Path("app/discovery/pipeline.py").read_text()
    block = src[src.index("Upgrading cross-source job"):]
    block = block[: block.index("session.add(existing_cross)")]
    assert re.search(r"existing_cross\.posted_at\s*=\s*r\.posted_at", block), (
        "the aggregator -> ATS upgrade no longer copies posted_at; the aggregator's "
        "fabricated date would survive and drive freshness ranking"
    )
