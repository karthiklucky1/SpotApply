"""The rejection-reason classifier must match the strings the code really writes.

This report is only useful if its buckets correspond to what production stores.
The strings below are copied from the writers — app/matching/filters/rule_filter.py
for the gates, scoring_lane._stamp_job for the wrappers — so if a reason is
reworded there and not here, this fails instead of the histogram quietly
collapsing into "unclassified".

The prefix case is the one that actually went wrong while this was being built:
a RuleFilter rejection does NOT bypass Tier-1. ``Reranker.prescore`` calls
``_pre_filter_job`` first and returns its verdict AS the prescore, so the stored
text is "Pre-screened (Tier-1 fit 10): Sponsorship pre-filtered: ...". A
classifier that matched the Tier-1 prefix first filed every structured gate
rejection under "model drain" and reported one opaque bucket.
"""
from __future__ import annotations

import pytest

from scripts.funnel_diagnosis import _classify

# (stored reasoning, is_drain, expected bucket)
CASES = [
    # Bare gate strings (as rule_filter.py writes them).
    ("Sponsorship pre-filtered: matches 'no visa sponsorship' (US)",
     True, "work auth / sponsorship"),
    ("Experience pre-filtered: requires 10+ years (candidate has 4)",
     True, "experience (years)"),
    ("Title pre-filtered: 'Staff Engineer' is a senior/staff-level role",
     True, "seniority (title)"),
    ("Salary too low: up to $85,000 (target floor $130,000)",
     True, "salary out of band"),
    ("Salary too high: starts at $400,000 (target ceiling $250,000)",
     True, "salary out of band"),
    ("Internship filtered: user did not opt into internships",
     True, "internship / job type"),
    ("Full-time filtered: user wants internships only",
     True, "internship / job type"),
    ("Hire-probability: C++/Rust listed as required — not in candidate stack",
     True, "stack mismatch (C++/Rust)"),
    ("Hire-probability: GPU/kernel/compiler systems role — not in candidate stack",
     True, "stack mismatch (GPU/systems)"),
    ("Hire-probability: pure research role requiring publications/PhD",
     True, "research / PhD required"),
    ("Embedding similarity 0.204 vs threshold 0.35", True, "embedding similarity"),
    ("Ghost filtered (score=0.81): no_apply_link, reposted_many_times",
     False, "ghost / fake posting"),

    # THE PREFIX CASE: the same gates as production actually stores them.
    ("Pre-screened (Tier-1 fit 10): Sponsorship pre-filtered: matches 'no sponsorship'",
     True, "work auth / sponsorship"),
    ("Pre-screened (Tier-1 fit 10): Experience pre-filtered: requires 12+ years",
     True, "experience (years)"),
    ("Pre-screened (Tier-1 fit 10): Hire-probability: pure research role requiring a PhD",
     True, "research / PhD required"),

    # A genuine model drain: nothing under the prefix matches a hard rule.
    ("Pre-screened (Tier-1 fit 28): adjacent role, core stack only partially overlaps",
     True, "tier-1 model drain (no hard rule)"),

    # A Tier-2 final carries free-form reasoning and is not a rejection at all.
    ("Solid overlap on Python/SQL; missing Kubernetes depth",
     False, "tier-2 final (scored, not rejected)"),
]


@pytest.mark.parametrize("reasoning,is_drain,expected", CASES)
def test_reason_is_classified(reasoning, is_drain, expected):
    bucket, _fit = _classify(reasoning, is_drain)
    assert bucket == expected, f"{reasoning!r} -> {bucket!r}"


def test_tier1_fit_is_extracted_from_the_prefix():
    _b, fit = _classify("Pre-screened (Tier-1 fit 33): adjacent role", True)
    assert fit == 33
    # ...and a gate rejection carried through Tier-1 still reports its fit.
    _b, fit = _classify(
        "Pre-screened (Tier-1 fit 10): Salary too low: up to $70,000", True)
    assert fit == 10


def test_missing_reasoning_is_reported_not_guessed():
    assert _classify(None, True)[0] == "tier-1 drain, reason not recorded"
    assert _classify("", False)[0] == "final scored, no reason recorded"


def test_every_bucket_label_is_reachable():
    """A pattern that can never match is dead weight in the report."""
    from scripts.funnel_diagnosis import _REASON_RULES
    reached = {_classify(r, d)[0] for r, d, _e in CASES}
    for label, _rx in _REASON_RULES:
        if label in ("location / remote", "other hire-probability",
                     "degraded / local scorer"):
            continue  # exercised by the patterns above them; no literal here
        assert label in reached, f"no case covers bucket {label!r}"
