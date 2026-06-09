import pytest
from app.qa_store.resolver import QAResolver
from app.db.models import Job

def test_qa_resolver_identity():
    resolver = QAResolver()
    
    # Test linkedin
    ans, conf = resolver.resolve("LinkedIn Profile")
    assert ans == "https://www.linkedin.com/in/amruthaluri/"
    assert conf >= 0.95
    
    # Test full name
    ans, conf = resolver.resolve("Please enter your full name:")
    assert ans == "Karthik Amruthaluri"
    assert conf >= 0.95

def test_qa_resolver_work_auth():
    resolver = QAResolver()
    
    ans, conf = resolver.resolve("Are you legally authorized to work in the United States?")
    assert ans == "Yes"
    assert conf >= 0.95
    
    ans, conf = resolver.resolve("Will you now or in the future require visa sponsorship?")
    assert ans == "No"
    assert conf >= 0.95

def test_qa_resolver_always_ask_human():
    resolver = QAResolver()
    
    ans, conf = resolver.resolve("Did you use AI to fill this application?")
    assert ans is None
    assert conf == 0.0

def test_qa_resolver_safety_checks():
    resolver = QAResolver()
    
    # Criminal record
    ans, conf = resolver.resolve("Have you ever been convicted of a felony?")
    assert ans == "No"
    assert conf >= 0.95

    # Non-compete
    ans, conf = resolver.resolve("Are you subject to a non-compete agreement?")
    assert ans == "No"
    assert conf >= 0.95

def test_qa_resolver_unknown_yes_no():
    resolver = QAResolver()
    
    # Unknown yes/no should yield low confidence (0.0) so it routes to Telegram
    ans, conf = resolver.resolve("Do you like pineapples on pizza?")
    assert ans is None
    assert conf < 0.7

def test_qa_resolver_education():
    resolver = QAResolver()
    
    # Test university
    ans, conf = resolver.resolve("What university did you attend?")
    assert ans == "University of Cincinnati"
    assert conf >= 0.95
    
    # Test graduation date
    ans, conf = resolver.resolve("Date of Graduation")
    assert ans == "April 30, 2026"
    assert conf >= 0.95

    # Test degree
    ans, conf = resolver.resolve("Please state your highest degree:")
    assert ans == "Master of Engineering"
    assert conf >= 0.95
