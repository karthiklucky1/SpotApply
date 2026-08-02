# How Hiring Actually Works — August 2026

Distilled from an external field guide (*"How Hiring Actually Works — and how to get
inside it"*, compiled 1 Aug 2026; US market, direct hiring + the IT staffing/vendor
ecosystem, emphasis on early-career and international candidates).

This file is the **product-facing** distillation: what the research says, what
SpotApply already does about it, and what changed in the code because of it.
Everything below carries the source's own sourcing discipline — where the guide
flagged a number as stale or contested, that flag is preserved.

Companion docs: `freshness-strategy-2026-07.md` (the freshness wedge, already
implemented), `job-market-us-india-2026-07.md`.

---

## 1. The claims that are load-bearing for SpotApply

### 1.1 Distribution lag — the arbitrage we already build on

The company careers page *is* the ATS, and it is the origin. Everything downstream
is a **pull**, not a push:

| Destination | Mechanism | Lag after the careers page |
|---|---|---|
| Careers page (the ATS) | recruiter clicks publish | 0 — this is the origin |
| LinkedIn | scrapes ATS partner XML every 6h | 0–24h |
| Indeed | XML feed sync; Greenhouse documents "up to 48 hours" | 12–48h |
| Glassdoor / secondary aggregators | downstream of the above | 24–72h |

Two consequences, both already central to our design:

1. **The careers page is a strict superset.** Jobs drop out of syndication for
   mundane reasons — a malformed location field, an emoji in the title, a title
   that trips a spam filter, staffing-agency exclusion rules. Ashby documents that
   roles requiring background checks are *not* posted to Indeed in NY or NJ at all.
   A job you can only find on the company's own board is **normal**.
2. **The lag is a free head start.** Polling ATS boards directly puts you 0–48h
   ahead of LinkedIn/Indeed, applying while the pile is in the dozens.

This is what `discovery/` + the pulse lane already do. See
`freshness-strategy-2026-07.md` for the volume data behind it.

### 1.2 The single unfalsifiable field: true first-publish time

Every major ATS exposes an unauthenticated public feed carrying a **real** publish
timestamp that aggregators overwrite:

| ATS | Endpoint | Real timestamp field |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `first_published`, `updated_at` |
| Lever | `api.lever.co/v0/postings/{site}?mode=json` | `createdAt` (epoch ms) |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | `publishedAt`, `isListed` |
| Workday | `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (POST) | — |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | — |
| Workable | `apply.workable.com/api/v1/widget/accounts/{account}` | — |
| Recruitee | `{company}.recruitee.com/api/offers/` | — |

**The refresh-vs-reopen distinction matters and nothing else exposes it.** LinkedIn
and Indeed reset the visible date on a repost; the ATS `first_published` does not.
A req whose ATS timestamp is eight months old but shows "2 days ago" on LinkedIn was
*refreshed*, not *reopened* — a materially worse thing to spend an application on.

Also: Ashby's `includeCompensation=true` frequently returns a band the rendered
page does not display. Greenhouse migrated `boards.greenhouse.io` →
`job-boards.greenhouse.io`; **both hosts still resolve**.

### 1.3 What actually auto-rejects — the myth correction

> **"75% of resumes are auto-rejected by the ATS" is false.** It traces to a defunct
> resume company around 2012 and has no verifiable source.

Greenhouse's own documentation states applications "are reviewed by real people, one
by one, in the order received", that "AI doesn't score or rank applications, nor does
it make any decisions", and that auto-reject filters are "set and controlled directly
by recruiters — not outsourced to a bot."

**What actually auto-rejects: knockout questions, and only knockout questions.** In
both Greenhouse and Ashby, auto-reject fires exclusively on the answer to a custom
application question of type yes/no, single-select or multi-select. It does **not**
trigger on resume keywords. The questions that carry it in practice:

- "Are you legally authorised to work in the US?"
- "Will you now or in the future require sponsorship?"
- Location, onsite willingness, clearance, required licence, minimum years of experience

**What happens to everyone else:** you are not rejected — you are *unsearchable*, and
then unread. The ATS is a database recruiters query with boolean strings; résumés are
parsed into structured fields by third-party parsers that pattern-match rather than
comprehend. Greenhouse notes its search expands to synonyms, so exact-string matching
is not required — but relevance still is.

Where genuine AI ranking exists (Workday HiredScore A/B/C/D, Oracle 0–5), the honest
synthesis is: **AI in 2026 rarely rejects you; it reorders you.** With 244
applications per requisition, being ranked in the bottom half means no human opens
your file. That implies a different strategy than keyword stuffing: be legible and
relevant to a ranking model, and use the three routes that skip the queue entirely —
**referral, recruiter contact, or applying within hours of posting.**

Two dated corrections: HireVue discontinued facial analysis in **March 2020**. And in
*Mobley v. Workday* (N.D. Cal.) the court authorised notice to a collective of
applicants aged 40+, covering roughly 1.1 billion applications rejected via Workday
in the covered window.

### 1.4 Channel, not volume, is the lever

| Channel | Share of applications | Share of hires | Efficiency |
|---|---|---|---|
| Inbound (boards + careers page) | ~90% | ~50–57% | baseline |
| Recruiter-sourced | 2.6–2.9% | 9.7–11% | ~3–4× yield |
| Referral | 1–2% | 11.6–19% | ~5–11× yield |
| Internal mobility | small | meaningful | ~32× inbound |

Sources: Greenhouse 2026 Benchmark (6,000+ orgs, 640M applications), Gem 2026 (165M
applications, 1.2M hires), Ashby Talent Trends 2026.

> The famous **"referrals are 30–40% of hires" figure is from 2012–2016 data and is no
> longer supported.** Cite the *conversion advantage*, not the share.

Application → offer is roughly **0.5%** (~1 hire per 200 applications). Cold portal
applying converts at 0.1–2%. A referral converts application→interview at ~40%.

Why applications go unread, arithmetically — not malice, not a robot:

| Metric | 2022 | 2025 | Change |
|---|---|---|---|
| Applications per job posting | 115 | 244 | +111% |
| Applications per hire | ~79 | 203 | +158% |
| Recruiters per organisation (median) | ~11 | 5 | −56% |
| Applications per recruiter per year | ~146 | 746 | +412% |

Stage rates worth holding a user's funnel against: application → first stage **8–22%**
advance; recruiter screen **~35%** advance (**52%** for referrals); post-onsite →
offer **~95%**; offer → accept **~81%**. Median time to fill ≈ **39 days** (SHRM) or
**~57 days** (Greenhouse) — the gap is definitional ("time to fill" starts at req
open, "time to hire" at first candidate contact). **Never average the two.**

### 1.5 Ghost jobs — six tests, and the honest base rate

Prevalence estimates vary by method: an academic analysis of Glassdoor data put it at
up to **21%** of ads; survey work found **3 in 10** companies had a fake posting live
and 40% had advertised one in the past year; roughly 20% of listings on one major ATS
produced no hiring activity in any quarter since 2022. Hires per posting halved
between 2019 and 2024 (8 per 10 listings → 4).

**But most postings that stay up are not conspiracies.** They are recruiter neglect
(listings require manual deletion, and there are half as many recruiters as four
years ago), policy requiring a public posting when an internal candidate was chosen,
a req put on hold without closing, or five staffing agencies posting the same single
client role. **Evergreen is not ghost** — a genuinely always-hiring role is real
opportunity.

The six tests that actually work:

1. **Read the ATS timestamp** (`first_published`/`createdAt`/`publishedAt`) — the only
   unfalsifiable date. Board "posted 2 days ago" means nothing.
2. **No ATS link at all** — exists only on LinkedIn/Indeed with Easy Apply and no
   matching entry on the company's own board → pipeline harvesting or an agency post.
3. **Staffing-firm fingerprints** — same job text under multiple agency names, no
   named client, unusually wide salary band, "seeking candidates for our client".
4. **Check the company's own careers page** — if it is not there, it is not a live req
   for that company.
5. **Age plus vagueness** — unchanged for six months, generic description, no process
   transparency. Legitimate evergreen says "applications accepted on a rolling basis",
   lives on the official careers site, and states a review cadence.
6. **Base rates** — ghost density is highest in large firms, specialised industries,
   and high-turnover sectors.

Regulation arriving: Ontario in force **1 Jan 2026** (25+ staff must tell interviewed
candidates the outcome within 45 days *and* disclose AI screening). A New York bill
passed the Senate **June 2026** requiring disclosure of whether a posting is a current
vacancy, fines from $2,500 per posting. NJ, CA, PA bills introduced.

### 1.6 Sponsorship — the caveat we must never blur

> **LCA filed ≠ H-1B petition filed ≠ H-1B approved ≠ selected in the lottery.**

Employers file LCAs speculatively, in bulk, for multiple worksites, and for extensions
and transfers. A company with 500 certified LCAs may have sponsored far fewer people.
LCA volume must always be cross-referenced against the USCIS H-1B Employer Data Hub's
**initial approvals**.

| Source | What it tells you | What it does not |
|---|---|---|
| USCIS H-1B Employer Data Hub | actual petition outcomes by employer + FY: initial approvals, initial denials, continuing approvals | salaries, titles, cap-subject vs exempt, whether the beneficiary was a new grad |
| DOL OFLC disclosure data | every LCA: employer, worksite, SOC code, title, offered vs prevailing wage. Also PERM — the better signal of **long-term** intent | an LCA is a prerequisite, not a petition, and certainly not an approval |
| h1bdata.info | free, no login, 4.8M LCA records Oct 2013 – Sep 2025 | anything USCIS-side |
| MyVisaJobs | sponsor rankings, PERM/green-card, prevailing wage, E-Verify listings | methodology unpublished — directional only, some paywalled |

**And regardless of history: the sponsorship knockout question on the application is
the binding constraint.** A company that sponsored 400 people last year can still have
a req that auto-rejects on that question this year.

**The channel almost nobody uses — cap-exempt employers** under INA § 214(g)(5):
institutions of higher education, nonprofits related to or affiliated with them
(university hospitals, research institutes), nonprofit research organisations, and
governmental research organisations. Petitions filed by or for these are **not counted
against the cap and can be filed year-round with no lottery**. Large volumes of
engineering and research work sit at university medical centres, national labs
operated through affiliated nonprofits, and university-affiliated research centres.
There is also a real strategy in **concurrent employment**: someone in cap-exempt H-1B
status can hold a concurrent cap-subject position with no lottery, so long as the
cap-exempt job continues.

H-1B calendar: registration announced late Jan–Feb; electronic registration ~first
three weeks of March; selection by 31 Mar; petition filing opens 1 Apr (90-day
window); employment start 1 Oct. FY2027 cycle: selection completed 31 Mar 2026, filing
opened 1 Apr 2026, cap reached **17 Jul 2026**.

**Volatile — re-verify before relying on it:** the $100,000 H-1B fee from a Sept 2025
proclamation was **vacated** by a federal district court in Massachusetts on
**8 Jun 2026** as unauthorised by existing law; the First Circuit denied the
government's motion to stay on **27 Jul 2026**. Unenforceable as of 1 Aug 2026, but an
appeal is expected.

### 1.7 The staffing-vendor chain — the biggest uncovered risk for our core user

Roughly 75% of the Fortune 1000 buy contingent labour this way. It is a legitimate
industry with a segment inside it that is predatory in ways that put the *candidate's
immigration status*, not the vendor's balance sheet, at risk.

The layer stack: **end client → MSP → VMS** (SAP Fieldglass, Beeline, Workday VNDLY,
Coupa) **→ prime/Tier-1 vendor** (holds the MSA, the only entity that can invoice) **→
sub-vendor/Tier-2** (no MSA, no VMS login) **→ "implementation partner"/Tier-3 → you.**
Four to six hops is common. Reqs are released in **tiered waves** — top-tier suppliers
see a req immediately, lower tiers are release-delayed.

**Why twenty recruiters call about one job:** the client's VMS distributes a single
requisition to multiple approved suppliers at once; each prime has many sub-vendors;
all search the same résumé databases with the same boolean strings. The tell is
near-identical JDs, same city, same rate band, same start date, from different
companies, **all refusing to name the client**. If two suppliers submit you to the same
client req, the VMS honours the **first timestamped submission** and rejects the
second — or rejects you outright so the client avoids an ownership dispute. You bear
that cost.

**Calibrate the blacklist threat:** an industry-wide candidate blacklist **does not
exist** — ATSs are multi-tenant with isolated per-company databases. What is real:
per-client flags in that one client's ATS/VMS, and individual agency do-not-work lists.

**Immigration rules that get candidates into trouble:**
- H-1B **requires W2 employment by the petitioning employer**. A C2C arrangement is a
  contract between your *sponsor* and a vendor — never between your own LLC and a
  vendor. "Solo C2C" on H-1B is not a tax strategy, it is unauthorised employment.
- STEM OPT requires an **E-Verify-enrolled employer**, and USCIS states plainly that
  "the staffing agency would not be permitted to hire the student and send him or her
  to work for a customer or client at the client's place of business." The entity that
  signs your Form I-983 must be the entity that actually trains you.

**The 2026 enforcement environment** (why this year is materially riskier):
DOL "Project Firewall" (19 Sep 2025); ICE/HSI OPT operation (12 May 2026, ~10,000 F-1
students with "highly suspect" employers); SEVP broadcast on STEM OPT employer fraud
(23 Mar 2026 — DHS investigating IT recruitment, consulting and staffing firms
specifically; site visits found unoccupied offices, residential addresses, offshore
payroll); Texas AG "ghost office" probe (Jan–Apr 2026, **open investigations, no
findings of liability**); DOL OIG H-1B/PERM fraud and human-trafficking investigation
(8 Jul 2026).

**The asymmetry, and it is the least understood fact in the guide:** in every
documented enforcement case the owners faced prison and forfeiture; **the workers faced
status loss and inadmissibility.** INA § 212(a)(6)(C)(i) makes fraud or wilful
misrepresentation a **permanent** bar with no time limit. The standard is wilful
misrepresentation *by the applicant* — a candidate who knows their résumé was inflated
can be found to have acted wilfully even though the vendor drafted it. **"My employer
told me to" is not a recognised defence.** Deferred Action for Labor Enforcement
(DALE), which shielded workers reporting employer abuse, is no longer available in
practice.

**The behavioural test that actually separates legitimate from predatory** — every
line tests the same thing: *is this firm willing to give up its information advantage
over you?*

| A legitimate firm | A predatory one |
|---|---|
| Never charges you anything, at any point | Has a fee, deposit, or deduction "from your first paychecks" |
| Names the end client before asking for an RTR | Will not name the client at all |
| Uses a per-requisition RTR in writing | Pushes a blanket, multi-client, or multi-year RTR |
| Discloses how many layers sit between it and the client | Deflects the question |
| States the pay rate in writing before the interview | Keeps the rate verbal and moving |
| Submits your résumé as you wrote it, and sends you a copy | Offers to "adjust" your experience |
| Pays W2 workers during bench time | Benches you unpaid (a federal violation for H-1B) |
| Runs background checks and requests documents **after** an offer | Wants your I-20, passport or SSN at first contact |
| Lets you decline a submission without retaliation | Threatens you with "blacklisting" |

There is **no legitimate reason** for a recruiter to need immigration documents at the
sourcing or submission stage. I-9 verification happens *after* you accept an offer, and
demanding specific documents based on citizenship status is itself unlawful under
8 U.S.C. § 1324b.

Money, because two numbers get swapped opportunistically: **markup is expressed on the
pay rate, gross margin on the bill rate.** A "30% markup" on $100 pay is a $130 bill
and a 23% margin. A "30% margin" on a $130 bill is a $91 pay rate. Median US IT
temporary-staffing gross margin is ~25.6%; 2026 markup guidance is 40–65% (50–75% for
specialised tech). In a four-layer chain a $110 bill rate lands at a **$57–64/hr
effective W2 rate (52–58% of bill)**; a two-layer chain lands nearer 65–70%.
**Rule of thumb: a C2C rate needs to be ~25–35% above a W2 rate just to be
equivalent**, before unpaid bench risk.

### 1.8 Referral mechanics — why the ask must be shaped a specific way

Inside the ATS, an employee clicks "Add a Referral", and two things happen that
dictate what you should actually ask for:

1. **They must select a specific live job.** You cannot be referred as a general
   prospect. → Always hand a referrer a **specific requisition link**, never "let me
   know if anything opens up".
2. **They must describe their relationship to you, in writing, on the record.** This is
   precisely why strangers decline — the form asks how they know you, and they cannot
   honestly answer "I don't".

Once submitted you enter the ATS with `source = Referral`, the referrer is credited,
and recruiters typically sort or filter by source — so you surface ahead of the inbound
pile. The bonus is usually 50% at hire and 50% after 90 days: ~$5,000 for tech roles,
~$2,500 all industries. Small money, deferred, against real reputational exposure —
so asks that acknowledge that land better.

**What to ask a stranger, in descending order of yes:**

| The ask | Why it works |
|---|---|
| "Would you be willing to forward this req to the recruiter with a note that I reached out?" | Costs them nothing, requires no relationship claim, still gets you out of the inbound pile |
| "Would you be open to a 15-minute call? If it seems like a fit afterwards, I'd love to ask about a referral." | Converts stranger → acquaintance first, which is the actual unlock |
| "Can you tell me who owns this req?" | Pure information, near-zero cost, high yes rate |
| "Will you refer me?" — cold | **Lowest yield.** Most likely to produce silence, because of the relationship field |

**Alumni is the highest-yield channel, and the reason is mechanical:** a shared
institution is a verifiable, non-fabricated relationship claim, so the referrer can
honestly fill in the ATS relationship field.

**Two myths to drop:** "80% of jobs are never advertised" (traces to a 1980 newspaper
interview citing a 1974 Boston-area study; never replicated at 80%) and "referrals are
40% of hires" (2012 data).

### 1.9 Outreach craft, with numbers

Best-instrumented dataset available: 4M+ recruiting messages, Jun 2025 – May 2026,
across 1,500+ organisations. Direction is recruiter-to-candidate, but message craft
transfers.

| Variable | Finding |
|---|---|
| Channel | LinkedIn message **17.1%** reply · human-written email **6.3%** · automated email **5.0%** |
| Subject line | **5–6 words** performs best |
| Email body | **150–199 words** best. 300+ was the most common length and underperformed |
| LinkedIn body | under **400 characters** is +22% vs average; over 1,200 characters is −11% |
| Follow-ups | **three touches capture 93.2%** of every reply a sequence will earn (5.5% → 5.4% → 4.2% → 3.3% → 2.9%) |
| Timing | Wednesday–Thursday peak; Saturday worst |
| Multi-channel | LinkedIn note **plus** a short email: **45.8%** vs 19.7% for email alone |
| Personalisation | real personalisation lifts replies **30%+**; using a first name does not (98% already do) |

Structure that works: **specific common ground → proof → a low-cost ask**, at 25–50%
response, versus 5–25% for one that leads with credentials. Target **managers** (ideally
under two years in role — they are building teams and have unfilled headcount), not
directors or VPs. Do not explain how you found them. Do not attach a résumé on first
contact. Include one link. Close with two concrete time windows.

**Batch size correlates inversely with reply rate: under 50 recipients 5.8%, over 1,000
recipients 2.1%. Volume is not the lever here.**

**Follow-up cadence:** applied via portal → wait 5–7 business days → touch 1
(referencing the specific req ID) → wait 7–10 days → touch 2 (must carry *new*
information) → **stop**. Re-engage only on a genuinely new trigger. Never contact the
same person twice in one day. Never fire the same message across email, LinkedIn and
phone simultaneously. **Never escalate to a more senior person because the first did not
reply** — "calling numerous employees in the same company" is on every recruiter's
named-disqualifying list. If the posting says no calls, do not call.

### 1.10 The research line — professional vs personal

The organising principle, one sentence: **information the person published, in a
professional capacity, for a professional audience, without a privacy control on it.**

| Fair game | Not fair game |
|---|---|
| Name, current title, employer, work location | Home address |
| Work email; the company's main and department lines | Personal cell number |
| Published professional work: talks, engineering blog posts, papers, patents, OSS, podcasts, press quotes | Personal email address |
| Public professional posts (LinkedIn, professional X/Bluesky, technical blog) | Family details — spouse, children, spouse's employer |
| The job posting, req ID, and team/department from public ATS feeds | Personal social accounts (Instagram, Facebook, Strava, Reddit) |
| Org structure inferred from public titles | Financial info, property records, voter registration, court records |
| Alumni or professional-association membership | Anything behind a privacy setting they chose; anything obtained by pretexting |

The consequences of the right-hand column are not reputational hand-waving. Greenhouse
has a "Do not email" candidate tag that blocks all mail to a candidate and
auto-applies when emails bounce. Zoho Recruit's "Block Candidate" permanently flags the
email address so the person cannot apply again, archives existing applications, cancels
scheduled interviews and withdraws pending offers — stated triggers include
"unprofessional conduct", and it persists until manually reversed. **Reapplying does not
reset it**: ATSs run duplicate checks on email, phone and last name and merge profiles.

The legal floor is lower than people assume: 18 U.S.C. § 2261A reaches a *course of
conduct* using email, messaging or phone with intent to harass — **no threat, no
violence and no in-person contact is required.** State civil harassment statutes
(California CCP § 527.6 is the model) explicitly name "harassing correspondence"; orders
can run five years, and in many states the *employer* can seek one on the employee's
behalf.

### 1.11 Pre-posting signals, ranked by evidence — and one to stop believing

| Signal | Strength | Why |
|---|---|---|
| Polling ATS boards directly | **Strongest** | Not a prediction at all — a measured 0–48h head start. First-party documented. |
| A departure or promotion on LinkedIn | Strong | Backfills inherit an existing position ID and budget, so they skip exception approval. Lead time ≈ notice period. |
| Internal-only posting window | Structural | The req exists and is invisible externally for ~5 business days by policy. |
| Earnings-call language | Strong (**negative**) | A stated hiring freeze is a direct statement that reqs will not be approved. |
| Contractor conversions | Moderate | Posted for compliance with a predetermined outcome — a real component of the ghost-job gap. |
| New office or site announcement | Directional | Generates location-specific req blocks 2–6 months ahead. |
| Employees posting "we're hiring" | Directional | Often precedes public posting. |
| **Funding rounds** | **Weak — mostly myth** | No statistically significant correlation between capital raised and headcount growth. Revelio Labs found median Series A headcount *fell* from 57 to 44 between 2020 and 2025 while funding per employee doubled. |

Recruiting-signal vendors sell "a Series B announcement precedes a hiring wave by three
to six weeks." **That claim has no published methodology behind it.**

### 1.12 The calendar

Tech recruits 9–14 months ahead, and the lead time is getting longer.

| Month | Internships (following summer) | New-grad full-time |
|---|---|---|
| Jun–Jul | Earliest wave: quant/HFT and a few big tech | Off-cycle trickle |
| Aug | Ramp begins | Fall reqs begin appearing |
| **Sep** | **Peak month. Highest volume of the year** | **Peak. Most Fortune-500 and big-tech reqs live** |
| Oct | Still heavy; many close mid-to-late month | Heavy; first offers land |
| Nov | Tapering | Tapering; offer deadlines cluster |
| Dec | Slow — holiday freeze, reqs sit unattended | Slow; budget resets |
| **Jan** | **Second wave. Under-filled reqs repost** | **Second wave — best window if you missed fall** |
| Feb | Spring wave; startups peak | Continued |
| Mar | Tail end | Steady. H-1B registration (~first 3 weeks) |
| Apr–May | Scattered backfills only | May-graduate urgency reqs. H-1B filing opens Apr 1 |

Sub-cycles: **quant/HFT recruit 14–18 months ahead** with sophomore-year pipelines.
**Defense/aerospace post continuously year-round** rather than in waves — though much
is clearance-gated and closed to non-citizens.

**Why the internship cycle matters more than the new-grad cycle:** intern-to-full-time
conversion is **63.1%** (NACE 2026). External new-grad requisition counts are a
*residual* — what is left after return offers are counted — which is why they cluster
Sept–Nov. Missing the fall internship window costs more than missing a new-grad window.

---

## 2. What this changed in SpotApply

### Shipped

**A. We no longer auto-answer the sponsorship knockout question** (§1.3, §1.7) —
*the most serious finding.* `qa_store/answers.yaml` shipped
`requires_sponsorship: false` with the comment *"Defaulting to false (answering 'No'
to sponsorship questions to avoid auto-filtering)"* — two lines above
`visa_type: "OPT"` and `sponsorship_timeline: "…requires future H-1B sponsorship"`.
`QAResolver` served that "No" at **0.95 confidence** into autofill, and
`tests/test_qa_resolver.py` asserted it as correct behaviour.

That is a false answer to the one question that genuinely auto-rejects — and under
INA § 212(a)(6)(C)(i) a wilful misrepresentation to procure an immigration benefit is
a **permanent** bar, with the standard applied to the *applicant*, not the tool. The
resolver now returns `(None, 0.0)` for every phrasing of it, routing to the existing
human-approval path — matching what `autofill/answer_pack.py` already did correctly
(`"auto": False`, *"We never auto-answer it"*). The two subsystems had disagreed.
`requires_sponsorship` is now truthful (`true`) and documented as display-only.

The guard matches any question mentioning sponsorship and runs **before** the
work-authorisation branch, because the most common real phrasing is compound —
*"Are you authorized to work in the US **without sponsorship**?"* That matches the
work-auth keywords, and answering it from `authorized_to_work_us` returns "Yes",
which is false for an OPT holder: authorised now, but not without future
sponsorship. Ordering the guard after the auth branch (the first attempt at this
fix) left all three compound phrasings still answering "Yes". Over-routing costs the
user a click; under-routing can cost them the right to be in the country.
`resolver.py`, `answers.yaml`, `tests/test_qa_resolver.py`.

**B. The STEM OPT pitch no longer claims "zero cost or filing"** (§1.7).
`work_auth.py` handed the user a line to paste to employers: *"zero cost or filing
required from the employer right now"* / *"no paperwork, cost, or sponsorship
required from you now."* STEM OPT requires an **E-Verify-enrolled employer** and a
signed **Form I-983** training plan — both are employer obligations. The framing now
leads with what is genuinely true and strong (no petition, no filing fee, no lottery)
and states the two obligations plainly. `intelligence/work_auth.py`.

**C. Greenhouse freshness read the falsifiable date** (§1.2). `greenhouse.py` took
posting age from `updated_at`, which moves every time a recruiter touches or re-posts
a req — so an eight-month-old listing refreshed yesterday read as brand new, in a
product whose core promise is "freshest first". Now prefers `first_published`, the
only unfalsifiable date, falling back to `updated_at`. `discovery/greenhouse.py`.

**D. Ashby postings had no publish date at all** (§1.2). `discovery/ashby.py` read
`publishedDate` while our own `intelligence/job_check.py` read `publishedAt` — two
places in this repo disagreeing about one API. Now tries `publishedAt` then
`publishedDate`. `discovery/ashby.py`.

**E. The sponsorship card stopped claiming a source it had not read** (§1.6). The
cap-exempt verdict is produced by a name/URL heuristic *before* any data lookup runs,
yet the card said *"Their public filing record backs it up."* The claim is now gated
on whether filing data actually exists, and cap-exempt is described as what it is —
an inference from employer type under INA § 214(g)(5). `templates/dashboard.html`.

**F. New: the H-1B cap calendar** (§1.6). `intelligence/h1b_calendar.py` — pure, no
DB/LLM/network — reports which phase of the cap year today falls in and, critically,
that from April onward a cap-subject offer cannot start until **Oct 1 of the following
year**, pointing at the cap-exempt route whenever the lottery cannot help. Wired into
the existing OPT clock (`_opt_clock_payload`) and rendered on the OPT card. Dates are
flagged approximate and point at uscis.gov, per §4. 16 tests in
`tests/test_h1b_calendar.py`.

**G. Corrected our own ATS-keyword myth** (§1.3). `tailoring/ats_keywords.py` asserted
as fact that *"Real ATS systems (Greenhouse, Lever, Workday, Taleo) score a resume by
how many of the job description's exact terms appear verbatim"* — which Greenhouse's
own documentation explicitly denies. Reframed around the real mechanic: keyword
coverage buys **findability in recruiter boolean search**, not immunity from a
rejecter that does not exist. The analysis is unchanged and still useful; only the
stated rationale was wrong. `tailoring/ats_keywords.py`.

**H. New: staffing-vendor posting detection** (§1.7) — *the gap flagged as most
likely to matter, now closed.* A grep for `staffing|C2C|RTR|bill rate|markup|MSP|VMS|
hotlist` across `app/` and `extension/` returned **nothing** — Part 2 had zero
coverage, in a product whose core users are exactly the people it endangers.

`intelligence/vendor_posting.py` (pure: no DB, LLM or network) classifies a posting
as vendor-sourced from deterministic fingerprints — an unnamed end client (the
strongest tell), contract-engagement vocabulary (C2C, corp-to-corp, W2, RTR, prime/
sub-vendor), hourly rather than salaried pay, visa-status filtering in the posting
body, and agency-shaped company names. It requires either the unnamed-client signal
or two independent corroborating signals, because staffing is a **legitimate**
industry (~75% of the Fortune 1000) and a badge that cries wolf is a badge users
learn to ignore.

Three deliberate design choices:

- **The badge is descriptive, never an accusation.** Misconduct lives on a separate
  `red_flags` axis — fee requests, immigration documents demanded before an offer,
  blanket RTR, chat-only interviews, offers to "adjust" your experience. A normal
  vendor post gets the chain explanation and the seven-item checklist; only genuine
  red-flag language gets the warning treatment.
- **The STEM OPT caution is gated on the user's own profile.** Only F-1/OPT users see
  it, because only for them does a client-site placement collide with the I-983
  training requirement and the E-Verify rule — the asymmetry being that when a vendor
  lies, the owners face prosecution and *the worker* faces a permanent bar.
- **"No sponsorship available" is not a vendor signal.** That is a lawful statement by
  a direct employer, owned by `sponsorship.py`, and is explicitly tested against.

Wired into `/application/{id}/sponsorship` and rendered in the job drawer. 26 tests
across `tests/test_vendor_posting.py` (both directions — five true-positive and five
true-negative fixtures) and `tests/test_vendor_posting_endpoint.py` (end-to-end,
including the profile gating).

**I. The agency-duplicate tell** (§1.5, ghost test 3). `ghost_detector.py` counted
repeats of `(company, title)` — which by construction cannot see one client req
fanned out across a prime vendor and its sub-vendors, because the whole point is that
the *company name differs*. Added a check on `content_hash` (already
`sha256(description)` and already indexed, so this is a keyed lookup, not a scan),
tenant-scoped like the existing query, scoring modestly: the req is real but
multiplied, not fake. `matching/filters/ghost_detector.py`, +3 tests.

### Audited and found already covered

The freshness thesis in `freshness-strategy-2026-07.md` is genuinely implemented: we
poll the documented public ATS endpoints directly, the pulse lane schedules per-board
polling to realise the 0–48h head start, the ghost gate runs before any LLM spend, and
we never automate LinkedIn/Indeed. The free job-check already re-queries a posting's
own ATS API. `Job.is_cap_exempt` already exists.

### Known gaps, not yet built

Ranked by value/effort from the audit, and **not** verified as thoroughly as the items
above (the verification pass was cut short — see §5):

1. **"Refreshed, not reopened" badge** (§1.2) — we now read the right timestamp but do
   not yet tell the user when the two dates disagree.
2. **Evergreen-is-not-ghost exception** (§1.5) — genuine rolling-basis early-career
   pipelines may be over-penalised by the age signal.
3. **Pre-apply knockout prediction** (§1.3) — Greenhouse's public API exposes the real
   application questions via `?questions=true`; we fetch content but never questions,
   so we cannot warn "this posting's sponsorship question will eliminate you" before
   the user spends a tailoring credit.
4. **Initial vs continuing H-1B approvals** (§1.6) — `h1b_data.py` sums them, so "400
   approvals" may be 397 renewals for existing staff. The Data Hub ships them as
   separate columns.
5. **Cap-exempt detection breadth** (§1.6) — a ~16-substring name match catches
   "Stanford University" but misses Mass General Brigham, Fred Hutchinson, Battelle,
   HHMI, SRI International and JPL, which is where the volume actually is.
6. **DOL LCA / PERM ingestion** (§1.6) — needed to answer "do they sponsor at *entry*
   level" and "will they keep me past year six". Free bulk files, but real work.

Still entirely unaudited: **referral and outreach craft** (§1.8, §1.9), **funnel
benchmarks and the recruiting calendar** (§1.4, §1.12), and **application routing**
(ATS page vs aggregator, §1.1). The §1.10 research line — professional vs personal —
is worth a dedicated pass, since it is a guardrail question and we generate outreach.

---

## 3. Deliberately not built

- **Vendor-legitimacy lookups against E-Verify / SAM.gov / PACER / state SoS
  registries** (§1.7). Each is a separate scraper against a government site with its own
  robots and rate posture, and none has a bulk API we can lawfully poll at our volume.
  The checklist is surfaced to the user as guidance instead of being automated.
- **Anything that finds a person's contact details** (§1.10). The research is explicit
  that the yield on personal-detail research is negative and the failure mode is
  candidacy termination plus, at the tail, a restraining order. SpotApply surfaces
  public professional identity only and refuses the rest by construction.
- **Funding-round hiring signals** (§1.11). Explicitly refuted by the source; adding
  them would be adding noise with a confident face.

---

## 4. What to re-verify before relying on it

Three things in this document are volatile:

1. **The $100,000 H-1B fee litigation** — blocked as of 1 Aug 2026, appeal expected.
2. **Ghost-job disclosure bills** in New York, New Jersey, California and Pennsylvania.
3. **AI-hiring regulation** — Colorado's 2024 law became operative 30 Jun 2026 and is
   itself replaced by SB 26-189 on 1 Jan 2027. Anything citing 1 Feb 2026 is stale;
   that date was postponed. Illinois HB 3773 took effect 1 Jan 2026. California's FEHA
   automated-decision-system regs took effect 1 Oct 2025. The EEOC deleted its AI hiring
   guidance in Jan 2025 — the guidance is gone, the law is not.

Everything else (how a requisition gets approved, the ATS distribution order, the
referral mechanics) is structural and stable.

---

## 5. How complete this audit is

Eight themes were scoped against the codebase; **four completed** by the automated
audit: ATS freshness/timestamps, ghost-job detection, knockout/AI-screening, and
sponsorship intelligence. The adversarial verification pass and synthesis step never
ran — the audit hit its usage limit twice, on the first run and again on resume.

**The staffing-vendor theme (Part 2) was then audited by hand** rather than retried a
third time, and it produced items H and I above. That was the right call on the
evidence: it was predicted to be the likeliest place for another serious finding, and
it turned out to have *zero* prior coverage.

**Three themes remain unaudited**: referral and outreach craft (Part 4), funnel
benchmarks and the recruiting calendar (§1.4, §1.9, §1.12), and application routing
(ATS page vs aggregator).

Every change listed in §2 was therefore re-verified by hand against the code before it
was made, and each one is a factual correction with a citation above it. The §2 "known
gaps" list carries the audit's confidence, not a verified one. **The four unaudited
themes are the obvious next pass** — Part 2 (the staffing-vendor chain) is the one most
likely to contain another finding of the same severity as item A, because it is the
area where the product currently has no coverage at all and where the research
documents the harshest consequences for exactly our core user.

Two API field names in §1.2 could not be checked against live endpoints (the audit
environment's network policy blocks ATS hosts), so changes C and D read the documented
field with a fallback to the previous one rather than swapping one guess for another.
