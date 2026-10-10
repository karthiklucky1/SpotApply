"""Email sync accuracy, measured (2026-10-10).

Production had 84 applications imported from the inbox; a 50-row sample named
the real employer in NONE of them (the sender's domain was taken as the
company: "Linkedin", "Greenhouse-mail", "Workable", "Clearcompany", "Jobs2web"),
and job alerts, a sign-in link, a promo and profile-view notices were imported
as applications. Matching was a raw substring over every application, SKIPPED
included, first hit wins.

This file is the labelled corpus the classifier is measured against — subject,
sender, display name and the inbox preview snippet (the sync never sees a body)
for the email shapes the main ATSes and boards actually send. The legacy logic
is reproduced below so the BEFORE and AFTER numbers come from the same rows.
Synthetic emails only; no real person is named.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from app.intelligence import email_signals as es

# (subject, sender_email, sender_name, snippet, expected_kind, expected_company)
CORPUS = [
    # ── acknowledgements from ATS / board mailers: the company is in the subject
    ("Thank you for applying to Clearstory", "no-reply@workablemail.com", "Workable",
     "Thanks for applying to Clearstory. We'll review your application and get back to you.", "ack", "Clearstory"),
    ("KARTHIK, your application was sent to Cooler Master USA", "jobs-noreply@linkedin.com", "LinkedIn",
     "Your application was sent to Cooler Master USA. You applied for Software Engineer.", "ack", "Cooler Master USA"),
    ("Your application to Neuralink", "no-reply@us.greenhouse-mail.io", "Neuralink",
     "Thank you for your interest in Neuralink. We have received your application for Software Engineer.", "ack", "Neuralink"),
    ("Neuralink", "no-reply@us.greenhouse-mail.io", "Neuralink",
     "Thank you for applying to Neuralink! We received your application.", "ack", "Neuralink"),
    ("Thank you for your application - Stripe", "noreply@myworkday.com", "Stripe",
     "Your application has been received. Thank you for applying to Stripe.", "ack", "Stripe"),
    ("We received your application", "careers@stripe.com", "Stripe Careers",
     "Thanks for applying to the Backend Engineer role at Stripe.", "ack", "Stripe"),
    ("Application received: Data Engineer", "talent@acme-corp.com", "Acme Corp Talent",
     "We have received your application for Data Engineer and will be in touch.", "ack", "Acme Corp"),
    ("Thanks for applying to Figma!", "no-reply@hire.lever.co", "Figma",
     "We've received your application for Product Designer and will be in touch.", "ack", "Figma"),
    ("Your application for Senior Software Engineer at Databricks", "no-reply@ashbyhq.com", "Databricks",
     "Thanks for applying! We will review your application shortly.", "ack", "Databricks"),
    ("Application Confirmation - Microsoft", "noreply@microsoft.com", "",
     "Your application for Software Engineer II has been submitted.", "ack", "Microsoft"),
    ("Successfully applied: Machine Learning Engineer at Scale AI", "noreply@indeed.com", "Indeed Apply",
     "You applied to Scale AI. The employer will review your application.", "ack", "Scale AI"),
    ("You applied to Datadog", "jobs-noreply@linkedin.com", "LinkedIn",
     "Your application to Datadog was sent. Good luck!", "ack", "Datadog"),
    ("Thank you for applying to Amazon", "no-reply@amazon.jobs", "Amazon Jobs",
     "We received your application for Software Development Engineer.", "ack", "Amazon"),
    ("Your application has been received - Google", "noreply@google.com", "Google Careers",
     "Thanks for applying to Google! Your application for Software Engineer III was received.", "ack", "Google"),
    ("Thank you for applying to Clearstory", "Workable", "",            # Outlook: display name only
     "Thanks for applying to Clearstory. We'll review your application.", "ack", "Clearstory"),
    ("Thank you for your application", "noreply@myworkday.com", "",
     "Thank you for applying to the Software Engineer position at NVIDIA. Your application has been received.", "ack", "NVIDIA"),
    ("Your application was sent to Mondo", "LinkedIn", "",
     "Your application was sent to Mondo for AI Engineer.", "ack", "Mondo"),
    ("We have recieved your application for AI Engineer job", "recruiting@mondo.com", "Mondo",
     "Thank you! We have recieved your application and a recruiter will review it.", "ack", "Mondo"),
    ("Your application to Cloudflare", "no-reply@us.greenhouse-mail.io", "Cloudflare",
     "We have received your application. Our process includes a phone screen, a technical interview and a final round. We will reach out if your experience matches.", "ack", "Cloudflare"),
    ("Thanks for your interest in Zapier", "no-reply@us.greenhouse-mail.io", "Zapier",
     "Hello! We're still reviewing applications and will be in touch.", "ack", "Zapier"),
    ("Your Indeed application: Data Analyst at Kroger", "indeedapply@indeed.com", "Indeed Apply",
     "Your application has been sent to Kroger.", "ack", "Kroger"),
    # ── rejections
    ("Update on your application to Airbnb", "no-reply@us.greenhouse-mail.io", "Airbnb",
     "Thank you for your interest in Airbnb. After careful consideration, we have decided to move forward with other candidates.", "rejection", "Airbnb"),
    ("Your application with Snowflake", "noreply@myworkday.com", "Snowflake",
     "Unfortunately, we will not be moving forward with your candidacy at this time.", "rejection", "Snowflake"),
    ("Re: Software Engineer - Phone Screen", "alice@notion.so", "Alice Chen",
     "We enjoyed the phone screen, but we won't be proceeding to the next round.", "rejection", "Notion"),
    ("Your application to Reddit", "no-reply@hire.lever.co", "Reddit",
     "We regret to inform you that we have chosen another candidate.", "rejection", "Reddit"),
    ("Thank you for applying to Shopify", "noreply@shopify.com", "Shopify Talent Acquisition",
     "We have decided not to move forward with your application. We wish you the best.", "rejection", "Shopify"),
    ("An update from Coinbase", "no-reply@ashbyhq.com", "Coinbase",
     "We won't be moving forward. We encourage you to apply for future openings.", "rejection", "Coinbase"),
    ("Application status: Backend Engineer at Plaid", "noreply@plaid.com", "Plaid",
     "Thank you for your interest, however we will not be proceeding with your application.", "rejection", "Plaid"),
    ("Thank you — Lyft", "no-reply@smartrecruiters.com", "Lyft",
     "The position has been filled. We will keep your resume on file.", "rejection", "Lyft"),
    ("Your candidacy at Uber", "noreply@uber.com", "Uber Careers",
     "We have decided to pursue other candidates whose experience more closely matches.", "rejection", "Uber"),
    ("Application Update", "no-reply@us.greenhouse-mail.io", "OpenAI",
     "Thank you for your interest in OpenAI. Unfortunately, we won't be moving forward.", "rejection", "OpenAI"),
    ("Following up on your interview with Asana", "recruiting@asana.com", "Asana Recruiting",
     "Thank you for taking the time to interview. After careful consideration we have decided not to move forward.", "rejection", "Asana"),
    ("Software Engineer, Backend", "no-reply@hire.lever.co", "Ramp",
     "Unfortunately we have decided to move forward with other candidates.", "rejection", "Ramp"),
    # a polite close alone is a WEAK rejection: noted, never moves the card
    ("Thank you for your interest in Dropbox", "no-reply@us.greenhouse-mail.io", "Dropbox",
     "We wish you the best in your job search.", "rejection", "Dropbox"),
    # ── interviews
    ("Invitation to interview - Anthropic", "no-reply@ashbyhq.com", "Anthropic",
     "We'd like to invite you to interview for the Software Engineer role. Please schedule a time.", "interview", "Anthropic"),
    ("Schedule your phone screen with Figma", "scheduling@goodtime.io", "Figma Recruiting",
     "Please pick a time that works for you.", "interview", "Figma"),
    ("Next steps: Technical interview at Brex", "recruiting@brex.com", "Brex Recruiting",
     "We would like to invite you to a technical interview. Share your availability.", "interview", "Brex"),
    ("Your interview with Robinhood is confirmed", "notifications@calendly.com", "Calendly",
     "Interview with Sam from Robinhood - 30 min.", "interview", "Robinhood"),
    ("Software Engineer - Meta - Interview Request", "recruiting@meta.com", "Meta Recruiting",
     "I'd love to chat about the role. Can you share your availability for a call this week?", "interview", "Meta"),
    ("Phone screen: Staff Engineer", "hiring@discord.com", "Discord",
     "Please schedule a phone screen using the link below.", "interview", "Discord"),
    ("Re: Next round at Square", "jordan@block.xyz", "Jordan Lee",
     "Congrats! Please book a time for the final round.", "interview", "Square"),
    ("Thank you for applying to Lattice", "no-reply@ashbyhq.com", "Lattice",
     "Thank you for applying! We'd love to chat about the role — please pick a time that works for you.", "interview", "Lattice"),
    ("Next steps - Hopper", "talent@hopper.com", "Hopper",
     "We'd like to move forward with your application and schedule a call with the hiring manager.", "interview", "Hopper"),
    ("Re: AI Engineer application", "hiring@tinyco.ai", "TinyCo",
     "Are you available for a 30 minute call next week? Here is my Calendly link.", "interview", "TinyCo"),
    # ── assessments / offers
    ("Your HackerRank assessment for Stripe", "no-reply@hackerrank.com", "HackerRank",
     "Stripe has invited you to complete the coding challenge.", "assessment", "Stripe"),
    ("Codility test invitation - Atlassian", "noreply@codility.com", "Codility",
     "Atlassian invites you to a Codility test.", "assessment", "Atlassian"),
    ("Your offer from Figma", "people@figma.com", "Figma People Team",
     "We are pleased to extend you an offer of employment.", "offer", "Figma"),
    ("Offer Letter - Senior Engineer", "hr@acme.io", "Acme HR",
     "Congratulations! Attached is your offer letter. Your start date is Nov 3.", "offer", "Acme"),
    ("Offer: Welcome to the team!", "hr@contoso.com", "Contoso HR",
     "We are pleased to offer you the position.", "offer", "Contoso"),
    # ── noise: alerts, account mail, profile views, marketing
    ("New jobs posted from ascendlearning.jobs.hr.cloud.sap", "noreply@jobs2web.com", "Jobs2web",
     "New jobs matching your saved search.", "job_alert", None),
    ("10 new jobs for Software Engineer in Columbus", "jobs-noreply@linkedin.com", "LinkedIn Job Alerts",
     "Jobs you may be interested in.", "job_alert", None),
    ("Software Engineer jobs in Cincinnati, OH", "alert@indeed.com", "Indeed",
     "New jobs matching your search.", "job_alert", None),
    ("Weekly digest: Jobs at companies hiring now", "noreply@glassdoor.com", "Glassdoor",
     "Top jobs for you this week.", "job_alert", None),
    ("Sign in link for Answer Financial Inc", "noreply@clearcompany.com", "ClearCompany",
     "Click the link below to sign in to your account.", "account", None),
    ("Your verification code is 482913", "no-reply@myworkday.com", "Workday",
     "Enter this code to verify your email.", "account", None),
    ("Reset your password", "no-reply@icims.com", "iCIMS", "Click here to reset your password.", "account", None),
    ("Your Workday account", "noreply@myworkday.com", "Workday",
     "Welcome! Complete your profile to finish your application.", "account", None),
    ("Show recruiters you're really interested with Dream Job", "no-reply@greenhouse-jobs.io", "Greenhouse",
     "Stand out from other candidates.", "marketing", None),
    ("Your profile was viewed by Futran Solutions", "notifications-noreply@linkedin.com", "LinkedIn",
     "Futran Solutions viewed your profile.", "profile_view", None),
    ("Karthik, 5 people viewed your profile this week", "notifications-noreply@linkedin.com", "LinkedIn",
     "See who's viewed your profile.", "profile_view", None),
    # ── recruiter outreach: not an application
    ("Exciting opportunity at Datadog", "recruiter@datadoghq.com", "Taylor Kim",
     "I came across your profile and think you'd be a great fit. Are you open to a quick chat?", "recruiter_outreach", "Datadog"),
    ("Quick question", "sam@gmail.com", "Sam Patel",
     "I'm a recruiter working with a fintech startup, reaching out to see if you're open to new opportunities.", "recruiter_outreach", None),
    # ── says nothing about an application
    ("Please rate your application experience", "surveys@smartrecruiters.com", "SmartRecruiters",
     "How did we do? Take our 2-minute survey.", "other", None),
    ("Random Email", "newsletter@gmail.com", "", "Check out these top links from the community.", "other", None),
]


# ── The legacy logic, reproduced for the BEFORE numbers ─────────────────────

def _legacy_company(sender_email: str, sender_name: str) -> str | None:
    """extension/content.js guessCompany, before 2026-10-10."""
    if sender_email and "@" in sender_email:
        domain = sender_email.split("@", 1)[1].lower()
        if not re.search(r"gmail|yahoo|hotmail|outlook|protonmail|icloud|aol|mail\.com", domain):
            parts = domain.split(".")
            while len(parts) > 1 and re.match(r"^(com|io|co|org|net|uk|us|dev|ai|app|xyz|jobs|careers)$", parts[-1]):
                parts.pop()
            if parts:
                name = parts[-1]
                if len(name) >= 2:
                    return name[0].upper() + name[1:]
    name = sender_name or ("" if "@" in (sender_email or "") else sender_email)
    if name:
        m = re.search(r"from\s+(.+)", name, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"at\s+(.+)", name, re.I)
        if m:
            return m.group(1).strip()
        if not re.search(r"\s", name.strip()) and len(name) >= 2:
            return name.strip()
    return None


def _legacy_kind(subject: str, snippet: str) -> str:
    """server.py sync_emails, before 2026-10-10: rejection > (ack suppresses) interview > ack."""
    text = f"{subject} {snippet}".lower()
    rej = any(k in text for k in es.REJECTION_KEYWORDS)
    ack = any(k in text for k in es.ACKNOWLEDGMENT_KEYWORDS)
    strong = any(k in text for k in es.POSITIVE_KEYWORDS)
    stage = any(k in text for k in es.STAGE_NOUNS)
    sched = any(k in text for k in es.SCHEDULING_HINTS)
    if rej:
        return "rejection"
    if not ack and (strong or (stage and sched)):
        return "interview"
    return "ack" if ack else "other"


def _same_company(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return es.norm_company(a) == es.norm_company(b)


def _score():
    new_kind = new_co = old_kind = old_co = 0
    old_noise_imported = 0
    misses = []
    for subject, sender, name, snippet, kind, company in CORPUS:
        addr = sender if "@" in sender else ""
        display = name or ("" if "@" in sender else sender)
        skind = es.sender_kind(addr, display)
        sig = es.classify(subject, snippet, skind)
        got_co = es.extract_company(subject, addr, display, None, None, snippet)
        if sig.kind == kind:
            new_kind += 1
        if _same_company(got_co, company):
            new_co += 1
        if sig.kind != kind or not _same_company(got_co, company):
            misses.append((subject, kind, sig.kind, company, got_co))
        lk = _legacy_kind(subject, snippet)
        if lk == kind:
            old_kind += 1
        lc = _legacy_company(addr, name)
        if _same_company(lc, company):
            old_co += 1
        if kind in es.NOISE_KINDS and lc:
            old_noise_imported += 1
    n = len(CORPUS)
    return n, new_kind, new_co, old_kind, old_co, old_noise_imported, misses


def test_corpus_accuracy_is_measured_and_high():
    n, new_kind, new_co, old_kind, old_co, old_noise, misses = _score()
    print(f"\n[email-sync accuracy on {n} labelled emails]")
    print(f"  kind     legacy {old_kind}/{n} = {old_kind / n:.0%}   new {new_kind}/{n} = {new_kind / n:.0%}")
    print(f"  company  legacy {old_co}/{n} = {old_co / n:.0%}   new {new_co}/{n} = {new_co / n:.0%}")
    noise_n = sum(1 for r in CORPUS if r[4] in es.NOISE_KINDS)
    print(f"  noise imported as an application: legacy {old_noise}/{noise_n}, new 0/{noise_n}")
    for m in misses:
        print("  MISS", m)
    assert new_kind / n >= 0.95, misses
    assert new_co / n >= 0.95, misses
    # The numbers must have moved, or the measurement is not measuring.
    assert new_kind > old_kind and new_co > old_co


def test_noise_is_never_an_application():
    for subject, sender, name, snippet, kind, _ in CORPUS:
        if kind in es.NOISE_KINDS:
            addr = sender if "@" in sender else ""
            assert es.classify(subject, snippet, es.sender_kind(addr, name)).is_noise, subject


def test_only_a_confident_signal_moves_a_card():
    weak = es.classify("Thank you for your interest in Dropbox", "We wish you the best in your job search.")
    assert weak.kind == "rejection" and not weak.moves_status
    strong = es.classify("Your application with Snowflake", "Unfortunately, we will not be moving forward.")
    assert strong.kind == "rejection" and strong.moves_status
    invite = es.classify("Thank you for applying to Lattice",
                         "Thank you for applying! We'd love to chat about the role — please pick a time that works for you.")
    assert invite.kind == "interview" and invite.moves_status
    process = es.classify("Your application to Cloudflare",
                          "We have received your application. Our process includes a phone screen and a technical interview.")
    assert process.kind == "ack"
    # An acknowledgement never becomes a status change; an assessment is a note.
    assert not es.classify("Thanks for applying to Figma!", "We've received your application.").moves_status
    assert not es.classify("Your HackerRank assessment for Stripe", "Stripe has invited you to complete the coding challenge.").moves_status


def test_a_rejection_always_beats_an_interview_stage_noun():
    sig = es.classify("Re: Software Engineer - Phone Screen",
                      "We enjoyed the phone screen, but we won't be proceeding to the next round. Please schedule nothing further.")
    assert sig.kind == "rejection"


# ── sender kinds and company extraction edge cases ───────────────────────────

@pytest.mark.parametrize("addr,name,want", [
    ("no-reply@us.greenhouse-mail.io", "Neuralink", "ats"),
    ("jobs-noreply@linkedin.com", "LinkedIn", "job_board"),
    ("sam@gmail.com", "Sam", "freemail"),
    ("careers@stripe.com", "Stripe", "company"),
    ("", "me", "self"),
    ("karthik@example.com", "", "self"),
])
def test_sender_kinds(addr, name, want):
    assert es.sender_kind(addr, name, user_email="karthik@example.com") == want


@pytest.mark.parametrize("subject,addr,name,want", [
    ("Your application to Acme Corp", "jobs@acme.com", "", "Acme Corp"),
    ("Thank you for applying to NimbusAI - Backend Engineer", "no-reply@nimbusai.com", "", "NimbusAI"),
    # a platform name is never the employer
    ("Thanks for applying!", "no-reply@us.greenhouse-mail.io", "Greenhouse", None),
    ("Your application", "jobs-noreply@linkedin.com", "LinkedIn", None),
    # a role is never the employer
    ("Your application to the Senior Engineer role", "no-reply@hire.lever.co", "", None),
    # "me" (the user's own row in a Gmail thread) never becomes a company
    ("Re: Interview", "", "me", None),
])
def test_company_extraction_edge_cases(subject, addr, name, want):
    assert es.extract_company(subject, addr, name, user_email="karthik@example.com") == want


# ── matching an email to the user's applications ─────────────────────────────

def _c(id_, status, company, title, days_ago=0):
    return es.Candidate(id=id_, status=status, company=company, title=title,
                        submitted_at=datetime(2026, 10, 1) - timedelta(days=days_ago),
                        updated_at=datetime(2026, 10, 1) - timedelta(days=days_ago))


def test_matching_is_by_normalised_name_never_substring():
    cands = [_c(1, "submitted", "Clever Inc", "Engineer"), _c(2, "submitted", "Acme, Inc.", "Engineer")]
    assert es.pick_application("Lever", "", cands) is None            # "lever" is not "Clever"
    assert es.pick_application("Acme Corp", "", cands).id == 2         # suffixes do not matter
    assert es.pick_application("ACME", "", cands).id == 2


def test_matching_prefers_what_the_user_submitted_then_the_closest_title():
    cands = [_c(1, "shortlisted", "Acme", "Data Scientist", 1),
             _c(2, "submitted", "Acme", "Backend Engineer", 5),
             _c(3, "submitted", "Acme", "Frontend Engineer", 2),
             _c(4, "rejected", "Acme", "Backend Engineer", 0)]
    assert es.pick_application("Acme", "Backend Engineer", cands).id == 2
    assert es.pick_application("Acme", "Frontend", cands).id == 3
    assert es.pick_application("Acme", "", cands).id == 3              # most recent submitted on a tie


def test_company_matches_word_boundaries():
    assert es.company_matches("Acme", "Acme Corporation")
    assert es.company_matches("Cooler Master USA", "Cooler Master")
    assert not es.company_matches("Lever", "Clever")
    assert not es.company_matches("Gem", "Gemini")
    assert not es.company_matches("", "Acme")
