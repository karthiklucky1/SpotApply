"""Role-alias regressions: title variants that routing previously missed."""
from app.discovery.title_filter import role_title_match


def test_sde_matches_software_engineer_user():
    assert role_title_match("SDE II", ["software engineer"])
    assert role_title_match("Software Development Engineer - 1", ["software engineer"])
    assert role_title_match("Senior Software Engineer", ["sde"])


def test_react_matches_frontend_user_and_reverse():
    assert role_title_match("React Developer", ["frontend engineer"])
    assert role_title_match("Senior Frontend Engineer", ["react developer"])
    assert role_title_match("JavaScript Engineer", ["frontend engineer"])


def test_sre_devops_bidirectional():
    assert role_title_match("Site Reliability Engineer", ["devops engineer"])
    assert role_title_match("DevOps Engineer", ["site reliability engineer"])


def test_nurse_family():
    assert role_title_match("RN - ICU Nights", ["registered nurse"])
    assert role_title_match("Registered Nurse (Med/Surg)", ["rn"])


def test_unrelated_titles_still_rejected():
    assert not role_title_match("Bakery Production Manager", ["software engineer"])
    assert not role_title_match("VP, Physical Production", ["frontend engineer"])
    assert not role_title_match("Registered Nurse", ["software engineer"])


def test_ai_lab_and_startup_titles_are_software_roles():
    """'Member of Technical Staff, Evals' carries no role word; a Software /
    AI Engineer user never received Harvey's or Profound's postings
    (2026-10-29). Forward Deployed and Founding Engineer are the same market."""
    roles = ["Software Engineer", "Backend Engineer", "AI Full Stack Developer"]
    for title in ("Member of Technical Staff, Data & Evals", "MTS, Inference",
                  "Forward Deployed Engineer", "Forward-Deployed Engineer, Enterprise",
                  "Founding Engineer"):
        assert role_title_match(title, roles), title
    # A manufacturing "Product Engineer" is not software, and other fields
    # stay closed.
    assert not role_title_match("Product Engineering Technician (Seneca Battery)", roles)
    assert not role_title_match("Member of Technical Staff", ["Registered Nurse"])
