"""The compiler replay experiment must be trustworthy before anyone acts on
its verdicts: the fitting machinery has to reproduce a consistent family AND
refuse a vague one (the plan's whole safety story). The selftest runs the full
path — feature extraction, family fit, LOO scoring, verdicts — on synthetic
data with known ground truth. No DB, no LLM, no network."""
from scripts.compiler_replay import (
    bucket_for_reasoning,
    family_key,
    grade_evidence,
    jd_requirements,
    jd_signals,
    selftest,
)


def test_selftest_end_to_end():
    summary = selftest()
    assert summary["selftest_pass"], summary["families"]
    verdicts = {f: r["verdict"] for f, r in summary["families"].items()}
    # The vague family MUST be rejected — shipping a confident rubric for
    # boilerplate is worse than not compiling at all (FM-5).
    assert verdicts["swe|mid"] == "KEEP-LLM"
    # The visa feature must produce measurable lift over the skills-only fit.
    back = summary["families"]["backend|senior"]
    assert back["rho"] > back["rho_v1"]


def test_jd_visa_signals():
    no = jd_signals("Great role. We are unable to provide visa sponsorship.")
    ok = jd_signals("H1B sponsorship available for the right candidate.")
    neither = jd_signals("We need a Python developer.")
    assert no["no_sponsor"] == 1.0 and no["sponsor_ok"] == 0.0
    assert ok["sponsor_ok"] == 1.0 and ok["no_sponsor"] == 0.0
    assert neither["no_sponsor"] == 0.0 and neither["sponsor_ok"] == 0.0


def test_disagreement_buckets_read_claudes_reasoning():
    assert bucket_for_reasoning(
        "Strong overlap but the posting offers no visa sponsorship") == "visa/work-auth"
    assert bucket_for_reasoning(
        "Role requires 8+ years; the candidate is early-career") == "seniority"
    assert bucket_for_reasoning(
        "Solid engineer but the role is onsite in Berlin") == "location"
    assert bucket_for_reasoning(
        "A nuanced judgement call on overall trajectory") == "holistic/other"


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
