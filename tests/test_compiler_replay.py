"""The compiler replay experiment must be trustworthy before anyone acts on
its verdicts: the fitting machinery has to reproduce a consistent family AND
refuse a vague one (the plan's whole safety story). The selftest runs the full
path — feature extraction, family fit, LOO scoring, verdicts — on synthetic
data with known ground truth. No DB, no LLM, no network."""
from scripts.compiler_replay import (
    family_key,
    grade_evidence,
    jd_requirements,
    selftest,
)


def test_selftest_end_to_end():
    summary = selftest()
    assert summary["selftest_pass"], summary["families"]
    verdicts = {f: r["verdict"] for f, r in summary["families"].items()}
    # The vague family MUST be rejected — shipping a confident rubric for
    # boilerplate is worse than not compiling at all (FM-5).
    assert verdicts["swe|mid"] == "KEEP-LLM"


def test_graded_evidence_separates_depth_from_listing():
    deep = grade_evidence("6 years of production python. python schedulers, "
                          "python APIs, python data tooling.")
    shallow = grade_evidence("Skills: Python, HTML, Excel")
    assert deep["python"] > shallow["python"]


def test_acceptance_set_credits_adjacent_skill():
    req = jd_requirements("We need FastAPI experience")
    ev = grade_evidence("Built async REST services with Flask")
    # Flask evidence partially satisfies a fastapi requirement (FM-3) —
    # nonzero credit, but less than the real thing would earn.
    assert req.get("fastapi") == 1.0
    assert 0 < ev.get("fastapi", 0) < 4.0


def test_family_key_buckets_title_variants_together():
    assert family_key("Senior Backend Engineer") == family_key("Sr. Back-End Developer (Python)") \
        or family_key("Senior Backend Engineer").startswith("backend|")
    assert family_key("Machine Learning Engineer II") .startswith("ml-eng|")
    assert family_key("Staff Site Reliability Engineer").startswith("platform|staff")
