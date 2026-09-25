# The evidence inventory

*Added 2026-09-25. Guards: `tests/test_evidence_inventory.py`,
`tests/test_requirement_review.py`.*

`app/tailoring/evidence.py` answers **"does this sentence appear in the master
résumé?"** That is enough to catch a fabricated sentence and not nearly enough to
answer the two questions a recruiter screening call turns on:

* Is this skill backed by **paid work**, or by a weekend project?
* How much time is behind it, once you stop counting the same months twice?

`app/tailoring/inventory.py` answers those. `app/tailoring/requirements.py` reads
what a posting asks for and compares the two.

## The defect that started it

`resume_basic_extract._years_experience` measured `min(start) → max(end)` — the
**span** of a career rather than the time inside it. A 2016 summer internship
plus a job started in 2024 reported **10 years of experience** for a résumé
holding under three:

```
gap career (2016 intern + 2024 job):  10 years   →  3 years
two concurrent 2-year roles        :   2 years   →  2 years
```

That number is not cosmetic. It reaches:

| consumer | what it does with it |
| --- | --- |
| `matching/reranker.py:516-536` | the scoring prompt's seniority rules — "candidate has ~{yoe} years" |
| `matching/filters/rule_filter.py:138` | `cand_years`, the seniority gate |
| `matching/cards.py:437` | `UserCard`'s stated years |

So the inflation spends a day's finals budget on Staff and Principal postings the
user will not be screened for — the opposite of what the product is for.

Summing the periods instead is the mirror error: two roles held at once would
count their overlap twice. **Tenure is the measure of a union of intervals**, and
`inventory.merged_months` is the one implementation of it. `_years_experience`
now calls it.

## Five kinds of work, kept apart

A résumé's "experience" is not one substance:

| kind | what it is | counted as experience? |
| --- | --- | --- |
| `professional` | paid employment | yes |
| `freelance` | paid client/contract work | yes |
| `internship` | real, paid, routinely discounted | **reported separately** |
| `academic` | coursework, research/teaching assistant | never |
| `personal` | side projects, hackathons, self-study | never |
| `volunteer` | unpaid contribution | never |

Kind is read from the section heading, then **overridden by the title**: a
"Software Engineering Intern" under a plain `## Experience` heading is an
internship. A title override can never promote academic work into employment.

A completed project is genuine evidence that someone can do the thing. It is
**not** years of professional employment, and the difference is exactly what a
screening call exposes. So `SkillEvidence.project_only` carries no months, and
`internship_only` is a separate case — telling someone their Python is "a
personal project" when they used it at an employer is both wrong and insulting.

## Reading a requirement as written

"4-6 years", "three (3) years", "2+", "at least 18 months" and "one year" are
five different requirements. Each keeps its **verbatim wording**, its bounds in
months, whether it was stated as required or preferred, and the skill it attached
to. Two scoping rules, both pinned by test, because both errors point at a
candidate being told a job is out of reach when the posting does not say so:

* a preference word binds to its own **clause**. In *"5 years of experience;
  Kafka preferred"* the preference is about Kafka.
* a **"Preferred Qualifications:" heading** makes the bullets under it preferred.

`up to 5 years` is a ceiling, not a floor, and yields no requirement at all.

## The pre-download review

`GET /application/{id}/review` — owner-scoped, read-only, no LLM, no network.
Three buckets and no fourth:

* **requirements supported** — what the master résumé actually answers
* **genuine gaps** — a *preferred* shortfall is **not** one; it goes to questions
* **open questions** — what only the candidate can answer

Plus an **improvement plan**: skill-gap suggestions stay there until confirmed,
and `unconfirmed_project_claims` checks the draft against them. If "build
something with Kafka" turns up in the résumé as a Kafka bullet, that is a
fabrication and the review names it (`ok is False`).

**There is no score and no estimated chance of an interview.** A keyword match is
not a hiring probability, and presenting one as though it were is how someone
walks into a screening call expecting a number to speak for them. Two tests
assert no output ever contains `%`, "chance", "likelihood", "probability",
"odds", "score" or "you will get".

Year-only dates are **admitted**, not smoothed over: a bare year is read as
mid-year (the expected value, not the flattering one) and the review says the
totals are approximate. Only dates that feed a total can make a total
approximate — `2021 - 2023` under Education says nothing about employment.

## What the tailor is told

`Tailor.tailor_resume` builds a `VERIFIED EVIDENCE` block beside the existing ATS
block:

* **backed by paid work**, strongest first — an employer reads the top of a page
* **demonstrated but not employment** — stays where the résumé puts it, never
  reworded into a job
* **acronyms to expand once** — only from `inventory.ACRONYMS`; nothing is
  invented
* **the posting's requirements as worded** — with an explicit instruction not to
  restate, inflate or imply a number of years the master résumé does not support

The block is best-effort: if anything in it raises, tailoring continues without
it rather than failing the request.

## What the inventory deliberately does not do

**It never leaves the master résumé.** No LLM, no database, no network — pinned by
a test that greps the module for `requests.`, `httpx.`, `anthropic`, `openai`,
`get_session` and `socket.`. That is what makes it safe to build on any path,
including inside the tailor's prompt assembly, and it is why external evidence
(GitHub repositories, a LinkedIn profile) is **not** folded in here.
`intelligence/skill_gap.py` already gathers those and classifies each skill by the
strongest evidence available; the inventory answers the narrower question the
tailoring path needs, from the one document the user is about to send.

The consequence is honest and worth stating: a skill the user demonstrably uses on
GitHub but never wrote on their résumé reads as `listed_only` or as a gap here.
That is the correct answer for a résumé review — an employer reads the résumé —
and the review says so in a way the user can act on ("add where you used it").

## The improvement plan, and why the check was dead

`review(..., suggested_projects=[...])` takes skill-gap advice and keeps it in
`improvement_plan`, never in the document. `unconfirmed_project_claims` then
checks the draft against that list.

Nothing in production supplied the list, so the plan was always empty and the
check never ran. It is now **derived** from the posting's own unevidenced skills
when no advice is passed, because the check is the point:

> the posting asks for Kafka → the master résumé has no Kafka → the tailored draft
> now mentions Kafka → `ok is False`, and the review names it.

This class is not covered anywhere else. `evidence.fabrication_violations`
compares employers, titles, employment dates, degrees, institutions,
certifications and numbers — a skill is none of those, so a draft that quietly
acquires one passes every deterministic check but the one above.
