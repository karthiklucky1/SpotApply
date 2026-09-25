# Five-job package pilot — 2026-09-25

> **Redacted for a public repository.** Candidate profile details are removed;
> see §1. The engineering findings (§4) and the recommendation (§8) are complete.

**Question.** Do researched application packages, including credible recruiter
connections, add enough value to build and charge for?

**Short answer.** The research layer found one real, decision-changing thing per
job — but almost none of it was the recruiter. What it actually found is that
**4 of the 5 jobs SpotApply delivered today are ones this candidate cannot
take**, and the product marked all 5 `eligible`. That is a supply-quality
finding, not a research-product finding, and it is worth more than the packages.

---

## 0. Provenance and limits

| | |
|---|---|
| Résumé used | `data/profiles/backend.md` and `ai_agents.md`, last changed 2026-08-29. **Not confirmed current** — production stores the uploaded résumé in Supabase storage, which was not read. |
| Profile / preferences | Live read, 2026-09-25, `userprofile` for the account owner |
| Jobs | Live read, 5 rows, `application.status='SHORTLISTED'`, all delivered 2026-09-25 |
| Posting text | `job.description`, full, as stored at ingest |
| **Employer postings NOT opened** | Every ATS host is blocked by this environment's egress policy. No posting was fetched directly. Live status comes only from SpotApply's own `job_liveness` rows, which exist for 2 of 5. |
| **Application form questions NOT seen** | Same reason. Task 4's "answer the real form questions" could not be done for any job. |
| Contact research | 5 web searches, one per employer, search-index snippets only. No page was fetched, so no evidence excerpt is direct-fetch verified. |

An access block is **not** evidence a vacancy closed. Three jobs below are marked
live-status *unverified*, not closed.

---

## 1. The pilot candidate (redacted)

**This repository is public, so the candidate's profile details are held
privately and not reproduced here.** The full unredacted pilot — including the
work-authorization analysis and the tailored résumé draft — was delivered
directly to the account owner. Only what the engineering findings depend on is
kept below.

What the findings turn on:

- Based in the US Midwest; **`open_to_relocation`: false**; `remote_ok`: true.
- `target_roles`: Software Engineer / Senior Software Engineer / Backend
  Engineer; `years_experience`: 3.
- Holds a **temporary, time-limited work authorization**, and the profile's
  authorization fields are **internally inconsistent** — one field asserts no
  sponsorship is required in a way that contradicts the stated status, and a
  date field is months in the past. Every eligibility verdict and every score
  below was computed against those inconsistent values. Resolving them is a
  human step, not a machine one.
- **`userprofile.key_skills` claims three technologies that appear nowhere in
  either résumé variant** (`data/profiles/*.md`). Three of the five jobs named
  exactly those technologies as requirements. Nothing in the tailoring drew on
  them.

The lesson that generalises: a profile can be internally contradictory and the
pipeline will score against it silently. There is no check that
`requires_sponsorship` is consistent with `work_authorization`, and no check
that `key_skills` is supported by the résumé the scorer actually reads.

## 2. Job packages

### JOB 1 — Senior Software Engineer I, Search and AI Platform · Elsevier (RELX)

**Verify.** Title *Senior Software Engineer I*; company stored as `Relx`
(actual hiring brand: Elsevier — see §4.3); req **R118682**; Workday;
`https://relx.wd3.myworkdayjobs.com/relx/job/Philadelphia-PA/Senior-Software-Engineer-I_R118682-2`;
full-time; senior IC with architecture leadership.
Posted 2026-09-25 · first seen 2026-09-25T15:28:17 · discovered 15:28:19.
**Live status: UNVERIFIED** — no `job_liveness` row; posting not fetched.

Sites (geo evidence, `ats_location`): Philadelphia PA, plus *Home based* in
Pennsylvania, New Jersey, Virginia, Maryland. **The candidate's state is not among them.**
`work_mode`: NULL.

Work authorization: **no sponsorship statement in the posting** → unknown.
Separately, `sponsorship_json` records a favourable *employer-level* signal:
USCIS 57 H-1B approvals FY2026, 98% approval rate. That is a company fact, not
a statement about this requisition.

**Should you apply — SKIP (hard eligibility blocker: location).**

Three strongest evidence-backed matches:
1. *"Designing, developing, and maintain generative AI services… using mostly
   Python"* → résumé: RAG pipelines with OpenAI, LangChain, FAISS,
   SentenceTransformers at NTT DATA; FastAPI inference at 2,500+ rpm, p95 <180ms.
2. *"Working within a Kubernetes (EKS) environment"* → Docker + Kubernetes
   production deployments at both employers (GCP/Vertex, not EKS specifically).
3. *"familiarity with modern AI/LLM tools and frameworks (e.g., LangChain,
   LangGraph)"* → LangChain is résumé-evidenced; LangGraph is not.

Gaps, separated:
- **Hard blocker:** no work location in the candidate's state, and
  `open_to_relocation: false`.
- **Hard-ish:** *"Deep expertise in Python and Java"* — Java is **not
  résumé-evidenced**. Claimed in profile `key_skills` only.
- Preference/learnable: 4–6 yrs asked vs 3 held; architecture leadership and
  mentoring asked, résumé is IC-shaped. LangGraph, knowledge graphs, EKS.

SpotApply scored this 72 and its own reasoning already named the seniority and
architecture-leadership gaps. It did **not** name the location problem.

---

### JOB 2 — Sr. Full Stack Engineer, Cloud Native – AIDR (Hybrid, Sunnyvale) · CrowdStrike

**Verify.** Req **R29845**; Workday;
`https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/USA---Sunnyvale-CA/Sr-Full-Stack-Engineer--Cloud-Native---AI-Detection-and-Response--AIDR---Hybrid--Sunnyvale-_R29845`;
full-time; senior; hybrid Sunnyvale CA. Salary $140,000–$215,000.
Posted 2026-09-25 · **first_seen 2026-09-21T19:50:55** (see §4.1 — this date is
contaminated) · discovered 2026-09-25T15:14:33.
**Live status: VERIFIED LIVE** — `job_liveness`: state LIVE, HTTP 200, checked
2026-09-25T15:16:41, `inconclusive_streak` 0. This is the only job with a
same-day positive liveness check.

**Should you apply — SKIP. Hard, explicit, non-negotiable eligibility blocker.**

Verbatim from the posting:

> "Must be eligible for CJIS clearance (requires U.S. citizenship or Green
> Card/permanent resident status)."

The candidate holds temporary work authorization. This is not a stretch, a preference or a learnable
skill — it is a stated citizenship/permanent-residence requirement. Two further
blockers stack on top: hybrid Sunnyvale against `open_to_relocation: false`, and
*"Comprehensive experience… with the Go programming language or other
object-oriented languages"* plus *"Comprehensive experience utilizing React.js"*
— neither Go nor React is résumé-evidenced.

Matches exist (REST/GraphQL APIs, CI/CD, git, large-scale production) but are
irrelevant against a citizenship gate.

**Product note:** SpotApply's own `rerank_reasoning` for this job reads *"CJIS clearance requires US
citizenship or green card; [status redacted] unclear"* — it
identified the blocker, scored the job **72**, and delivered it to the board
anyway.

---

### JOB 3 — Technical Consultant · TeamDynamix

**Verify.** Ashby; external id `a9fda5e3-60a8-4fa1-927f-9702fe7c7f61`;
`https://jobs.ashbyhq.com/teamdynamix/a9fda5e3-60a8-4fa1-927f-9702fe7c7f61`;
full-time; **Remote, USA**; $115K–$125K.
Posted 2026-09-25T14:29:01 · first seen 14:36:36 · discovered 14:43:09.
**Live status: VERIFIED LIVE** — HTTP 200, checked 2026-09-25T14:44:29.
Geo: `work_mode: remote`, country United States, evidence
`address.postalAddress.addressCountry`. **Location-eligible.**

Work authorization: no sponsorship statement → unknown. `sponsorship_json`:
*"No USCIS H-1B filing found for this employer name"* — which the record itself
qualifies as inconclusive because USCIS lists legal entity names.
`hire_probability_signals` shows `low_velocity_2_openings` — a small employer
with little visible hiring volume, which raises the sponsorship risk given an
OPT clock.

**Should you apply — STRETCH. The only location-eligible job of the five, and
it is off your stated target roles.**

Three strongest evidence-backed matches:
1. *"Strong understanding of enterprise integrations, APIs, and web services"* →
   résumé: FastAPI/REST/gRPC services, Kafka streaming, microservice
   architectures across multiple client deployments at NTT DATA.
2. *"Exposure to or hands-on experience with programming languages such as Java,
   JavaScript, Python, or PowerShell"* → Python and JavaScript are
   résumé-evidenced; the requirement is satisfied by an "or".
3. *"Experience with conversational AI or virtual service agent technologies (a
   plus)"* → RAG pipelines, multi-agent document extraction, LangChain, and the
   Volta verification platform.

Gaps, separated:
- **Not a hard blocker, but a real one:** *"1–3+ years of experience in
  professional services, consulting, solutions engineering"* and *"3+ years…
  working directly with customers in a technical environment."* The résumé shows
  stakeholder partnership at NTT DATA but **no customer-facing consulting
  role**. This is the load-bearing requirement and the weakest point.
- Preference/learnable: iPaaS and no-code/low-code platforms (preferred, absent);
  PowerShell (absent).
- **Career-direction question, not a skill gap:** this is a consulting/solutions
  role. Target roles are Software Engineer / Senior Software Engineer / Backend
  Engineer. Taking it is a lateral move out of engineering.

---

### JOB 4 — Engineer Sr Lead, Software · FIS

**Verify.** Req **JR0309198**; Workday;
`https://fis.wd5.myworkdayjobs.com/searchjobs/job/US-GA-ATL-201-STE-900/Engineer-Sr-Lead--Software_JR0309198`;
full-time; senior lead. Site: US GA ATL 201 STE 900 (Atlanta). `work_mode`: NULL.
Salary not stated. Posted 2026-09-25 · first seen 14:34:05 · discovered 14:43:09.
**Live status: UNVERIFIED** — no `job_liveness` row.

Work authorization: no sponsorship statement → unknown. `sponsorship_json`:
"No filing found", explicitly qualified as possibly-wrong-entity-name.

**Should you apply — SKIP as posted (location blocker). Would be the strongest
STRETCH of the five if it were remote or if relocation were on the table.**

This job has the **highest SpotApply score (78)** and genuinely the best backend
overlap:
1. *"Design and develop backend services, REST APIs, and microservices using
   Python and FastAPI"* → the single closest match in the whole pilot. FastAPI
   at 2,500+ rpm, p95 <180ms, microservice architectures, gRPC.
2. *"Build and maintain CI/CD pipelines using GitHub Actions, containerized
   deployments using Docker"* → GitHub Actions and Docker both résumé-evidenced.
3. *"cloud-native applications using Microsoft Azure and/or AWS"* → AWS and GCP
   both listed; Vertex AI and Kubernetes production experience.

Gaps, separated:
- **Hard blocker:** Atlanta GA on-site, `open_to_relocation: false`.
- **Substantial:** the role is *"primarily focused on front-end engineering using
  React, Next.js, TypeScript, and Tailwind CSS."* TypeScript is listed on the
  résumé; **React, Next.js and Tailwind are not evidenced at all.** This is the
  majority of the job.
- Preference/learnable: Terraform (absent), Sr Lead seniority vs 3 yrs.

The high score reflects the backend half. The posting says front-end is primary.

---

### JOB 5 — Associate, Software Engineer · Morgan Stanley

**Verify.** Req **JR036259**; Workday;
`https://ms.wd5.myworkdayjobs.com/external/job/New-York-New-York-United-States-of-America/Associate--Software-Engineer_JR036259`;
full-time; New York NY; *"Telecommuting permitted up to 2 days per week"*;
$150,000/yr stated (description gives $141,000–$150,000). `work_mode`: NULL.
Posted 2026-09-25 · first seen 14:36:11 · discovered 14:36:13.
**Live status: UNVERIFIED** — no `job_liveness` row.

**Should you apply — SKIP.**

**Inference, clearly labelled as such and worth verifying:** this posting has the
shape of an immigration-driven (PERM labour-certification) filing rather than an
open competitive search — an exhaustively enumerated skills list with a fixed
"Requires three (3) years of experience with the following skills:" preamble, a
single exact salary, a named legal entity ("Morgan Stanley Services Group,
Inc."), and a precise telecommuting allowance. I could not verify this: the
posting was not fetched and I have no filing record. Treat it as a reason to
check, not as a fact.

Gaps, separated:
- **Hard requirements absent from the résumé**, named explicitly: Angular, DB2,
  Camunda 7 and 8, Kotlin, Java, JPA/JDBC, Spring/Spring Boot. The posting asks
  for three years with each.
- **Hard blocker:** NYC with ≤2 days telecommute, `open_to_relocation: false`.
- Genuine overlaps exist (Python, SQL, MongoDB, Docker, Git, microservices,
  RESTful API, multi-threading, Agile) but they are the minority of the list.

SpotApply scored this 72 and its reasoning correctly named Angular, Camunda and
DB2 as concerns — then delivered it.

---

## 3. Contact research — the part that did not work

**Method.** One targeted search per employer using company + exact title + the
requisition ID + team language. Search-index snippets only; no page fetched.
Verification date for all: 2026-09-25.

| Job | Employer | SpotApply's own extracted contact data | Search result |
|---|---|---|---|
| 1 | Elsevier/RELX | req id only | No verified job-specific contact found |
| 2 | CrowdStrike | req id only (and it is the wrong employer's — §4.1) | No verified job-specific contact found |
| 3 | TeamDynamix | department + team: *Professional Services* | No verified job-specific contact found |
| 4 | FIS | req id only | No verified job-specific contact found |
| 5 | Morgan Stanley | req id only | No verified job-specific contact found |

**Named recruiters: 0 of 5. Named hiring managers: 0 of 5. Contact emails: 0 of
5.** `reporting_manager_name`, `recruiter_name`, `posting_creator_name` and
`contact_email` are NULL on all five rows. The only non-requisition context in
the entire set is TeamDynamix's department/team.

Two near-misses, both correctly excluded:
- CrowdStrike's posting contains `recruiting@crowdstrike.com`. That is an
  **accommodations mailbox** ("If you need assistance accessing or reviewing the
  information on this website…"), not a contact for this requisition. Using it
  for outreach would be both ineffective and a misuse.
- A Glassdoor snippet describes TeamDynamix's interview process as including "an
  initial screening call with a recruiter… an interview with the hiring manager."
  That establishes a *process*, not a *person*. No name.

This matches SpotApply's own prior evidence exactly: `docs/research/hiring-contacts-2026-09.md`
found 16 of 16 reporting statements title-only and 0 naming a person; the
production measurement across 45 shortlisted jobs found 2.2% named recruiter and
0/45 named manager. **Three independent samples, 66 postings, same answer.**

**Consequence for task 5 (outreach).** With zero credible job-specific contacts,
there is nothing to draft that would not misrepresent a relationship. No outreach
message was written for any of the five jobs. Writing one anyway — to a generic
company recruiter, or to someone who merely posted about the company — is
precisely the failure mode this pilot was meant to test for.

---

## 4. What the pilot found instead (the valuable part)

### 4.1 Requisition IDs are not globally unique, and two employers' jobs merged

`JobHiringContext`, `JobGeography` and `JobLiveness` are all keyed
`UniqueConstraint("source", "external_id")` (`app/db/models.py`), and the Workday
scraper sets `external_id = jobReqId` (`app/discovery/workday.py:249`). **A
Workday requisition ID is unique within a tenant, not across tenants.**

Live proof in this extract. CrowdStrike's hiring-context row
(`workday` / `R29845`) carries:

```
source_url: https://gn.wd3.myworkdayjobs.com/gn-careers/job/MN-Shakopee/
            Aud-Fitting-Support-Advisor_R29845-1
observed_at: 2026-09-21T19:50:54
```

That is **GN, a hearing-technology company, hiring an "Aud Fitting Support
Advisor" in Shakopee MN** — attached to a CrowdStrike senior engineering
requisition, because both are Workday `R29845`.

It is not only cosmetic. CrowdStrike's `Job.first_seen` is **2026-09-21T19:50:55**
— one second after GN's observation — while its `discovered_at` is 2026-09-25.
The freshness record was inherited from a different company's posting. `Job`
itself is keyed `("user_id", "source", "external_id")`, so the collision reaches
the job row too.

Blast radius: wrong-employer hiring context, wrong-employer geography and
liveness verdicts, and corrupted `first_seen` — which is the column the whole
"be first to apply" promise and the 5-day scoring window are built on.

The fix is a tenant-qualified key (Workday host/tenant slug + req id), not a
change to any cap or gate.

### 4.2 The eligibility gate passed four jobs the candidate cannot take

All five rows read `eligibility: eligible`. Four read
`eligibility_reason: "Located in United States; work mode not stated"`.

`app/common/eligibility.py:322` blocks on relocation only when work mode is
known:

```python
if geo.work_mode in ("onsite", "hybrid") and not prefs.open_to_relocation:
```

Every Workday job in this set has `work_mode: NULL`, so that branch never runs
and control falls to line 343, which returns eligible and appends *"work mode
not stated"*. `areas_json` is `[]` on all five, so the state/area rule does not
fire either.

Result, with `open_to_relocation: false` and the candidate's home state:

| Job | Work location | Offered remote? | Eligible per system | Actually takeable |
|---|---|---|---|---|
| 1 Elsevier | Philadelphia PA | home-based PA/NJ/VA/MD only | eligible | **no** |
| 2 CrowdStrike | Sunnyvale CA hybrid | no | eligible | **no** (also CJIS) |
| 3 TeamDynamix | Remote USA | yes | eligible | **yes** |
| 4 FIS | Atlanta GA | not stated | eligible | **no** |
| 5 Morgan Stanley | New York NY, ≤2 d/wk | no | eligible | **no** |

**80% of today's delivered board is unreachable for this candidate, and the
system says all of it is eligible.** This is the single largest quality problem
the pilot surfaced, and it is upstream of any packaging work.

### 4.3 Two smaller data-quality defects

- **Company names are slug-derived and wrong for outreach.** `Relx` (the hiring
  brand is Elsevier), `Ms` (Morgan Stanley), `Fis` (FIS), `Crowdstrike`,
  `Teamdynamix`. Any generated letter or message addressing "Ms" or "Relx" is
  visibly machine-made.
- **Salary scraped from the wrong geography.** Job 1 stores
  `$102,333 - $163,467`; the description says the **U.S. national** range is
  `$86,600 - $144,400` and that `$102,333 - $163,467` applies **if performed in
  New Jersey**. The job is posted for Philadelphia PA. The stored figure
  overstates the national range by ~19% at the floor.

---

## 5. Comparison table

| Job | Eligibility verdict | Live-status evidence | Job-specific contact? | Contact relationship | Application ready? | Missing information | Research effort | Measured cost |
|---|---|---|---|---|---|---|---|---|
| 1 Elsevier · R118682 | **SKIP** — no work location in the candidate's state, `open_to_relocation:false`; Java not evidenced | **Unverified** (no liveness row; posting not fetched) | No | — | No | Form questions; sponsorship statement; whether relocation is negotiable; Java depth | 1 search, 9 indexed sources | 1 Tier-2 final (see §6) |
| 2 CrowdStrike · R29845 | **SKIP** — explicit CJIS citizenship/GC requirement; temporary work authorization | **LIVE**, HTTP 200, 2026-09-25T15:16:41 | No | — (accommodations mailbox only, excluded) | No | Nothing that would change the verdict | 1 search, 9 indexed sources | 1 Tier-2 final |
| 3 TeamDynamix · a9fda5e3… | **STRETCH** — location-eligible; consulting experience is the gap | **LIVE**, HTTP 200, 2026-09-25T14:44:29 | No | — (dept/team only: Professional Services) | **Draft ready** (résumé); form questions unseen | Form questions; sponsorship position; whether a consulting pivot is wanted | 1 search, 9 indexed sources | 1 Tier-2 final |
| 4 FIS · JR0309198 | **SKIP as posted** — Atlanta on-site, `open_to_relocation:false`; front-end is primary and unevidenced | **Unverified** (no liveness row) | No | — | No | Form questions; is it remote-eligible; React/Next depth; sponsorship under legal entity name | 1 search, 10 indexed sources | 1 Tier-2 final |
| 5 Morgan Stanley · JR036259 | **SKIP** — Angular/DB2/Camunda/Kotlin/Java all absent; NYC ≤2 d/wk remote | **Unverified** (no liveness row) | No | — | No | Form questions; whether this is a PERM filing (inference only) | 1 search, 9 indexed sources | 1 Tier-2 final |

**Totals: 0 Apply · 1 Stretch · 4 Skip. 0 of 5 with a job-specific contact.
2 of 5 with positive live-status evidence. 0 of 5 with form questions seen.**

---

## 6. Cost — measured vs estimated

**Measured** (SpotApply's own token telemetry, per CLAUDE.md, $0.0025 per Tier-2
final at Haiku 4.5 list):

- 5 Tier-2 finals for these five jobs = **$0.0125**
- Tier-1 prescores: 5 calls. **Cost unmeasured** — CLAUDE.md states the Tier-1
  estimate in `spend.py` is still UNMEASURED. Not estimated here.
- Liveness checks: 2 HTTP requests. Measured p50 ~335 ms; negligible cost.

**Not measured, and deliberately not invented:**

- My own research cost for this pilot. I can report effort in units I actually
  observed — **5 web searches, 46 indexed sources returned, 1 database extract,
  0 postings fetched** — but I have no token accounting for this session and will
  not convert it to dollars.
- Time saved versus doing this by hand. Not measured. No baseline exists.
- Reply rates, referral rates, interview conversion. **Zero outreach was sent, so
  there is no data and none is claimed.**

---

## 7. Pilot findings

**How many of the five had genuinely job-specific contact evidence?** **Zero.**
No named recruiter, no named manager, no requisition-linked person, no usable
contact email.

**How many had only general company contacts, or none?** All five had none worth
using. One (CrowdStrike) had a general accommodations mailbox, excluded on
purpose. One (TeamDynamix) had a department and team name but no person.

**What existing SpotApply data could be reused?** Substantially more than
expected, and it is the strongest part of the stack:

- `job.description` — full text at ingest. This is what made task 2 possible
  without fetching anything.
- `job_geography` — structured country, every site untruncated, evidence field
  cited. Gave the Elsevier home-based-states list, which is what decided that job.
- `job_liveness` — real HTTP evidence with timestamps, where it ran (2 of 5).
- `job_hiring_context` — requisition IDs (3 of 5, wrong employer on 1) and
  department/team (1 of 5). Everything person-shaped is NULL.
- `sponsorship_json` — USCIS-backed employer-level H-1B signal with approval
  counts and an honest "no filing found ≠ no sponsorship" caveat.
- `rerank_reasoning` — already names real concerns, including CrowdStrike's CJIS
  gate. The reasoning was right; the delivery decision ignored it.

**Which steps can be automated reliably?**
- Extract and normalise posting facts (title, req id, sites, salary, employment
  type) — already works, with the two defects in §4.3.
- Structured geography and eligibility from ATS fields — works when `work_mode`
  is populated; silently degrades when it is not (§4.2).
- Liveness re-checks — works; coverage is the problem, not correctness.
- Requirement-vs-résumé gap extraction with verbatim quotes — the most valuable
  automatable step, and the one this pilot leaned on hardest.

**Which need human review?**
- Work-authorization eligibility. The three profile contradictions in §1 are not
  machine-resolvable and gate everything.
- Whether a stretch is worth taking (Job 3's consulting pivot).
- Any claim about a named person.
- Seniority judgements ("Sr Lead" vs 3 years) — a score cannot settle this.

**Which should not be promised?**
- **"We find the hiring manager."** 0/5 here, 0/16 in the September probe,
  0/45 in production. Three samples, 66 postings. Do not sell this.
- **"Verified live at time of application."** Only with real liveness coverage;
  3 of 5 had no check at all.
- **"Answers to the application questions."** Requires reading the live form.
  Not possible without fetching the posting, and not possible at all for
  authenticated multi-step Workday flows.
- Any reply-rate or interview-rate claim. No data.

**Directional only.** Five jobs, one candidate, one day. This establishes nothing
about market-wide contact coverage or willingness to pay, and the four SKIPs are
driven by one candidate's `open_to_relocation: false` — a different candidate
would see a different board.

---

## 8. Recommendation — one experiment

**Do not build the recruiter-connection product.** Three independent samples
totalling 66 postings say the public record does not contain the person. Building
it means either shipping "assignment unconfirmed" guesses or buying a vendor feed
that resells the same LinkedIn poster field. Neither is worth $100/month, and
attaching a name to every job is the failure mode, not the feature.

**Build instead: the eligibility pre-flight.** The pilot's own finding is that
4 of 5 delivered jobs were unreachable. Fixing that improves every user's board
immediately, costs no LLM spend, and is the precondition for any paid research
layer — researching a job the candidate cannot take is worth less than nothing.

**Scope** (smallest version):
1. Populate `work_mode` for Workday from its own structured fields, so
   `eligibility.py:322` can actually run. Where it genuinely cannot be
   determined, decide `unknown` and **hold**, rather than `eligible`.
2. Treat a site list with no site in the candidate's commutable area as a
   location decision when `open_to_relocation` is false — Elsevier's
   PA/NJ/VA/MD list should have blocked Job 1 without needing `work_mode`.
3. Tenant-qualify the posting key so `R29845` cannot merge two employers (§4.1).
4. Surface the résumé-vs-`key_skills` divergence to the user once (Java, React,
   Spring Boot claimed in profile, absent from résumé), because three of five
   jobs turned on it.

**Acceptance checks:**
- Re-run today's five: Jobs 1, 2, 4, 5 decide `ineligible` or `unknown` with a
  reason naming the location; Job 3 stays `eligible`.
- A Workday posting and a same-req-id posting from a different tenant produce
  two rows in `job_hiring_context`, not one. Guard test.
- No job reaches the board with `eligibility_reason` containing "work mode not
  stated" while `open_to_relocation` is false.
- Zero change to any cap, gate, price or plan limit.

**Estimated operating cost:** zero marginal LLM spend — this is deterministic
field plumbing. It should *reduce* spend by removing ineligible jobs before
Tier-2, in the same direction as the documented "queue was 73% ineligible"
finding. Engineering time only. **Estimate, not measured.**

**How to test willingness to pay with existing users, honestly:** do not ask.
Ship the pre-flight, then measure the one behaviour that already exists and
costs nothing to observe: **`Application.viewed_at` and the shortlist→tailored
transition rate, before and after.** Today all five jobs have
`viewed_at: NULL`. If a board the user can actually act on lifts opens and
tailors, that is a real signal. Only then put a price on a research add-on, and
put it in front of users whose open rate moved. A pricing question asked before
the board is trustworthy measures the wrong thing.

---

## 9. Candid answer on the $100/month question

This pilot does not justify charging $100/month for researched packages, and the
reason is not that the research is bad — the requirement-vs-résumé analysis in
§2 is genuinely useful and would have saved this candidate from four
applications. The reason is that **four of the five jobs should never have
reached the board**, and a research layer on top of a board like that multiplies
effort against the wrong inventory.

The recruiter component specifically should not be sold at any price on this
evidence.

Fix supply first. The packaging is the easier half and it will be worth more
once the board is worth researching.
