"""Autofill agent.

Approach: per-platform handlers. Greenhouse and Lever pages have predictable
DOM structures so we hand-write resilient selectors. For Workday and one-off
career pages, fall back to a generic field-finder + Claude-assisted mapping.

The agent NEVER clicks submit. It opens the page, fills what it can, and
returns a list of PendingQuestion records for whatever it couldn't determine.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List
from urllib.parse import urlparse

from playwright.async_api import Page, async_playwright
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, PendingQuestion, AnswerMemory
from app.matching.pipeline import _load_resume

log = logging.getLogger(__name__)

# Track active headful browser page contexts: application_id -> Page
_active_previews: dict[int, Page] = {}
_main_loop: asyncio.AbstractEventLoop | None = None

def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop
    log.info("Main event loop registered in autofill agent")

def get_main_loop() -> asyncio.AbstractEventLoop | None:
    return _main_loop



@dataclass
class UnknownField:
    label: str
    selector: str
    field_type: str
    options: List[str] | None = None


# --- generic personal-info map ---

def _personal_fields() -> dict:
    return {
        "first_name": settings.applicant_first_name,
        "last_name": settings.applicant_last_name,
        "email": settings.applicant_email,
        "phone": settings.applicant_phone,
        "location": settings.applicant_location,
        "github": settings.applicant_github,
        "linkedin": settings.applicant_linkedin,
    }


def _resolve_deterministic_question(label: str) -> str | None:
    low = label.lower().strip()
    
    # 1. Work Authorization (Yes - F-1 students have legal right to work via OPT/CPT)
    auth_kws = ["authorized to work", "legally authorized", "legal right to work", "lawful right to work", "eligible to work in", "right to work"]
    if any(kw in low for kw in auth_kws):
        log.info("Deterministic match: Work Authorization -> Yes")
        return "Yes"

    # 2. Sponsorship now or future (Yes - will need sponsorship for H-1B in the future)
    spons_kws = ["require sponsorship", "sponsorship now or in the future", "visa sponsorship", "sponsorship in the future", "require visa", "sponsorship requirements"]
    if any(kw in low for kw in spons_kws):
        log.info("Deterministic match: Visa Sponsorship -> Yes")
        return "Yes"

    # 3. Security Clearance (No - active security clearances are restricted to US citizens)
    clearance_kws = ["security clearance", "active clearance", "clearance level", "government clearance", "active security clearance"]
    if any(kw in low for kw in clearance_kws):
        log.info("Deterministic match: Security Clearance -> No")
        return "No"

    # 4. Relocation (Yes - willing to relocate anywhere)
    reloc_kws = ["willing to relocate", "willingness to relocate", "willing to move"]
    if any(kw in low for kw in reloc_kws):
        log.info("Deterministic match: Relocation -> Yes")
        return "Yes"

    # 5. Salary expectations (Negotiable)
    salary_kws = ["salary expectation", "desired salary", "salary requirement", "compensation expectation", "target salary", "salary requirements"]
    if any(kw in low for kw in salary_kws):
        log.info("Deterministic match: Salary Expectation -> Negotiable")
        return "Negotiable"

    return None


def _check_memory(label: str) -> str | None:
    # 1. Intercept with deterministic rules
    det_ans = _resolve_deterministic_question(label)
    if det_ans is not None:
        return det_ans

    # 2. Check DB memory
    norm = label.lower().strip()
    with get_session() as session:
        mem = session.exec(select(AnswerMemory).where(AnswerMemory.label_normalized == norm)).first()
        if mem:
            from datetime import datetime
            mem.use_count += 1
            mem.last_used_at = datetime.utcnow()
            session.add(mem)
            session.commit()
            return mem.answer
    return None


SYSTEM_QUESTION_ANSWERER = """You write short, professional, and honest answers to job application screening questions.

Candidate Context:
- Karthik Amruthaluri
- Github: github.com/karthiklucky1 | Email: mahikish11@gmail.com
- Education: Master of Engineering, University of Cincinnati (graduating Aug 2026).
- Experience: Python Developer at Globali20 India (03/2023 – 08/2024), Python Developer Intern (03/2022 – 03/2023). 2.5+ years of experience building AI/ML-powered backend systems, LLM architectures, and machine learning APIs.
- Flagship Project: Volta (Dad Layer Architecture) — a runtime LLM hallucination verification system. Trained a 120MB-parameter autoregressive Transformer from scratch on Apple Silicon (1B+ tokens). Designed tiered Agent Cascade (Agent A/B/C) for domain-specific source trust scoring. Built FAISS-based semantic cache with domain-specific TTL policies. Designed token-level confidence scoring engine using softmax probabilities.
- Project Stark Labs: Autonomous Agent Synthesis & Self-Healing Pipeline. Architected "Mark II" self-healing loop using E2B sandboxes for agents to test and correct code errors autonomously.
- Project Smart Prompt Engine: NLP Optimization Backend & Chrome Extension. Built and shipped JavaScript extension with FastAPI backend, implementing prompt compression reducing token consumption by 22%.
- Tech Stack: Python, PyTorch, Transformers, LLMs, RAG, FAISS Vector Search, Semantic Caching, Agent Orchestration, FastAPI, MLOps, Docker, AWS ECS/Lambda.

Rules:
- Be honest. Do not invent or fabricate experience.
- If the candidate lacks direct experience with a specific tool/skill, use "truthful transfer": explain that they haven't used it directly but have adjacent experience (e.g., "I haven't used Ray directly, but I have built distributed async inference with FastAPI and AWS ECS").
- Keep the answer concise and natural, between 50 and 120 words.
- Write in first person ("I").
- Do not add any placeholder, explanations, or metadata. Return the exact response text to be entered into the form."""


def _answer_question_with_llm(label: str, job: Job, resume_text: str) -> str:
    from anthropic import Anthropic
    log.info("Generating LLM answer for screening question: '%s'", label)
    client = Anthropic(api_key=settings.anthropic_api_key)
    prompt = f"""<resume>
{resume_text[:6000]}
</resume>

<job>
Title: {job.title}
Company: {job.company}
Description: {job.description[:4000]}
</job>

Screening Question: "{label}"

Write a professional response answering this question based on the resume and job context."""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=[{"type": "text", "text": SYSTEM_QUESTION_ANSWERER}],
            messages=[{"role": "user", "content": prompt}],
        )
        ans = resp.content[0].text.strip()
        log.info("Generated LLM response: '%s...'", ans[:80])
        return ans
    except Exception as e:
        log.warning("LLM question answering failed for '%s': %s", label, e)
        return ""


# ---------- cover letter helper ----------

async def _fill_cover_letter_area(page: Page, cover_text: str) -> bool:
    """Detect and fill any cover letter textarea on the page."""
    if not cover_text:
        return False
    cover_keywords = ["cover letter", "cover_letter", "coverletter", "motivation letter", "why do you want", "why this company"]
    textareas = await page.query_selector_all("textarea")
    for ta in textareas:
        try:
            label = await ta.evaluate("""(e) => {
                if (e.id) {
                    const lbl = document.querySelector(`label[for='${e.id}']`);
                    if (lbl) return lbl.innerText.toLowerCase();
                }
                const parent = e.closest('[class*="field"], [class*="question"], .form-group');
                const lbl = parent?.querySelector('label, .label, legend');
                return lbl?.innerText?.toLowerCase() || e.name?.toLowerCase() || e.id?.toLowerCase() || '';
            }""")
            if any(kw in label for kw in cover_keywords):
                val = await ta.input_value()
                if not val:
                    await ta.fill(cover_text)
                    log.info("Filled cover letter text area (label: '%s')", label)
                    return True
        except Exception:
            continue
    return False


# ---------- React Select helper ----------

async def _fill_react_select(page: Page, field_id: str, answer: str) -> bool:
    """Fill a React Select combobox by clicking the control and selecting the matching option.

    React Select renders a text input with role=combobox inside a .select__control div.
    We click the control to open the menu, then click the option whose text matches `answer`.
    Returns True if an option was successfully clicked.
    """
    try:
        # Bug fix: .select__control is an ANCESTOR of the input, not a sibling descendant.
        # Use :has() to find the wrapping control, then fall back to clicking the input itself.
        control = await page.query_selector(f".select__control:has(#{field_id})")
        if not control:
            inp = await page.query_selector(f"#{field_id}")
            if not inp:
                return False
            await inp.click()
        else:
            await control.click()

        await page.wait_for_timeout(700)

        # Bug fix: scope to the currently open menu so we don't match options from
        # already-closed menus elsewhere on the page.
        menu = await page.query_selector(".select__menu, [class*='menu--is-open']")
        if menu:
            options = await menu.query_selector_all("[class*='option']")
        else:
            # Fallback: React Select generates predictable option IDs
            options = await page.query_selector_all(f"[id^='react-select-{field_id}-option']")
            if not options:
                options = await page.query_selector_all("[role='option']")

        low_answer = answer.lower().strip()
        for opt in options:
            txt = (await opt.inner_text()).strip()
            if txt.lower() == low_answer or txt.lower().startswith(low_answer):
                await opt.click()
                log.info("React Select '%s' → exact match '%s'", field_id, txt)
                await page.wait_for_timeout(300)
                return True

        # Partial match fallback
        for opt in options:
            txt = (await opt.inner_text()).strip()
            if low_answer in txt.lower():
                await opt.click()
                log.info("React Select '%s' → partial match '%s'", field_id, txt)
                await page.wait_for_timeout(300)
                return True

        log.warning("React Select '%s': no option matched '%s'. Available: %s",
                    field_id, answer, [((await o.inner_text()).strip()) for o in options[:8]])
        await page.keyboard.press("Escape")
        return False
    except Exception as e:
        log.warning("React Select fill failed for '%s': %s", field_id, e)
        return False


# ---------- Greenhouse handler ----------

# Preset answers for known greenhouse common questions (pre-seeded into memory)
_GREENHOUSE_KNOWN = {
    # Social links
    "linkedin profile": settings.applicant_linkedin,
    "github url": settings.applicant_github,
    "website": settings.applicant_github,

    # EEO — Gender
    "gender": "Male",
    "gender identity": "Male",
    "what is your gender?": "Male",

    # EEO — Hispanic/Latino
    "are you hispanic/latino?": "No, I am not Hispanic/Latino",
    "hispanic/latino": "No, I am not Hispanic/Latino",
    "hispanic ethnicity": "No, I am not Hispanic/Latino",

    # EEO — Race/Ethnicity
    "race": "Asian",
    "ethnicity": "Asian",
    "race/ethnicity": "Asian",
    "what is your race/ethnicity?": "Asian",
    "which race/ethnicity best describes you?": "Asian",
    "please identify your race": "Asian",
    "racial category": "Asian",
    "region": "Asia (India)",
    "what region are you from?": "Asia (India)",

    # EEO — Veteran
    "veteran status": "I am not a protected veteran",
    "are you a veteran?": "I am not a protected veteran",
    "protected veteran": "I am not a protected veteran",

    # EEO — Disability
    "disability status": "No, I do not have a disability, or history/record of having a disability",
    "disability": "No, I do not have a disability, or history/record of having a disability",
    "do you have a disability?": "No, I do not have a disability, or history/record of having a disability",

    # AI Policy — Yes/No dropdown
    "ai policy for application": "No",
    "ai policy": "No",
    "artificial intelligence policy": "No",
    "did you use ai": "No",
    "use of ai": "No",
    "ai usage": "No",
    "did you use artificial intelligence": "No",
}

async def _fill_greenhouse(page: Page, resume_docx: str, cover_text: str, job: Job, resume_text: str) -> List[UnknownField]:
    """Handles both boards.greenhouse.io and job-boards.greenhouse.io formats.
    New format uses id-based fields with no name attr and aria-required instead of required.
    """
    import os
    pf = _personal_fields()

    # --- Standard personal fields (both old and new Greenhouse) ---
    field_map = {
        "#first_name": pf["first_name"],
        "#last_name": pf["last_name"],
        "#email": pf["email"],
        "#phone": pf["phone"],
        "#country": "United States",
        "input[name='job_application[first_name]']": pf["first_name"],
        "input[name='job_application[last_name]']": pf["last_name"],
        "input[name='job_application[email]']": pf["email"],
        "input[name='job_application[phone]']": pf["phone"],
    }
    for sel, val in field_map.items():
        if not val:
            continue
        try:
            el = await page.query_selector(sel)
            if el:
                val_str = str(val)
                curr_val = await el.input_value()
                if curr_val != val_str:
                    await el.fill(val_str)
                    log.info("Filled %s", sel)
        except Exception as e:
            log.debug("GH fill skipped for %s: %s", sel, e)

    # --- Resume upload (new GH: id='resume', old GH: name contains 'resume') ---
    resume_abs = os.path.abspath(resume_docx) if resume_docx else ""
    uploaded = False
    if resume_abs:
        for file_sel in ["#resume", "input[type='file'][id='resume']", "input[type='file'][name*='resume']", "input[type='file']"]:
            try:
                el = await page.query_selector(file_sel)
                if el:
                    await el.set_input_files(resume_abs)
                    log.info("Resume uploaded via selector: %s", file_sel)
                    uploaded = True
                    break
            except Exception as e:
                log.debug("Resume upload attempt failed for %s: %s", file_sel, e)
    if not uploaded:
        log.warning("Could not upload resume — no matching file input found")

    # --- LinkedIn / GitHub quick-fill for known question IDs ---
    quick_fill = {
        "#question_13303964008": pf["linkedin"],  # LinkedIn Profile
        "#question_13303967008": pf["github"],    # GitHub URL
    }
    for sel, val in quick_fill.items():
        if not val:
            continue
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(str(val))
        except Exception:
            pass

    # --- EEO React Select dropdowns (not required, so scanner misses them) ---
    # Some fields (e.g. race) only appear after a prior answer (hispanic_ethnicity=No).
    # Multi-pass: after each fill, wait for DOM to settle, then re-check remaining fields.
    # Any EEO field present on the page but not fillable is added to unknown for bot report.
    eeo_fields = {
        "gender":             "Male",
        "hispanic_ethnicity": "No",
        "race":               "Asian",
        "veteran_status":     "I am not a protected veteran",
        "disability_status":  "No, I do not have a disability and have not had one in the past",
    }
    eeo_filled: set = set()

    for _pass in range(4):  # up to 4 passes to catch dynamically revealed fields
        new_fills_this_pass = 0
        for field_id, answer in eeo_fields.items():
            if field_id in eeo_filled:
                continue
            el = await page.query_selector(f"#{field_id}")
            if not el:
                continue
            role = await el.get_attribute("role") or ""
            filled = False
            if role == "combobox":
                filled = await _fill_react_select(page, field_id, answer)
                log.info("EEO field #%s → '%s' (%s)", field_id, answer, "✅" if filled else "❌")
            else:
                try:
                    await el.select_option(label=answer)
                    filled = True
                except Exception:
                    try:
                        await el.fill(answer)
                        filled = True
                    except Exception:
                        pass
                if filled:
                    log.info("EEO field #%s → '%s' ✅", field_id, answer)
            if filled:
                eeo_filled.add(field_id)
                new_fills_this_pass += 1
                await page.wait_for_timeout(700)  # let any newly revealed fields render

                # Selecting "No" for hispanic_ethnicity reveals the race field dynamically.
                # It gets a generated ID (not "#race"), so hunt for it immediately by label.
                if field_id == "hispanic_ethnicity":
                    await page.wait_for_timeout(1200)  # extra wait for race field to fully render
                    race_cbs = await page.query_selector_all("input[role='combobox']")
                    for rcb in race_cbs:
                        rcb_id = await rcb.get_attribute("id") or ""
                        if rcb_id in eeo_filled:
                            continue
                        rcb_label = await rcb.evaluate("""(e) => {
                            if (e.id) {
                                const lbl = document.querySelector(`label[for='${e.id}']`);
                                if (lbl) return lbl.innerText.replace(/[*\\n]+/g, ' ').trim();
                            }
                            const parent = e.closest('[class*="field"], [class*="question"], li');
                            const lbl = parent?.querySelector('label, .label, legend');
                            return lbl?.innerText.replace(/[*\\n]+/g, ' ').trim() || '';
                        }""")
                        if any(kw in rcb_label.lower() for kw in ["race", "ethnicity", "racial"]):
                            race_filled = await _fill_react_select(page, rcb_id, "Asian")
                            if race_filled:
                                eeo_filled.add(rcb_id)
                                log.info("Dynamic race field #%s ('%s') → Asian ✅", rcb_id, rcb_label)
                            else:
                                log.warning("Dynamic race field #%s ('%s') → failed ❌", rcb_id, rcb_label)
                            break

        if new_fills_this_pass == 0:
            break  # nothing new filled this pass, stop early

    # --- Cover letter ---
    await _fill_cover_letter_area(page, cover_text)

    # --- Scan all visible required custom fields ---
    unknown: List[UnknownField] = []

    # EEO fields that were present on the page but couldn't be filled → report to bot
    for field_id, answer in eeo_fields.items():
        if field_id in eeo_filled:
            continue
        el = await page.query_selector(f"#{field_id}")
        if not el:
            continue
        label_text = await el.evaluate("""(e) => {
            if (e.id) {
                const lbl = document.querySelector(`label[for='${e.id}']`);
                if (lbl) return lbl.innerText.replace(/[*\\n]+/g, ' ').trim();
            }
            const parent = e.closest('[class*="field"], [class*="question"], li');
            const lbl = parent?.querySelector('label, .label, legend');
            return lbl?.innerText.replace(/[*\\n]+/g, ' ').trim() || e.id;
        }""") or field_id
        log.warning("EEO field #%s ('%s') present but not filled — adding to missing", field_id, label_text)
        unknown.append(UnknownField(label=label_text, selector=f"#{field_id}", field_type="select"))

    # --- Label-based combobox sweep ---
    # Catches dynamic fields (e.g. race appearing after hispanic=No) that have generated
    # IDs not matching eeo_fields, plus new required dropdowns like "AI Policy".
    # For every role=combobox on the page: fill if label is known, report if required & unknown.
    all_comboboxes = await page.query_selector_all("input[role='combobox']")
    for cb in all_comboboxes:
        try:
            cb_id = await cb.get_attribute("id") or ""
            if cb_id in eeo_filled:
                continue
            # Skip if the React Select control already shows a selected value
            already_selected = await cb.evaluate("""(e) => {
                const ctrl = e.closest('.select__control');
                if (ctrl) {
                    const val = ctrl.querySelector('.select__single-value');
                    return !!(val && val.innerText.trim());
                }
                return false;
            }""")
            if already_selected:
                if cb_id:
                    eeo_filled.add(cb_id)
                continue
            # Get label text for this combobox
            label_text = await cb.evaluate("""(e) => {
                if (e.id) {
                    const lbl = document.querySelector(`label[for='${e.id}']`);
                    if (lbl) return lbl.innerText.replace(/[*\\n]+/g, ' ').trim();
                }
                const parent = e.closest('[class*="field"], [class*="question"], li');
                const lbl = parent?.querySelector('label, .label, legend');
                return lbl?.innerText.replace(/[*\\n]+/g, ' ').trim() || '';
            }""")
            if not label_text:
                continue
            is_required = ('*' in label_text) or (
                await cb.evaluate("(e) => e.getAttribute('aria-required') === 'true'")
            )
            clean_label = label_text.strip().rstrip('*').strip()
            low_label = clean_label.lower()
            known_val = _GREENHOUSE_KNOWN.get(low_label) or _check_memory(clean_label)
            if known_val and cb_id:
                filled = await _fill_react_select(page, cb_id, known_val)
                if filled:
                    eeo_filled.add(cb_id)
                    log.info("Combobox sweep filled '%s' (#%s) → '%s'", clean_label, cb_id, known_val)
                    await page.wait_for_timeout(700)
                    continue
                # Fill failed — if required, fall through to report
            if is_required:
                sel = f"#{cb_id}" if cb_id else "input[role='combobox']"
                if not any(u.selector == sel for u in unknown):
                    log.warning("Unknown required combobox: '%s' (#%s)", clean_label, cb_id)
                    unknown.append(UnknownField(label=clean_label, selector=sel, field_type="select"))
        except Exception as exc:
            log.debug("Combobox sweep error: %s", exc)
            continue

    # New Greenhouse uses aria-required=true on the input itself
    all_fields = await page.query_selector_all("input[aria-required='true'], textarea[aria-required='true'], select[aria-required='true'], input[required], textarea[required], select[required]")

    # Also grab any field with a question_ id that has no value
    question_fields = await page.query_selector_all("input[id^='question_'], textarea[id^='question_']")
    seen_ids = set()
    combined = all_fields + [f for f in question_fields if f not in all_fields]

    for el in combined:
        try:
            el_id = await el.get_attribute("id") or ""
            if el_id in seen_ids:
                continue
            seen_ids.add(el_id)

            # Bug fix: React Select combobox inputs always return "" from input_value()
            # even when filled, so skip EEO fields we already successfully filled above.
            if el_id in eeo_filled:
                continue

            # Skip file inputs and hidden
            el_type = await el.get_attribute("type") or "text"
            if el_type in ("file", "hidden", "submit"):
                continue

            value = await el.input_value()
            if value:
                continue

            # Get label text
            label = await el.evaluate("""(e) => {
                if (e.id) {
                    const lbl = document.querySelector(`label[for='${e.id}']`);
                    if (lbl) return lbl.innerText.replace(/[*\\n]+/g, ' ').trim();
                }
                const parent = e.closest('[class*="field"], [class*="question"], li');
                const lbl = parent?.querySelector('label, .label, legend');
                return lbl?.innerText.replace(/[*\\n]+/g, ' ').trim() || e.placeholder || e.id || '(unlabeled)';
            }""")
            label = label.strip().rstrip("*").strip()
            tag = await el.evaluate("(e) => e.tagName.toLowerCase()")

            # Check against known quick-fills first
            low_label = label.lower()
            known_val = _GREENHOUSE_KNOWN.get(low_label) or _check_memory(label)
            el_role = await el.get_attribute("role") or ""
            if not known_val and tag in ["input", "textarea"] and el_role != "combobox" and el_type not in ["checkbox", "radio"]:
                known_val = _answer_question_with_llm(label, job, resume_text)
                if known_val:
                    try:
                        with get_session() as session:
                            session.add(AnswerMemory(
                                label_normalized=low_label,
                                label_original=label,
                                answer=known_val
                            ))
                            session.commit()
                    except Exception:
                        pass
            if known_val:
                log.info("Answer memory hit for '%s': %s", label, known_val[:80])
                filled = False
                try:
                    if el_role == "combobox":
                        filled = await _fill_react_select(page, el_id, known_val)
                    elif tag == "select":
                        await el.select_option(label=known_val)
                        filled = True
                    else:
                        await el.fill(known_val)
                        filled = True
                except Exception as e:
                    log.warning("Failed to auto-fill '%s': %s", label, e)
                if filled:
                    continue

            log.info("Unknown required field: '%s' (%s) role=%s", label, el_id, el_role)
            unknown.append(UnknownField(label=label, selector=f"#{el_id}" if el_id else f"*[placeholder='{await el.get_attribute('placeholder')}']", field_type=tag))
        except Exception as exc:
            log.debug("Error scanning field: %s", exc)
            continue

    # Post-fill standard field verification
    for sel, val in field_map.items():
        try:
            el = await page.query_selector(sel)
            if el:
                current_val = await el.input_value()
                if not current_val:
                    await el.fill(str(val))
                    await page.wait_for_timeout(300)
                    if not await el.input_value():
                        label_name = sel.replace("#", "").replace("input[name='job_application[", "").replace("]']", "").replace("_", " ").title()
                        if not any(u.selector == sel for u in unknown):
                            unknown.append(UnknownField(label=f"Required: {label_name}", selector=sel, field_type="text"))
        except Exception:
            pass

    return unknown



# ---------- Lever handler ----------

async def _fill_lever(page: Page, resume_docx: str, cover_text: str, job: Job, resume_text: str) -> List[UnknownField]:
    pf = _personal_fields()
    selectors = {
        "input[name='name']": pf["first_name"] + " " + pf["last_name"],
        "input[name='email']": pf["email"],
        "input[name='phone']": pf["phone"],
        "input[name='org']": "University of Cincinnati",
        "input[name='urls[LinkedIn]']": pf["linkedin"],
        "input[name='urls[GitHub]']": pf["github"],
    }
    for sel, val in selectors.items():
        if not val or not str(val).strip():
            continue
        try:
            el = await page.query_selector(sel)
            if el:
                curr_val = await el.input_value()
                if curr_val != val:
                    await el.fill(val)
        except Exception as e:
            log.debug("Lever fill skipped for %s: %s", sel, e)

    try:
        file_input = await page.query_selector("input[type='file'][name='resume']")
        if file_input and resume_docx:
            await file_input.set_input_files(os.path.abspath(resume_docx))
    except Exception as e:
        log.warning("Resume upload failed: %s", e)

    await _fill_cover_letter_area(page, cover_text)

    unknown: List[UnknownField] = []
    custom_fields = await page.query_selector_all(".application-question input, .application-question textarea, .application-question select")
    for el in custom_fields:
        try:
            value = await el.input_value()
            if value:
                continue
            required = await el.evaluate("(e) => e.required || e.getAttribute('aria-required') === 'true'")
            if not required:
                continue
            label = await el.evaluate("""(e) => {
                const parent = e.closest('.application-question');
                const lbl = parent?.querySelector('.application-label');
                return lbl?.innerText.trim() || e.name || '(unlabeled)';
            }""")
            tag = await el.evaluate("(e) => e.tagName.toLowerCase()")
            
            cached_ans = _check_memory(label)
            el_type = await el.get_attribute("type") or "text"
            if not cached_ans and tag in ["input", "textarea"] and el_type not in ["checkbox", "radio"]:
                cached_ans = _answer_question_with_llm(label, job, resume_text)
                if cached_ans:
                    try:
                        with get_session() as session:
                            session.add(AnswerMemory(
                                label_normalized=label.lower().strip(),
                                label_original=label,
                                answer=cached_ans
                            ))
                            session.commit()
                    except Exception:
                        pass
            if cached_ans:
                log.info("Answer memory hit for '%s': %s", label, cached_ans)
                try:
                    if tag == "select":
                        await el.select_option(label=cached_ans)
                    else:
                        await el.fill(cached_ans)
                    continue
                except Exception as e:
                    log.warning("Failed to auto-fill cached answer for %s: %s", label, e)
                    
            unknown.append(UnknownField(label=label, selector=f"*[name='{await el.get_attribute('name')}']", field_type=tag))
        except Exception:
            continue

    # Post-fill standard field verification
    for sel, val in selectors.items():
        try:
            el = await page.query_selector(sel)
            if el:
                current_val = await el.input_value()
                if not current_val:
                    await el.fill(val)
                    await page.wait_for_timeout(300)
                    if not await el.input_value():
                        label_name = sel.replace("input[name='", "").replace("']", "").replace("urls[", "").replace("]", "").replace("_", " ").title()
                        if not any(u.selector == sel for u in unknown):
                            unknown.append(UnknownField(label=f"Required: {label_name}", selector=sel, field_type="text"))
        except Exception:
            pass

    return unknown


# ---------- Ashby handler ----------

async def _fill_ashby(page: Page, resume_docx: str, cover_text: str, job: Job, resume_text: str) -> List[UnknownField]:
    pf = _personal_fields()
    selectors = {
        "input[name='name']": pf["first_name"] + " " + pf["last_name"],
        "input[name='email']": pf["email"],
        "input[name='phone']": pf["phone"],
    }
    for sel, val in selectors.items():
        if not val or not str(val).strip():
            continue
        try:
            el = await page.query_selector(sel)
            if el:
                curr_val = await el.input_value()
                if curr_val != val:
                    await el.fill(val)
        except Exception as e:
            log.debug("Ashby fill skipped for %s: %s", sel, e)

    try:
        file_input = await page.query_selector("input[type='file']")
        if file_input and resume_docx:
            await file_input.set_input_files(os.path.abspath(resume_docx))
    except Exception as e:
        log.warning("Resume upload failed: %s", e)

    await _fill_cover_letter_area(page, cover_text)

    unknown: List[UnknownField] = []
    custom_fields = await page.query_selector_all("form input, form textarea, form select")
    for el in custom_fields:
        try:
            value = await el.input_value()
            if value:
                continue
            required = await el.evaluate("(e) => e.required || e.getAttribute('aria-required') === 'true'")
            if not required:
                continue
            label = await el.evaluate("""(e) => {
                const lbl = e.closest('label');
                if (lbl) return lbl.innerText.trim().split('\\n')[0];
                return e.name || '(unlabeled)';
            }""")
            tag = await el.evaluate("(e) => e.tagName.toLowerCase()")
            
            # Skip fields we already handled by name
            name_attr = await el.get_attribute("name")
            if name_attr in ["name", "email", "phone", "resume"]:
                continue
                
            cached_ans = _check_memory(label)
            el_type = await el.get_attribute("type") or "text"
            if not cached_ans and tag in ["input", "textarea"] and el_type not in ["checkbox", "radio"]:
                cached_ans = _answer_question_with_llm(label, job, resume_text)
                if cached_ans:
                    try:
                        with get_session() as session:
                            session.add(AnswerMemory(
                                label_normalized=label.lower().strip(),
                                label_original=label,
                                answer=cached_ans
                            ))
                            session.commit()
                    except Exception:
                        pass
            if cached_ans:
                log.info("Answer memory hit for '%s': %s", label, cached_ans)
                try:
                    if tag == "select":
                        await el.select_option(label=cached_ans)
                    else:
                        await el.fill(cached_ans)
                    continue
                except Exception as e:
                    log.warning("Failed to auto-fill cached answer for %s: %s", label, e)
                    
            unknown.append(UnknownField(label=label, selector=f"*[name='{name_attr}']", field_type=tag))
        except Exception:
            continue

    # Post-fill standard field verification
    for sel, val in selectors.items():
        try:
            el = await page.query_selector(sel)
            if el:
                current_val = await el.input_value()
                if not current_val:
                    await el.fill(val)
                    await page.wait_for_timeout(300)
                    if not await el.input_value():
                        label_name = sel.replace("input[name='", "").replace("']", "").replace("_", " ").title()
                        if not any(u.selector == sel for u in unknown):
                            unknown.append(UnknownField(label=f"Required: {label_name}", selector=sel, field_type="text"))
        except Exception:
            pass

    return unknown


# ---------- dispatcher ----------

async def _autofill_one(application_id: int) -> List[UnknownField]:
    with get_session() as session:
        app = session.get(Application, application_id)
        if not app:
            raise ValueError(f"Application {application_id} not found")
        job = session.get(Job, app.job_id)
        apply_url = app.apply_url or job.url
        resume_path = app.tailored_resume_path
        cover_path = app.cover_letter_path

    cover_text = ""
    if cover_path:
        from pathlib import Path
        cover_text = Path(cover_path).read_text(encoding="utf-8")

    resume_text = _load_resume()
    host = urlparse(apply_url).netloc
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)  # run in background (headless)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(apply_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        if "greenhouse" in host or "boards.greenhouse" in host:
            unknown = await _fill_greenhouse(page, resume_path, cover_text, job, resume_text)
        elif "lever.co" in host:
            unknown = await _fill_lever(page, resume_path, cover_text, job, resume_text)
        elif "ashbyhq.com" in host:
            unknown = await _fill_ashby(page, resume_path, cover_text, job, resume_text)
        else:
            log.warning("No handler for %s yet — falling through", host)
            unknown = []

        log.info("Autofill complete. %d unknown fields.", len(unknown))
        # await browser.close()  # leave open for now
        return unknown


async def _preview_one(application_id: int) -> None:
    """Re-fill the form in headful mode and keep the browser open until user closes it."""
    if application_id in _active_previews:
        existing_page = _active_previews[application_id]
        try:
            if not existing_page.is_closed():
                log.info("Bringing existing browser window to front for app %d", application_id)
                await existing_page.bring_to_front()
                return
        except Exception as e:
            log.debug("Failed to bring existing page to front: %s", e)
            _active_previews.pop(application_id, None)

    with get_session() as session:
        app = session.get(Application, application_id)
        if not app:
            raise ValueError(f"Application {application_id} not found")
        job = session.get(Job, app.job_id)
        apply_url = app.apply_url or job.url
        resume_path = app.tailored_resume_path
        cover_path = app.cover_letter_path

    cover_text = ""
    if cover_path:
        from pathlib import Path
        cover_text = Path(cover_path).read_text(encoding="utf-8")

    resume_text = _load_resume()
    host = urlparse(apply_url).netloc
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            _active_previews[application_id] = page
            
            try:
                await page.goto(apply_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                if "greenhouse" in host or "boards.greenhouse" in host:
                    await _fill_greenhouse(page, resume_path, cover_text, job, resume_text)
                elif "lever.co" in host:
                    await _fill_lever(page, resume_path, cover_text, job, resume_text)
                elif "ashbyhq.com" in host:
                    await _fill_ashby(page, resume_path, cover_text, job, resume_text)
                else:
                    log.warning("No handler for %s yet — falling through", host)
            except Exception as e:
                log.exception("Error during form filling in preview mode: %s", e)

            log.info("Form filled and open in browser. Close the browser window when done.")
            try:
                is_submitted = False
                for _ in range(3600):
                    try:
                        if page.is_closed():
                            break

                        # --- Live Auto-Save ---
                        try:
                            js_extract_script = """() => {
                                const data = [];
                                const inputs = document.querySelectorAll("input, textarea, select");
                                for (const el of inputs) {
                                    const type = el.getAttribute("type");
                                    if (type === "file" || type === "hidden" || type === "submit") continue;
                                    
                                    let val = "";
                                    if (el.tagName.toLowerCase() === "select") {
                                        const opt = el.options[el.selectedIndex];
                                        val = opt ? opt.text.trim() : "";
                                    } else {
                                        val = el.value ? el.value.trim() : "";
                                    }
                                    if (!val) continue;
                                    
                                    let label = "";
                                    if (el.id) {
                                        const lbl = document.querySelector(`label[for='${el.id}']`);
                                        if (lbl) label = lbl.innerText;
                                    }
                                    if (!label) {
                                        const parent = el.closest('[class*="field"], [class*="question"], li, .application-question, .form-question');
                                        const lbl = parent?.querySelector('label, .label, legend, .application-label');
                                        label = lbl ? lbl.innerText : "";
                                    }
                                    if (!label) {
                                        label = el.placeholder || el.name || el.id || "";
                                    }
                                    
                                    label = label.replace(/[\\*\\n\\r]+/g, ' ').replace(/[:\\s]+$/, '').trim();
                                    if (label && val) {
                                        data.push({ label: label, value: val });
                                    }
                                }
                                
                                const comboboxes = document.querySelectorAll("input[role='combobox']");
                                for (const cb of comboboxes) {
                                    const ctrl = cb.closest('.select__control');
                                    if (ctrl) {
                                        const singleValEl = ctrl.querySelector('.select__single-value');
                                        const val = singleValEl ? singleValEl.innerText.trim() : "";
                                        if (!val) continue;
                                        
                                        let label = "";
                                        if (cb.id) {
                                            const lbl = document.querySelector(`label[for='${cb.id}']`);
                                            if (lbl) label = lbl.innerText;
                                        }
                                        if (!label) {
                                            const parent = cb.closest('[class*="field"], [class*="question"], li, .application-question, .form-question');
                                            const lbl = parent?.querySelector('label, .label, legend, .application-label');
                                            label = lbl ? lbl.innerText : "";
                                        }
                                        label = label.replace(/[\\*\\n\\r]+/g, ' ').replace(/[:\\s]+$/, '').trim();
                                        if (label && val) {
                                            data.push({ label: label, value: val });
                                        }
                                    }
                                }
                                return data;
                            }"""
                            fields = await page.evaluate(js_extract_script)
                            if fields:
                                from datetime import datetime
                                with get_session() as session:
                                    for f in fields:
                                        label_orig = f["label"]
                                        label_norm = label_orig.lower().strip()
                                        val = f["value"]
                                        if len(label_norm) < 3 or len(val) < 1:
                                            continue
                                        existing = session.exec(
                                            select(AnswerMemory).where(AnswerMemory.label_normalized == label_norm)
                                        ).first()
                                        if existing:
                                            if existing.answer != val:
                                                existing.answer = val
                                                existing.last_used_at = datetime.utcnow()
                                                session.add(existing)
                                        else:
                                            session.add(
                                                AnswerMemory(
                                                    label_normalized=label_norm,
                                                    label_original=label_orig,
                                                    answer=val,
                                                    last_used_at=datetime.utcnow(),
                                                    use_count=1
                                                )
                                            )
                                    session.commit()
                        except Exception as e:
                            log.debug("Auto-save failed: %s", e)

                        current_url = page.url.lower()
                        success_keywords = ["/thanks", "/thank", "success", "confirmation", "submitted"]
                        if any(kw in current_url for kw in success_keywords):
                            is_submitted = True
                            log.info("Submission detected via URL navigation: %s", page.url)
                            break
                    except Exception as loop_err:
                        log.debug("Preview loop iteration warning (possibly transient): %s", loop_err)
                    await asyncio.sleep(1)

                if is_submitted:
                    from datetime import datetime
                    with get_session() as session:
                        app_db = session.get(Application, application_id)
                        if app_db:
                            app_db.status = ApplicationStatus.SUBMITTED
                            app_db.submitted_at = datetime.utcnow()
                            session.add(app_db)
                            session.commit()
                            log.info("Application %d status updated to SUBMITTED", application_id)

                if not page.is_closed():
                    await page.wait_for_event("close", timeout=0)
            except Exception as e:
                log.warning("Submission detection or close wait failed: %s", e)
    finally:
        _active_previews.pop(application_id, None)


def autofill(application_id: int) -> List[UnknownField]:
    """Sync wrapper — fills form, saves pending questions, updates status, and notifies via Telegram."""
    # Auto-tailor if not done yet
    with get_session() as session:
        app = session.get(Application, application_id)
        if not app:
            raise ValueError(f"Application {application_id} not found")
        is_shortlisted = app.status == ApplicationStatus.SHORTLISTED
        has_no_resume = not app.tailored_resume_path

    if is_shortlisted or has_no_resume:
        from app.tailoring.tailor import tailor_for_application
        log.info("Application %d is not tailored yet. Tailoring now...", application_id)
        try:
            tailor_for_application(application_id)
        except Exception as e:
            log.exception("Auto-tailoring failed for application %d: %s", application_id, e)

    # Clear old unanswered pending questions for this app so we get fresh ones
    with get_session() as session:
        old_pqs = session.exec(
            select(PendingQuestion).where(PendingQuestion.application_id == application_id)
        ).all()
        for pq in old_pqs:
            session.delete(pq)
        session.commit()

    unknown = asyncio.run(_autofill_one(application_id))

    with get_session() as session:
        for uf in unknown:
            session.add(
                PendingQuestion(
                    application_id=application_id,
                    field_label=uf.label,
                    field_selector=uf.selector,
                    field_type=uf.field_type,
                )
            )
        app = session.get(Application, application_id)
        job = session.get(Job, app.job_id)
        app.status = (
            ApplicationStatus.AWAITING_USER if unknown else ApplicationStatus.READY_TO_SUBMIT
        )
        session.add(app)
        session.commit()
        job_title = job.title
        job_company = job.company

    # Proactively ping Telegram with the first question
    try:
        import httpx
        from app.config import settings
        if unknown:
            first = unknown[0]
            msg = (
                f"🤖 *JobAgent* — Form filled for *{job_company}*\n"
                f"📋 Role: _{job_title}_\n\n"
                f"Found *{len(unknown)} question(s)* I couldn't answer automatically.\n\n"
                f"*Question 1 of {len(unknown)}:*\n{first.label}\n\n"
                f"Reply with your answer, or send /next to see this again."
            )
        else:
            msg = (
                f"✅ *{job_company}* — Form fully filled!\n"
                f"_{job_title}_\n\nNo custom questions needed. *Launching browser for final verification...*"
            )
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        log.info("Telegram notified: %d pending questions for app %d", len(unknown), application_id)
        
        # If fully filled, automatically launch preview so the user can "check full form"
        if not unknown:
            log.info("Launching automatic preview for app %d", application_id)
            preview(application_id)
            
    except Exception as e:
        log.warning("Telegram notification or auto-preview failed: %s", e)

    return unknown



def preview(application_id: int) -> None:
    """Re-open the filled form in a visible browser window so the user can review and submit."""
    loop = get_main_loop()
    if loop and loop.is_running():
        log.info("Scheduling preview for app %d on the main event loop", application_id)
        asyncio.run_coroutine_threadsafe(_preview_one(application_id), loop)
    else:
        log.info("Main event loop not running. Running preview synchronously on current thread")
        asyncio.run(_preview_one(application_id))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python -m app.autofill.agent <application_id>")
        sys.exit(1)
    autofill(int(sys.argv[1]))
