import pytest
from app.tailoring.grounding import GroundingChecker

def test_grounding_checker():
    source = """
# Karthik Amruthaluri

## PROFESSIONAL EXPERIENCE
- Built a machine learning model using PyTorch to predict user churn with 92% accuracy.
- Managed a team of 3 developers to deliver a React Native app.
"""
    tailored = """
# Karthik Amruthaluri

## PROFESSIONAL EXPERIENCE
- Created a PyTorch-based ML model predicting churn with 92% accuracy.
- Led and mentored a cross-functional group of three engineering peers in launching a React mobile app.
- Invented a new quantum computing chip that solves NP-hard problems in 1 second.
"""
    checker = GroundingChecker()
    result = checker.check(source, tailored)
    
    # Assert that it detected the fabrication
    assert not result.passed
    assert len(result.flagged_bullets) == 1
    assert "quantum" in result.flagged_bullets[0]["bullet"].lower()
