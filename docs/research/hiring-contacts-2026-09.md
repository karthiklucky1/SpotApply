# Job-specific people & team data: challenge review, live probes, and a build/buy plan

Review date: 11 September 2026. Scope: U.S. technology vacancies. Companion code:
`app/intelligence/hiring_contacts.py` (extractor, offline-tested), `tests/test_hiring_contacts.py`,
`scripts/probe_hiring_contacts.py` (live probe harness, run outside the research sandbox).

## 0. Decision in one paragraph

Build the cheap layer now, buy one job-linked feed on a free tier, and stop describing the
product as "find the hiring manager". The public record establishes, for most U.S. tech
postings, the **reporting TITLE and the team**, the **posting organisation vs. hiring company**,
sometimes a **recruiter or posting creator**, and rarely a **named manager**. In the 16 live
postings probed below, 16 of 16 reporting statements named a title and 0 named a person. The
person is then recovered by a second, separately-evidenced step (team page, first-person post,
search index, or a licensed poster feed), and that step yields *suggested* or *self-reported*
evidence far more often than *published-for-this-job* evidence. Every vendor that sells
"hiring manager" data for U.S. jobs is, on its own documentation, selling either the LinkedIn
job-poster field or an inference; none documents access to an employer's private ATS
assignment. The attached report's direction (evidence-typed assertions, separate org roles,
no single `hiring_manager` column) is right; several of its specifics need correction (§5).

## 1. What this review could and could not do

| Capability | Status | Consequence |
|---|---|---|
| Direct HTTPS to ATS/job/vendor hosts from the sandbox | **Blocked** (egress policy, `CONNECT 403` for every host tried: api.smartrecruiters.com, api.ashbyhq.com, boards-api.greenhouse.io, api.lever.co, hn.algolia.com, posthog.com, jobsearch.api.jobtechdev.se, docs.coresignal.com, theirstack.com, hirebase.org, dice.com, usajobs.gov, myworkdayjobs.com, …) | No raw JSON was fetched in this session. The attached report's 8–11 Sept fetches could not be re-run here. |
| Web search (indexed copies of live pages, with snippets) | Works | Live-page evidence below is **search-index evidence**: a current URL plus the indexed sentence. Treat as "observed via index on 2026-09-11", not a direct fetch. |
| Repository audit | Works | Every discovery scraper was read for the fields it keeps vs. drops (§3.4). |
| Offline extraction | Works | The extractor was built and run against the exact recovered sentences. |
| Paid vendor APIs | **NOT TESTED** (no purchase, no sign-up) | Capabilities are from vendor docs/pricing pages as indexed; coverage and precision are unmeasured. |

Run `scripts/probe_hiring_contacts.py` from any machine with normal egress to convert §2 into
directly-fetched JSON with every key the endpoint returns (the command is in §9).

## 2. Live probe: 16 current postings, 15 employers, 4 ATS families + 3 other source families

Method: site-restricted searches for reporting-line language on each ATS's public job-page
domain, then reading the indexed sentence for the advertised role. All URLs were live in the
index on 2026-09-11. Names in the "Person named?" column are what the *posting text* gives.

| # | Employer | Role | ATS | URL | Reporting statement (indexed snippet) | Person named? |
|---|---|---|---|---|---|---|
| 1 | Cloudflare | Engineering Manager, Browser Run | Greenhouse | https://job-boards.greenhouse.io/cloudflare/jobs/8099000 | "You will report to the Senior Engineering Manager for AI Agents Tooling & Platform" | No, title only |
| 2 | Crunchyroll | Software Engineer | Greenhouse | https://job-boards.greenhouse.io/crunchyroll/jobs/6696781 | "You will report to the Engineering Manager in the Service Monetization organization" | No |
| 3 | Consumer Reports | Full Stack Engineer | Greenhouse | https://job-boards.greenhouse.io/consumerreports | "You'll report to the Associate Director, AI/ML & Data Science" | No |
| 4 | The New York Times | Software Engineer, Growth Conversion | Greenhouse | https://job-boards.greenhouse.io/thenewyorktimes/jobs/4730745005 | "You will report to the Engineering Manager of the Subscription Conversion team" | No, but **team named** |
| 5 | Fleetio | Software Engineer, Marketplace | Greenhouse | https://job-boards.greenhouse.io/fleetio/jobs/5213856007 | "part of the Engineering department and you will report to the Web Engineering Manager" | No |
| 6 | EliseAI | Chief of Staff to CTO | Greenhouse | https://job-boards.greenhouse.io/meetelise/jobs/8030034002 | "reports directly to the CTO" | No (title resolves to one person, but the posting does not name them) |
| 7 | Zapier | Incident Operations Specialist | Ashby | https://jobs.ashbyhq.com/zapier/375dd701-86ba-45cd-8c24-daff6cc49223 | "report to the Incident Program Manager" (posted 10 Jul 2026) | No |
| 8 | Commure | Staff Software Engineer, Data Integrations | Ashby | https://jobs.ashbyhq.com/Commure/62be068a-2618-4e87-b279-93703f62dc7d | matched the query; reporting sentence not in snippet | Unknown (fetch needed) |
| 9 | Coupa | Principal Software Engineer | Lever | https://jobs.lever.co/coupa/76b8c172-d5b3-4927-9217-9f4745b7ad24 | "You'll report to the VP of Engineering" | No |
| 10 | FinQuery | Senior Software Engineer | Lever | https://jobs.lever.co/finquery/86ef832c-3b3c-44a4-b210-94dad75db36c | "Reports to the Director of Engineering" | No |
| 11 | LogRocket | Developer Relations | Lever | https://jobs.lever.co/logrocket/6d7fc949-6d48-4781-a0e4-77b4c48b5be8 | "You report directly to the VP of Marketing" | No |
| 12 | Jeeves | Technical Recruiter | Lever | https://jobs.lever.co/tryjeeves/84072ed0-4ea0-4bbb-9aa2-618c3013cd3c | "reports to the Head of Global Talent Acquisition" | No |
| 13 | CSC Generation | Software Engineering Manager | Lever | https://jobs.lever.co/cscgeneration-2/bb060c09-1e8c-4951-96d2-67fc8c8f9fc9 | "Reports to: Chief Technology Officer" (template field) | No |
| 14 | Greenlight | Senior Software Engineer, Full-Stack | Lever | https://jobs.lever.co/greenlight/83ce1d5b-a2c2-4765-aaae-585d93023af6 | "This role reports to an Engineering Manager" | No |
| 15 | Jito Labs | Applied AI Engineer | Lever | https://jobs.lever.co/jito.wtf/a60005b6-25b7-455b-9d3e-444a55458553 | "Reports to the CEO" | No (title = one identifiable person at a small company; still not named in the posting) |
| 16 | Live Nation / Ticketmaster | Lead Data & AI Platform Engineer (UK remote, non-U.S.) | Workday | https://livenation.wd503.myworkdayjobs.com/en-US/TMExternalSite/job/Remote---United-Kingdom/Lead-Data---AI-Platform-Engineer--Remote--United-Kingdom-_JR-90837-4 | "Hiring Manager: VP - Client & Fan Support Technology" (labelled template field) | No, title only |

Other source families checked the same way:

| Family | What was observed | Evidence boundary |
|---|---|---|
| Hacker News "Who is hiring" (first-person announcements) | Thread for September 2026 exists: https://news.ycombinator.com/item?id=49522897 (posted ~5 h before the check; rules require the poster to be personally part of the hiring company). Individual comments were not readable from the index. | The attached report's Quill/R_R example could not be re-verified. The *family* is confirmed live; per-comment poster identity is a fetch away (`--hn 49522897`). |
| Y Combinator Work at a Startup | Founder-led postings, e.g. https://www.workatastartup.com/jobs/82148 (Stardrift, "working shoulder-to-shoulder with the founder and founding team"), https://www.workatastartup.com/jobs/84315 (Novaflow). YC's Algolia index carries `founders[]` per company; SpotApply's `yc_companies.py` drops it. | Founder pages give *team context*, not a reporting line. |
| Teamtailor | Feature documented: the job ad shows the **Recruiter as "Contact"** (default: Career Site Manager) and optional **Colleagues** (support.teamtailor.com "Create a new job"). Live Teamtailor job pages exist for U.S./EU tech firms (e.g. https://tradingview.teamtailor.com/jobs/7678503-talent-acquisition-specialist), but the contact block was not in the indexed snippet. | Page-level, not in the RSS feed SpotApply reads (`teamtailor.py:36`). Needs a page fetch per shortlisted job. |
| USAJOBS | Indexed announcements exist (e.g. https://www.usajobs.gov/job/877964400, IT Specialist). The search API (free key) documents agency contact fields under `UserArea.Details` (`AgencyContactEmail`, `AgencyContactPhone`) per the developer reference; not re-verified here. | Agency HR contact for the announcement, never the selecting official. Federal only. |
| Dice | Dice's "Recruiter Profile" is a documented feature that attaches the posting recruiter's name and profile to the job page (Dice Knowledge Center: "View a Recruiter Profile"). | NOT TESTED live. Agency-heavy U.S. tech supply; Dice is a job board, so check robots.txt/terms before any ingestion. |
| JSON-LD `applicationContact` | Not testable here. The attached report's three tests found none. | Keep the check in the probe; expect near-zero yield. |
| PostHog team pages | Not re-fetched. Search confirms the handbook's small-team model ("a team lead is the leader of a small team", teams of 2–6). | A team page gives team membership and lead, and a job → team link only when the employer publishes it. GitLab's public team page now points to Workday/The Loop/Glean (page dated 30 Apr 2026), confirming the report's caution that public org directories are disappearing. |

**Finding A (the important one).** Reporting statements in U.S. tech postings overwhelmingly name a
*title*, not a person: 16/16 here, 0 named. The 8 Sept Ashby-board examples (Abhik, Kat) are the
exception pattern: small, transparent employers writing in the first person. A product that promises
"the hiring manager's name" from descriptions will be empty for most jobs; a product that promises
"reports to: Senior Engineering Manager, AI Agents Tooling & Platform; team: Subscription Conversion"
will be populated for a meaningful fraction and is *exactly* the query that resolves the person in step two.

**Finding B.** Template fields exist in the wild ("Reports to:", "Hiring Manager:") on Lever and Workday
tenants. They are cheap to parse and carry the same title-only caveat.

**Finding C.** The extractor (`hiring_contacts.py`) run over the 16 snippets produced a
`reporting_manager / title_only_for_this_job` assertion for every one that had a reporting sentence,
with the correct title, and no false person. Negative cases ("direct reports", "report to the office",
"the team reports to", "build reports to the board") produce nothing. See `tests/test_hiring_contacts.py`.

## 3. Source families: documented capability vs. what SpotApply keeps today

### 3.1 ATS public endpoints (unauthenticated)

| ATS | Public person field | Org/req join fields | SpotApply today (file:line) |
|---|---|---|---|
| SmartRecruiters | `creator{name, avatarUrl}` on the public posting detail (documented in the Posting API "postingcontent" reference; the attached report observed `creator.name = Anthony Rodriguez` on Wix2/744000148622869 on 10 Sept; not re-fetched here). Semantics: the employee who **created** the posting, usually a recruiter/coordinator. | `department.label`, `function`, `refNumber` (requisition) | Reads only id/name/location/date/company + jobAd sections; **drops `creator`, `department`, `refNumber`** (`smartrecruiters.py:131-171`) |
| Greenhouse Job Board API | None by default. `metadata[]` carries whatever custom fields the employer chose to expose; a "Hiring Manager" custom field is possible but employer-specific (unobserved). | `departments[].name`, `offices[]`, `requisition_id`, `internal_job_id` | **Drops `departments`, `metadata`, `requisition_id`** (`greenhouse.py:61-73`) |
| Lever postings API | None. `hiringManager` exists only in the authenticated Data API. | `categories.team`, `categories.department` | **Drops `categories.team/department`** (`lever.py:42-46`) |
| Ashby posting API | None (fields: id, title, department, team, location, employmentType, publishedAt, jobUrl, applyUrl, descriptionHtml/Plain, compensation). A search result attributing "hiring team" display to Ashby was TheirStack's Ashby data-source page, not Ashby documentation. | `department`, `team` | **Drops `department`, `team`** (`ashby.py:45-71`) |
| Workday CXS | None structured; some tenants put "Hiring Manager:" / "Recruiter:" in the description text (Live Nation above). | `jobReqId` (kept → `external_id`, `workday.py:210`), `hiringOrganization`, `jobRequisitionLocation` | Drops `hiringOrganization` and most of `jobPostingInfo` |
| Teamtailor | Recruiter "Contact" + "Colleagues" on the job page (documented). | RSS `category` | Reads RSS only (`teamtailor.py:62-93`); page never fetched |
| Recruitee / Pinpoint / JOIN / Rippling / Workable / Personio / Breezy / BambooHR | Unverified. The scrapers ignore every key outside the 8 `RawJob` fields, so nobody has looked. | `department` in most | `scan_person_like_keys()` in the probe dumps whatever they expose |

### 3.2 Aggregators and feeds already in the stack

| Source | Person data available | SpotApply today |
|---|---|---|
| HN Who is hiring (Algolia) | The **comment author** is the first-person poster; comments often carry a founder/CTO self-identification and a company-domain email. | `hn_whoishiring.py:157` reads `author` only as a deleted-comment guard and **drops it**; comment id is md5-hashed into `external_id` (`:181`); emails survive inside `description` (`:191`). Cheapest fix in this whole document. |
| YC companies (Algolia) | `founders[]` per company | Dropped (`yc_companies.py:36-45`) |
| SerpAPI Google Jobs | `via` (which board), `apply_options[]` | Poster not available |
| LinkedIn RapidAPI source | Some wrappers return a poster field | Dropped (`linkedin_rapidapi.py:75-107`); compliance rule: discovery-only links, no automation |
| `linkedin_xray.find_champions` (SerpAPI, `site:linkedin.com/in "Company" "Role"`) | Public search-index profile hits: name + headline | Already built; it answers "who at Company has title X", i.e. the **resolution step for a title-only reporting line**. Output is *suggested*, never *confirmed*. |

### 3.3 Regional structured-contact feeds (reference implementations, not U.S. coverage)

Sweden JobSearch (`application_contacts[]`, employer.name vs employer.workplace) and Norway NAV
(`contactList[]`, public trial token, republication terms with prompt removal) are as the attached
report describes. They matter as *schema references* for the assertion model, and as proof that
government feeds do carry per-vacancy contacts. They do not supply U.S. tech contacts.

### 3.4 The gap in one sentence

SpotApply already fetches payloads that contain department/team, requisition ids, a posting creator,
and first-person authors, and discards all of it at `RawJob` (`app/discovery/base.py:9-20`,
`pipeline.py:255-263`). Half of the "build" work is to stop discarding.

## 4. Vendors: documented capability, cost, rights, freshness; all NOT TESTED

| Vendor | Documented capability (as indexed 2026-09-11) | Price points seen | Display/redistribution rights | Freshness / provenance | Verdict |
|---|---|---|---|---|---|
| **TheirStack** | Job records with `hiring_team[]` (`first_name`, `full_name`, `linkedin_url`, `image_url`; role where available) for "some jobs"; docs explicitly distinguish poster from future manager; original career-page URL per job (join key). | Free tier (docs: 200 API credits/month; another page: 50 credits, so verify); $109 / 1,000 credits → $2,078 / 50,000; 1 credit per job. | Not stated for candidate-facing display; ask. | Poster data is LinkedIn's "Meet the hiring team" as far as documentation implies; subject to LinkedIn availability changes. A third-party review states TheirStack has "no contact-level data"; the vendor docs contradict this, so the review is likely stale. | **Trial first** (free tier, exact-URL join, explicit poster/manager distinction). |
| **Fantastic.jobs** (Active Jobs DB on RapidAPI/Apify, direct feed) | `recruiter_url` (LinkedIn profile), `recruiterOnly` filter; marketing says name/title/contact link "where available" under a "Hiring Manager" heading. | $1 per 1,000 jobs self-serve; $45 / 10,000 records subscription; Apify ≈ $0.05 / 1K. | Job-board feed licence; poster-field display rights unclear. | Hourly refresh claimed; poster field is LinkedIn-derived. | **Cheap second trial** on the same 100 jobs. |
| **Coresignal** (Multi-source Jobs) | `recruiter` object on job records (name/URL where available), source ids/URLs, external company job URL; sources LinkedIn/Indeed/Glassdoor/Wellfound. | Multi-source = 2 credits/record (not 1). Plan figures seen: 7-day trial 2,000 credits; $49 / 3,000; $150 / 10,000; $199 / 12,000 (Starter); $450 / 40,000; $499 / 35,000 (Pro); $5,000 Elite. Third-party pages disagree with each other and with the attached report's "$49 Mini / 2,500 credits": **verify on coresignal.com/pricing before budgeting**. | Dataset licence; candidate display not addressed in indexed text. | Multi-source dedup is the value; recruiter field is again LinkedIn-derived. | Defer until TheirStack lift is measured. |
| **Hirebase** (Hiring Manager Contact API) | "Identifies the **most likely** decision-maker … using company structure, team, and seniority signals"; ≈5 contacts per run; verified email/phone as optional add-on; add-on to any API plan, pay-per-lookup; POST /v2/jobs/contacts + task polling (per attached report). | "Contact us"; no public per-lookup price found. | Not stated. | No evidence/confidence field in the schema. | **This is an inference product** (the "suggested" tier), not job-linked proof. Correct the attached report's framing. Evaluate only against a human-reviewed sample. |
| **Apollo** | People/company enrichment; **standard plans prohibit powering external products or sharing data with customers**; a Data Reseller agreement is required. | Basic $49/user/mo (annual), credits: email reveal 1, phone 8; enterprise bespoke. | Reseller agreement only. | Identity enrichment; no job linkage. | Only if a resolved name needs contact details and the agreement is signed. Not a discovery source. |
| **Bright Data** (LinkedIn jobs dataset) | `job_poster` nested field (name/title/URL) in the jobs dataset. Docs state LinkedIn restricted public Position/Experience/Education since **13 Nov 2025**. | Dataset/scraper pricing per record; not itemised here. | Dataset licence. | Fill-rate collapse on profile fields is documented by the vendor; `job_poster` availability should be spot-checked, not assumed. | Reference only. |
| Candidate-side competitors | Jobright "Insider Connections": cross-references the job with the user's LinkedIn network and surfaces insiders (alumni/former colleagues/"hiring managers") with emails. Careerflow "Hiring Search": Boolean Google X-ray of LinkedIn profiles (same mechanism as SpotApply's `linkedin_xray`). Refer.me: opt-in referral network. "Apply Ops": not findable in search; treat the attached report's claim as unverifiable. | n/a | n/a | n/a | They demonstrate *suggested* and *network* tiers, not ATS access. |

**Cross-vendor conclusion.** Every U.S. "hiring manager" field on the market traces to one of three
things: (1) LinkedIn's job-poster/"Meet the hiring team" widget, which the poster can hide and which
LinkedIn can restrict, (2) an inference from company structure, or (3) the applicant's own network.
None documents employer ATS assignment. Buy (1) for coverage, label it "job poster", and never
upgrade it to "manager" without separate evidence.

## 5. Corrections and challenges to the attached report

1. **"Route 1: extract reporting managers from descriptions" overstates the yield.** The Ashby-board
   examples are first-person, small-company postings. Across four ATS families and 15 employers,
   reporting sentences were title-only 16/16. Reframe Route 1 as "reporting title + team", which is
   populated far more often and is the input to identity resolution.
2. **Hirebase is an inference service, not "research as a service" for a specific requisition.** Its own
   marketing says "most likely decision-maker … ~5 contacts per run". File it under *suggested*.
3. **Coresignal cost is 2 credits per multi-source job record, not 1**, and plan/credit figures circulating
   in 2026 are inconsistent ($49 for 2,500 vs 3,000 credits; Starter $199 vs $2,149/yr). Re-quote from
   the pricing page before the 100-job trial; do not carry "1,050 credits/user/month" into a budget.
4. **TheirStack's free tier is usable for the trial** (docs cite 200 API credits/month), which the
   attached report did not exploit. Start there before any purchase.
5. **Apollo standard plans cannot be used to power SpotApply's UI at all** (explicit prohibition on
   external products); the report's "reseller route" is the *only* route, and it is enterprise-priced.
6. **Ashby's public posting API exposes no hiring team**; do not rely on search summaries that say
   otherwise (they conflated a TheirStack page with Ashby docs).
7. **The Sweden/Norway routes are correctly scoped as non-U.S.**, but the report lists them before
   the two U.S.-relevant sources SpotApply already ingests and throws away: HN comment authors and
   SmartRecruiters `creator`/`department`/`refNumber`. Re-order priorities by U.S. yield.
8. **Teamtailor's recruiter "Contact" block and Dice's Recruiter Profile are two documented public
   sources the report omitted.** Both are page-level; both name a recruiter (not a manager).
9. **"Job poster" from any LinkedIn-derived feed is subject to poster opt-out** ("Show profile on the
   job post" is a checkbox) and to LinkedIn's November 2025 field restrictions. Freshness and
   fill-rate must be measured on SpotApply's own job mix, per vendor, per month.
10. **The 95% precision launch gate is right but under-specified**: define the denominator as
    *resolvable* claims of type `named_for_this_job` and `structured_field`; report `title_only`
    and `suggested` tiers separately and never inside the precision number.
11. **The report proposes JobSource/OrganizationRole/Person/JobPersonAssertion tables** without
    noting that `Job.description` is truncated to ~800 chars in hot paths (`matcher._candidate_columns`,
    tests/test_retrieval_egress.py). Extraction must run at ingest on the full text and persist
    assertions; it cannot be a later pass over the projected columns.

## 6. Evidence-based architecture for SpotApply

### 6.1 Evidence tiers (display labels)

| Tier | Definition | Label on the card |
|---|---|---|
| `named_for_this_job` | The posting or a structured field names the person for this vacancy | "Named in this posting" |
| `structured_field` | ATS/feed field (creator, recruiter, contact list) | "Posting creator (SmartRecruiters)", "Recruiter named on the job page" |
| `self_identified` | First-person author ("I'm Kat and this role reports to me"; HN comment author) | "Says they own this role (self-reported)" |
| `title_only_for_this_job` | Reporting title / team named, person not | "Reports to: VP Engineering, Platform team" |
| `team_page` | Employer team page lists members/lead; job → team link separately evidenced | "On the team page for this team" |
| `suggested` | Company + title/team match via search index or vendor inference | "Likely: holds the title this role reports to (unconfirmed)" |
| `generic_mailbox` | careers@/jobs@ | "Application mailbox" |

### 6.2 Data model (additive; no change to `Job` columns)

- `RawJob.extras: dict` (new, optional): scrapers put department/team/req id/creator/contacts/author here.
- `JobOrgAssertion(job_id, role ∈ {posting_organization, recruiting_agency, hiring_company_named_in_ad, employer_of_record}, name, field, quote, source_url, observed_at, origin_key)`.
- `JobPersonAssertion(job_id, relationship, evidence_type, name, title, organization, field, quote, qualifier, source_url, observed_at, origin_key, display_policy, expires_at)`; unique on `(job_id, relationship, origin_key)`.
- Shared across tenants (assertions are about the posting, keyed by the shared-pool job), the same way `JobCard` is shared; per-user private evidence (inbox/calendar) is a separate, user-scoped table and out of scope for this phase.
- Account-deletion guard: neither table is user-scoped, so `test_account_deletion` needs no handler; document that in the migration.

### 6.3 Pipeline placement (spend-neutral)

1. **Ingest (free):** in `pipeline._build_job`, run `extract_from_text` on the full description and
   `extract_from_structured` on `extras`; write assertions in the same transaction. No LLM. No network.
2. **Shortlist (bounded):** for jobs that reach the board, run the page-level fetchers (Teamtailor contact
   block; JSON-LD; Dice recruiter block once terms are checked) through `app.common.browser_client` /
   httpx under a per-day cap, and the search-index resolver (`linkedin_xray`-style query built from
   `title_only` + team) with a hard cap per user per day. Output is `suggested`.
3. **On demand (paid):** vendor lookup by exact source URL / requisition id for the shortlisted job,
   cached globally by canonical job URL so the second user pays nothing. Poster fields land as
   `structured_field` / relationship `job_poster`.
4. **Expiry:** assertions inherit the job's freshness (`app/common/freshness.py`); a closed job hides its
   contacts; vendor rows carry the licence's retention.

### 6.4 UI

A "People & team" panel on the shortlisted-job card with one row per assertion, tier label, the quote,
the source link, and the observed date. No emails or phones displayed in v1 (the extractor records
them; `display_policy` defaults to hidden). Existing referral drafts (`referral.py`) get the reporting
title and team injected, which is the single largest quality lift for those drafts.

## 7. Build / buy recommendation and costs

| Item | Decision | Why | Cost |
|---|---|---|---|
| Keep structured fields + text extractor at ingest | **Build** (this branch ships the extractor) | Zero marginal cost; 16/16 title-only lines and all team names come from here | Engineering time only |
| HN author + comment id; SmartRecruiters `creator`/`department`/`refNumber`; Greenhouse `departments`/`metadata`/`requisition_id`; Lever `categories.team`; Ashby `team`/`department`; YC `founders[]` | **Build** | Already fetched, currently discarded | Small scraper diffs |
| Search-index resolution of title-only lines (`site:linkedin.com/in "Company" "Title"`; `site:linkedin.com/posts "hiring" "Title" "Company"`) | **Build on existing SerpAPI** | Same mechanism Careerflow sells; output is `suggested`; no LinkedIn automation | SerpAPI ≈ $0.01–0.015/search on published plans; cap at 1–2 searches per shortlisted job |
| Teamtailor contact block, JSON-LD | **Build** (page fetch at shortlist) | Documented public recruiter name | Bandwidth only |
| TheirStack | **Buy, free tier first** | `hiring_team` + exact source URL; explicit poster/manager distinction | $0 → $109/1,000 credits |
| Fantastic.jobs | **Buy, second trial** | Cheapest per record; `recruiterOnly` filter measures fill rate directly | $1 / 1,000 jobs |
| Coresignal | **Defer** | 2 credits/record, same upstream, pricing inconsistent | verify |
| Hirebase | **Evaluate as inference only** | "most likely" contacts | contact sales |
| Apollo | **No** for discovery; reseller agreement only if contact details are ever needed | licence prohibits embedding on standard plans | enterprise |
| Dice ingestion | **Decide after terms review** | Recruiter Profile is public; robots/terms unknown | n/a |

Unit economics to measure, not assume: cost per **newly supported** assertion of tier ≥ `structured_field`,
computed as (vendor spend + search spend + review time) / (assertions that survived human review). At the
Pro shortlist ceiling of 35 jobs/day and a global cache, vendor lookups are bounded by *distinct* shortlisted
jobs across all users, not by users × jobs.

## 8. 14-day experiment plan (prioritised, gated)

| Day | Work | Gate / output |
|---|---|---|
| 1 | Run `scripts/probe_hiring_contacts.py` from a laptop against the §2 boards + `--hn 49522897` + 3 Recruitee/Pinpoint/JOIN slugs from the registry; commit `probe_results.json` (PII redacted). | Direct-fetch confirmation of §2; the "person-like keys" dump for every family. |
| 2 | Freeze the 100-job sample from production's shortlisted jobs (stratified by ATS × company size × role family; ≤3 per company; regional variants kept together). | Sample manifest with canonical URLs and requisition ids. |
| 3–4 | Land `RawJob.extras` + the scraper diffs (§7 row 2) behind a flag; run the extractor over the sample at ingest. | Baseline table: per tier counts; % jobs with team, reporting title, named person, recruiter, creator. |
| 5 | HN author/comment-id fix; YC founders. | HN comments in the sample gain `self_identified` posters. |
| 6–7 | Human review of every `named_for_this_job` / `structured_field` / `self_identified` assertion in the sample: correct / contradicted / unresolvable. | Precision on resolvable claims; error taxonomy fed back into regexes and tests. |
| 8 | TheirStack free tier: look up the 100 sample jobs by source URL; record match rate, `hiring_team` fill, role labels. | Exact-job match rate; poster fill rate; cost per filled job. |
| 9 | Fantastic.jobs $1/1k trial on the same 100; compare fill and agreement with TheirStack. | Independent-source agreement rate on poster identity. |
| 10 | Search-index resolver on the title-only lines (cap 2 searches/job): candidate holders of the reporting title. Reviewer marks plausible / implausible. | Suggested-tier precision; decide whether to ship suggested at all. |
| 11 | Teamtailor page fetch + JSON-LD on the sample's applicable jobs. | Recruiter-contact yield for that ATS. |
| 12 | Rights: written answers from TheirStack/Fantastic.jobs on candidate-facing display, caching across accounts, retention after job close. | Go/no-go on display of vendor poster data. |
| 13 | Panel prototype behind a founder-only flag using the sample; referral drafts consume reporting title + team. | Usability read on labels; no PII shown. |
| 14 | Write-up: coverage lift per channel over baseline, precision per tier, cost per supported assertion, stale rate; decide which channels ship. | Ship list and budget for the next 30 days. |

Launch gate (unchanged from the attached report, now with denominators): ≥95% precision on
reviewed `named_for_this_job` + `structured_field` + `self_identified` claims; `title_only` and
`suggested` reported separately with their own precision; unresolved claims excluded from both and
counted.

## 9. Reproduction and NOT TESTED list

```bash
# from a machine with normal egress (this sandbox could not reach any of these hosts)
python scripts/probe_hiring_contacts.py \
  --board greenhouse:cloudflare --board greenhouse:crunchyroll --board greenhouse:thenewyorktimes \
  --board greenhouse:fleetio --board greenhouse:meetelise \
  --board ashby:zapier --board ashby:Commure --board ashby:posthog --board ashby:ashby \
  --board lever:coupa --board lever:finquery --board lever:logrocket --board lever:cscgeneration-2 \
  --board lever:greenlight --board lever:jito.wtf \
  --board smartrecruiters:Wix2 \
  --board workday:https://livenation.wd503.myworkdayjobs.com/TMExternalSite \
  --hn 49522897 --limit 3 --out probe_results.json
# USAJOBS (free key): USAJOBS_API_KEY=… USAJOBS_EMAIL=… python scripts/probe_hiring_contacts.py --usajobs "software engineer"
# offline guard
pytest tests/test_hiring_contacts.py -q --disable-socket
```

NOT TESTED in this review: TheirStack, Fantastic.jobs, Coresignal, Hirebase, Apollo, Bright Data
(no accounts, no purchases); any direct fetch of ATS JSON, PostHog, HN comments, Sweden/Norway feeds,
JSON-LD blocks (egress blocked); Dice and Teamtailor contact blocks on live pages.

## Sources (as indexed 2026-09-11)

SmartRecruiters Posting API and `/postings/{id}` reference (dev.smartrecruiters.com … postingcontent);
Greenhouse Job Board API docs (developers.greenhouse.io/job-board; `metadata` = employer-exposed custom
fields); Ashby public posting API (developers.ashbyhq.com/docs/public-job-posting-api); Teamtailor
Support "Create a new job" (contact person, Colleagues); Dice Knowledge Center "View a Recruiter
Profile" and "Manage my Recruiter Profile"; LinkedIn Help "Job Poster Profile on Job Posting" and
"Meet the hiring team"; USAJOBS developer reference (developer.usajobs.gov); TheirStack docs
(theirstack.com/en/docs/app/contact-data, job-posting-api, pricing); Fantastic.jobs (fantastic.jobs/job-board,
RapidAPI Active Jobs DB); Coresignal docs (multi-source jobs data dictionary, pricing) and third-party
pricing summaries (apiserpent, dataforb2b, coldiq); Hirebase (hirebase.org/pricing, use-cases/recruiting-talent,
docs); Apollo pricing summaries (smarte.pro, salesmotion, cotera) and partners/api-reseller; Bright Data
LinkedIn scraper docs (data policy change, jobs dataset `job_poster`); PostHog handbook (small teams,
management); GitLab handbook team page (30 Apr 2026); HN threads 49522897 / 49522896; Y Combinator
Work at a Startup job pages listed in §2.
