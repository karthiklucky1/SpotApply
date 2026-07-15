"""All-sectors coverage: the cold-start keyword list and the keyless multi-sector
source (The Muse) must not be pinned to software/tech, or non-software candidates
(mechanical, civil, nursing, finance, sales…) get an empty board.
"""
from __future__ import annotations

from app.config import settings
from app.discovery.sources.themuse import _CATEGORIES


def _has_any(haystack_lower: str, needles) -> bool:
    return any(n in haystack_lower for n in needles)


def test_default_keywords_span_multiple_sectors():
    kws = " , ".join(settings.jobs_keywords_list).lower()
    # Software is fine to include, but the fallback must also reach other fields.
    assert _has_any(kws, ["mechanical", "civil", "electrical", "manufacturing"])  # engineering trades
    assert _has_any(kws, ["nurse", "healthcare"])                                  # healthcare
    assert _has_any(kws, ["financial", "accountant", "finance"])                   # finance
    assert _has_any(kws, ["sales", "marketing"])                                   # go-to-market
    # And it should be a reasonably broad list, not a handful of tech titles.
    assert len(settings.jobs_keywords_list) >= 15


def test_themuse_categories_cover_non_tech():
    cats = " | ".join(_CATEGORIES).lower()
    assert "engineering" in cats
    assert "healthcare" in cats
    assert "finance" in cats
    assert "sales" in cats
    assert "marketing" in cats
    # Still keeps tech.
    assert "software engineering" in cats
