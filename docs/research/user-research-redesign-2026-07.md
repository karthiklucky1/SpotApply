# SpotApply — User-Research Redesign Strategy (July 2026)

The answer to: *"our app is not unique — find the few best features that genuinely help
job seekers and recruiters, and change the app fully, based on user research."*

Method: two multi-agent research runs (25 agents, ~1.8M tokens): 6 parallel research
tracks (seeker pains, segments, recruiter side, retention/monetization, white space,
codebase audit) → 3 ideation lenses → 4-judge scoring panel (user impact,
differentiation, feasibility, skeptic) → 5 pillar v1 specs grounded in the actual code →
out-of-the-box round + skeptic judge → IA + positioning redesign. Sources are 2025–26,
cited inline. Builds on `competitive-analysis-2026-07.md` (July 10) — read that first
for the competitor map. Full v1 specs, IA detail, and the out-of-box catalog:
`redesign-appendix-2026-07.md`.

---

## 1. The diagnosis: not "not unique" — unique but invisible

The audit found the opposite of the founder's fear. SpotApply already has more genuinely
unique capability than any competitor: measured freshness (pulse lane, minutes-level),
the ghost stack, the honesty stack (grounding + lock + Doctor + metric-gap Q&A),
multi-country visa intelligence, Rejection Autopsy, email outcome sync — and an entire
**recruiter marketplace built and hidden** (`/recruiter`, `/messages`, `/u/{handle}`
trust profiles, intro tables — unlinked from any nav). The free ghost/fit check
(`/api/public/job-check`) — the natural acquisition wedge — has **no web surface** at all.

The real problems, per the research:
1. **The unique things are invisible or unscored.** Ghost filtering happens silently;
   freshness proof lives on an ops tab; the marketplace is dark; job cards lead with
   three unexplained numbers (the exact pattern users say they distrust in rivals).
2. **The product ends at Submit.** The #1 researched pain (the response void) begins
   *after* submit, where SpotApply currently goes as silent as the employers.
3. **The app is organized as an infinite job list**, while the data says targeted
   beats volume 3.5x — the UI shape contradicts the product's own thesis.
4. Live trust bugs: pricing shown in three contradictory versions, a landing meta tag
   saying "auto-apply" against our own doctrine (§9).

**The winning posture (positioning claim):** every rival competes on volume — *apply to
more jobs, faster*. SpotApply competes on **truth** — *know which applications deserve a
human's time, and prove you're the human*. This posture is structurally uncopyable:
auto-submitters can't attest human review; subscription-trap businesses can't ship
Hired Mode; ATSs can't publish cross-ATS response truth about their own customers.

---

## 2. What users actually say (the evidence)

### Seekers — pains ranked by frequency × intensity
| # | Pain | Key evidence |
|---|------|-------------|
| 1 | **Ghosting / feedback vacuum** | 61–63% ghosted after an interview (Greenhouse '24/25; Gen Z 78%); 94% want feedback, ~5.5% of rejected get any; resentment at a 13-yr high (+40% since 2016, Talent Board); honest feedback cuts resentment 29%; 66% cite no-feedback as burnout driver |
| 2 | **Ghost jobs** | 18–22% of listings (WSJ Apr '25); 93% of 918 HR pros admit posting them, 45% "regularly" (LiveCareer Mar '25); 36% of seekers applied to a never-filled role |
| 3 | **Volume arms race** | LinkedIn 11,000 applications/min (+45% YoY); recruiter inboxes 400+; Greenhouse CEO: "mad arms race… AI doom loop"; 41% of seekers admit resume prompt-injection |
| 4 | **AI-tool disappointment** | Jobright hallucinated skills + dead "high-match" jobs; Simplify Trustpilot 3.0 (67% one-star), autofill fills DOB with today's date; LazyApply answered work-authorization wrong. Failure axes: output quality + billing trust |
| 5 | **Emotional toll** | 72% say search harmed mental health; median search 57d (Q1'25) → 108d (Q1'26, Huntr); CS new-grad unemployment 6.1% (NY Fed) |
| 6 | **Opaque AI screening** | 38% withdrew over an AI interview; 70% never told AI would judge them; 8% of seekers call AI screening fair vs 70% of hiring managers trusting it; Mobley v Workday collective certified May '25 |

**What actually works:** referrals hire at ~28.5% vs 2.7% (Zippia/Pinpoint); 54% of
hires via connections vs 13% job boards; ~70% of interviews go to first-7-day
applicants (Lever); **11–20 targeted apps → 9.25% interview rate per app vs 2.58% at
100+ (Huntr, 600k apps) — mass-apply measurably backfires.** Every one of these favors
SpotApply's existing architecture over the auto-submit category.

### Segments — who to win first
1. **Visa-needing candidates (winner).** 1.18M intl students, 294K on OPT (+21% YoY);
   two 2025–26 policy shocks make this urgent: the $100K H-1B fee **exempts F-1/OPT
   change-of-status** (employers now prefer candidates already in the US), and the DHS
   **weighted lottery (eff. 2/27/26)** gives Level-1-wage jobs **15.29%** selection odds
   vs **61.16%** at Level 4 — *the job's salary level now determines visa odds and no
   tool scores jobs by it.* Segment provably pays flat fees ($199 Scale.jobs; FlashFire
   $1,200+). Jobright's cruder company-level H1B filter alone got 50K users in 2 months
   with zero marketing.
2. **Laid-off mid/senior engineers (runner-up).** 124–246K laid off in 2025; ageism
   mentions +133% YoY; 1 in 10 spent $500+ on their last search. They need triage
   (real + winnable), senior-grade grounded tailoring, referrals — maps to
   senior_reviewer + hire_probability.
3. New grads = freemium top-of-funnel only (huge pain, low willingness to pay).
   Career changers: avoid (need proof-of-skill, not apply-efficiency). Non-tech: phase 2.

### Recruiters — the other side of the flood
Avg 242 applications/role (2x in 5 yrs); 77% of hiring teams regularly see AI-assisted
apps; **67% of US HR leaders say AI-generated applications have SLOWED hiring** (Robert
Half Mar '26); only ~33% highly confident resumes reflect ability; Gartner: 25% of
applicants will be fake by 2028. Coping = retreat to trust: referrals convert 11x
inbound (Gem '26); 72% adding in-person rounds; **verification is the hot wedge**
(Greenhouse Real Talent + CLEAR, LinkedIn ID verification).
Marketplace graveyard lessons: **Hired** died lowering its candidate bar to chase supply;
**Triplebyte** died on candidate CAC + "credential = useless friction" (companies
re-interviewed anyway). Recruiters distrust opaque third-party scores — they read the
resume anyway. Survivors (Paraform $40M Series B) monetize *existing hiring spend on
outcomes*. → SpotApply's edge: candidates already come for their own reasons (CAC
solved); sell **triage, never interview-skipping**; show **evidence dossiers, never
bare scores**.

### Retention & money
Job search is an outcome product (~24-week average) — treat graduation churn as good
and compensate: dormant career mode, graduate-powered data loops (Levels.fyi give-to-get
precedent), B2B2C (Handshake ~$8k/university; outplacement $5.65B market). **Billing
rage is the #1 one-star axis across every rival** (Jobscan auto-renew after cancel,
Teal cancellation loops, Simplify+ 67% one-star). Accepted models: transparent ~$29/mo,
one-time lifetime (Rezi $149 is a beloved outlier), outcome-tied fees. Freshness is the
honest daily hook; paywall the high-stakes moments (tailoring, interview, negotiation),
not the organizer.

### White space (nobody owns these)
1. **Cross-ATS outcome truth** — per-company response rates from real user outcomes.
   LinkedIn/Indeed ship walled versions ("actively reviewing" badges), proving demand;
   no copilot publishes it. Network-effect defensible. ← biggest prize
2. **Passive interview intelligence** — stages/timelines/AI-screen presence harvested
   from scheduling emails (Glassdoor is decaying; its own leadership ratings at 2017 lows).
3. **Inbox parsing** as feedstock for 1–2 (tiny 2025–26 entrants exist; none integrated).
4. **Prep grounded in the exact submitted resume** — integration-defensible, only we
   hold the submitted artifact.
Taken/crowded (ride, don't build): proof-of-human ID rails (Greenhouse+CLEAR, LinkedIn),
standalone ghost index (GhostJobs.io), AI mock interviews (crowded, distrusted), salary
negotiation (commoditizing).

---

## 3. The judged feature bets

4-judge panel: user impact × evidence / differentiation × defensibility / feasibility
on current stack / skeptic (graveyard patterns). Scores 0–10, sorted by average.

| Bet | Impact | Diff | Feas | Skeptic | Avg |
|---|---|---|---|---|---|
| **Company Truth Index** — per-company response rate, reply time, ghost rate from real outcomes | 10 | 9 | 7 | 5 | **7.8** |
| **Visa Odds Engine** — wage-level lottery odds per job + OPT clock + Visa Pass | 10 | 6 | 6 | 7 | **7.3** |
| **Closure Engine** — predicted response window; declared closure when it expires | 8 | 5 | 9 | 6 | **7.0** |
| **Job Truth Check** — the built free ghost/fit check, finally surfaced as the wedge | 8 | 5 | 9 | 6 | **7.0** |
| **Calibrated Scoring** — outcomes recalibrate scores into honest P(response); weekly aim budget | 7 | 9 | 6 | 6 | **7.0** |
| **Hired Mode Billing** — detect the hire, proactively stop charging | 8 | 4 | 8 | 7 | **6.8** |
| **Verified-Live Postings** — "verified live 12 min ago" from pulse-lane polling | 7 | 6 | 8 | 5 | **6.5** |
| **Process X-Ray** — rounds/timelines/AI-interview presence from synced pipelines | 7 | 8 | 6 | 4 | **6.3** |
| **Resume Defense Prep** — prep pack against the exact tailored resume submitted | 5 | 6 | 8 | 6 | **6.3** |
| **Application Receipt** — verifiable human-review + claim→evidence provenance | 6 | 7 | 7 | 4 | **6.0** |
| **Recruiter Marketplace Launch** — un-hide `/recruiter`, boutique, per-intro pricing | 6 | 7 | 7 | 3 | **5.8** |

The 11 bets consolidate into **five pillars** (below) — each bet lands inside one.

---

## 4. The five pillars (v1 specs condensed — full specs in the appendix)

### Pillar 1 — The Truth Layer *(Company Truth Index + Verified-Live + SpotCheck)*
The data spine: nightly `CompanyTruthStat` aggregation over data we already collect —
pulse-lane liveness (posting lifetimes, removal/repost patterns; **zero users needed**),
the ghost corpus, and cross-user outcomes from email sync (new
`Application.first_response_at`). Stats are tiered and honest: Tier A posting-behavior
(day-1, infrastructure-derived) / Tier C response bands only as Wilson 95% intervals
with n visible (n≥5 private, n≥20 public). Three surfaces: (1) **Truth Strip** on every
job card — *"Verified live 12 min ago · typically up 31d · replies to 10–24% of tracked
apps (n=41, median 11d)"*; (2) **/check ("SpotCheck")** — public landing page for the
already-built no-account job check, with shareable `/check/{slug}` result pages = the
acquisition wedge; (3) public **`/companies/{key}`** SEO pages (n-thresholded,
methodology-linked, email-domain right-of-reply for recruiters). Legal posture:
measured facts + counts + methodology + right of reply. **4–6 wks, 1 engineer.**

### Pillar 2 — Visa Odds Engine *(the segment wedge)*
Salary extraction cascade (Ashby `includeCompensation` is already requested and
currently **dropped**; JD regex; corporate_insights cache) → new `PrevailingWage` table
from public DOL OFLC data (state × ~30 tech SOC codes) → per-job **wage level → FY2027
weighted-lottery odds band** on every card for sponsorship-needing users, with
methodology link, never a guarantee. Plus: **No-Lottery tab** (`is_cap_exempt` already
persisted), **OPT Clock** widget (user's EAD dates; clock zones re-weight the existing
sponsorship boost), and **Visa Pass $199 one-time** (Stripe `mode=payment`, no
auto-renew — the anti-Jobright billing story sold to the exact community burned by it).
Cold start: fully single-player, all public reference data. **4–6 wks.**

### Pillar 3 — The Outcome Loop *(Closure + Calibrated Scoring + Process X-Ray)*
At submit: stamp a **Reply Forecast** (3-tier fallback: company stats n≥5 → ATS-platform
aggregate → research-seeded sector priors, source always labeled). Waiting cards show a
calm countdown ("Day 9 of ~16"). A daily sweep declares **Closure** when the p90 window
expires — honest copy ("this silence is them, not you — 62% of apps here get no reply"),
auto-run **Rejection Autopsy** (module exists), 3 fresh redirect roles + a referral
draft; late replies auto-reopen. Nightly: `CompanyOutcomeStats` + `ScoreCalibration`
(score-decile → real response rates) → the **Weekly Aim Budget** (default 15 targeted
apps/wk, calibrated-EV ranked — finally implements the daily_engine stub) + a
"your funnel vs market" honesty panel. **Process X-Ray v1:** AI-interview-vendor and
stage detection in the existing email sync → per-user pre-interview AI-screen warning
(day 1, no corpus needed), company Process Cards at n≥3. Every closure feeds the Truth
Index — *the user's grief literally becomes the moat.* **4–6 wks.**

### Pillar 4 — Trust Economics *(billing as product)*
One canonical plan table (kills the live three-way price mismatch, §9): **Free $0 ·
Pro $29/mo · Season Pass $79 one-time/90d · Career Pass $149 one-time lifetime · Visa
Pass $199** — Passes structurally *cannot* auto-renew (no subscription object exists).
Real Stripe (checkout, portal, idempotent webhooks → existing `UserSubscription.stripe_*`
fields). The **Billing Bill of Rights** (`/billing-promise`): 1-click cancel with no
retention flow, 3-month "unemployed pause", pre-renewal email 7 days out, idle-billing
guard (21 idle days → "pause instead?"), visible billing audit log. **Hired Mode:**
offer-keyword tier in email sync → confirm-first prompt → billing auto-stops, free
dormant mode, give-to-get outcome contribution (`HiredOutcome`) + referral ask.
In a category whose one-star corpus is billing rage, the pricing page becomes a
positioning weapon. **3–5 wks.**

### Pillar 5 — Recruiter Bridge *(receipts first, marketplace boutique)*
**Application Receipt** (opt-in, default off): persist what the pipeline already
computes-and-discards (grounding confidence map, Doctor results, lock restores, review
events, the real submit click already detected by the extension) → public
`/r/{token}` page: human-review attestation timeline + every tailored bullet mapped to
its evidence (master-resume line / user-confirmed metric / GitHub / locked education).
One neutral cover-letter footer line. Framed as an **inspectable dossier, never a
score** — designed for recruiters who re-read resumes anyway. Rides existing ATS/email
flows: valuable to user #1 with zero recruiters; every receipt view is measured
recruiter lead-gen ("your packet was viewed" notification doubles as the seeker's
dopamine hit in the ghosting vacuum). Then **un-hide the built marketplace** as a
boutique: two curated pools only (visa-verified-in-US + senior-review-vetted), opt-in
per intro, design partners free then per-accepted-intro pricing — never seats, never
interview-skipping (the Hired/Triplebyte graveyard rule, stated verbatim on the pitch
page). **5–6 wks.**

---

## 5. Out-of-the-box moves (19 generated, skeptic-scored — top of the list)

| Score | Move | One-line |
|---|---|---|
| 8/10 | **Ghost Check Everywhere** | Unbundle SpotCheck: landing URL-checker + tiny standalone extension = Simplify-style free wedge, feeds the registry |
| 7/10 | **Visa Sprint** | Go full vertical: THE flat-fee software-led service for visa-clock candidates (not a horizontal SaaS with a visa filter) |
| 6/10 | **The 45-Day Standard** | Adopt Ontario's answer-within-45-days law as a global product feature + compliance index — regulatory tailwind marketing |
| 6/10 | **Candidate's system of record** | Every ATS serves employers; become the candidate's agent-of-record (tracking + outcome sync as the spine, apply as a module) |
| 6/10 | **Black Hole Collective** | Give-to-get verified outcome data → monthly "State of the Application Black Hole" data drop (Levels.fyi mechanics) |
| 6/10 | **Headless SpotApply (MCP)** | Expose truth/freshness/matching as an MCP server for users' own AI assistants (Tsenta ships CLI/MCP; ours would be truth-grounded) |
| 6/10 | **Post-Submit Autopilot** | Per-user apply alias (u_x@in.spotapply.ai) autofilled into every application → SpotApply becomes the outcome-capture rail |
| 6/10 | **Scorer-on-Device** | Ship the distilled scorer as ONNX inside the MV3 extension: instant private fit scores on any page, zero API cost |
| 6/10 | **Pocket Review Loop** | Multi-tenant Telegram/WhatsApp staging — approve tailored docs from your phone (bot exists, single-user today) |

Lower-scored (4/10 and below, kept for the record in the appendix): certification-
authority licensing, B2B2C-first pivot, signed-receipt protocol standard, marketplace-
as-MCP. Skeptic's consistent warning: don't pivot the company to a data/standards play
before the copilot earns density — **sequence, don't switch.** The apply-alias inbox
(6/10) is the boldest bet worth prototyping early: it makes outcome capture structural
instead of best-effort.

---

## 6. The app redesign blueprint

Principle (from the research): the home surface must answer **"what happened, what
should I do next, and what is true"** — not present an infinite feed. Full detail incl.
per-line template references in the appendix.

**Seeker IA — 5 destinations, one nav** (the duplicate top pill-bar dies):
1. **Today** (new default home): "What moved" event feed (replies, receipt views,
   forecast countdowns, closures, fresh alerts) · Weekly Aim Budget panel · funnel-vs-
   market honesty panel · OPT clock pinned for visa users.
2. **Jobs**: Shortlist (default) / Browse / No-Lottery / Watchlist as filter chips;
   ghost filtering becomes a "filtered for you" proof-of-work link, not a tab.
3. **Applications** (renamed Pipeline): the lifecycle spine — In Review → Submit moment
   (receipt mint + forecast stamp) → Waiting (countdown) → Closure (autopsy + redirect)
   ↔ auto-Reopen → Interviewing (Process X-Ray strip + Defense Prep) → Hired (billing
   stops on-screen).
4. **Companies**: watchlist + truth pages.
5. **Career Profile**: resume, trust profile, skill gap, memory, open-to-intros toggle.

**Job card, redesigned:** qualitative fit pill (kill the numeric dial — "three
unexplained numbers per card is the exact rival pattern users distrust"), the Truth
Strip, salary chip, visa-odds chip (sponsorship users only), posted-age. Everything
else → drawer.

**Kill list highlights** (20 items in appendix): dual navigation; "AI Agent Online"
theater (replace with the *measured* last-sweep line); Ghost Jobs and Boards as seeker
tabs; the numeric score ring; modal-as-navigation for Insights/Roles/Memory; the $99
Team plan card until seats exist; the pricing-FAQ copy describing the legacy Telegram
auto-submit flow; the landing "auto-apply" meta description.

**Mobile:** PWA over the existing Jinja (Today + Applications only), web push for fresh
alerts + closure digests; apply flow stays desktop and the UI says so honestly.
Telegram remains the power-user channel; no WhatsApp build now.

**Migration: 8 flag-gated steps, no big-bang** — (1) truth-and-deletion template pass
(week 1, zero schema) → (2) card truthification → (3) Applications lifecycle →
(4) Today home (only default-flip, both views coexist) → (5) visa surfaces →
(6) Stripe/Trust Economics → (7) public Truth Layer → (8) Recruiter Bridge +
Career-Profile consolidation + PWA. Total ~14 weeks single-engineer pace; pillars 5/6
parallelize.

---

## 7. Positioning, naming, launches

**Category claim:** *"The honest job-search copilot. Others compete on volume; we
compete on truth — know which applications deserve a human's time, and prove you're
the human."*

**Naming decisions:** public **Truth Index** / **Company Truth Pages** / **SpotCheck**
(renamed from job-check — brand-native, verb-able) / **Application Receipt** (always
"human-reviewed receipt" — Tsenta says receipts too; an auto-submitter structurally
cannot mint ours) / **Reply Forecast** + **Closure** (lane label "Closed — no response",
never "ghosted" tombstones) / **Visa Pass · Season Pass · Career Pass** ("Pass"
telegraphs ends-by-itself) / **Hired Mode** / **Aim Budget** ("Aim beats volume").

**Landing rewrite rule:** two visually distinct stat classes, never blended —
[MEASURED — live from our telemetry] vs [INDUSTRY RESEARCH — cited]. Hero: *"Stop
applying into the void."* + SpotCheck as the interactive hero widget. Delete the
pseudo-testimonials and vanity bands; the live ticker IS the social proof.

**Three launch moments:**
1. **SpotCheck goes public** — "Is this job even real?" + a data post computed entirely
   from Tier-A infrastructure telemetry (posting lifetimes, delete-and-repost patterns,
   ghost-flag rates — works at current scale, no user outcomes needed). Seed r/jobs,
   r/recruitinghell, the ghosting-spreadsheet communities.
2. **Show HN** — a measurement-and-ethics technical story (pulse-lane architecture,
   grounding checker, receipt event timeline, Billing Bill of Rights kicker), never an
   "AI job agent" story.
3. **Visa community** — free wage-level odds calculator + "we scored N postings under
   the new weighted lottery" data post → Visa Pass conversion; distribute via r/h1b,
   r/f1visa, OPT groups, international-student offices; time to fall OPT season.

---

## 8. Roadmap

**Now (weeks 1–6) — truth made visible**
1. Migration steps 1–2: deletion pass, price-truth fix, card truthification, Truth Strip v0.
2. Outcome Loop v1 (forecast + closure + aim budget) — the #1 pain, smallest build.
3. SpotCheck landing page (`/check`) — the wedge is already built; give it a front door.

**Next (weeks 5–10) — the wedges**
4. Visa Odds Engine + Visa Pass (launch moment 3 prep).
5. Trust Economics (Stripe + Bill of Rights + Hired Mode) — revenue turns on here.
6. Today home default-flip.

**Later (weeks 8–14) — the moat compounds**
7. Public Truth Layer (CompanyTruthStat, /companies pages, launch moments 1–2).
8. Recruiter Bridge (receipts → boutique marketplace un-hiding).
9. PWA + push; Career Profile consolidation.

**Explicitly do NOT do** (unchanged from July 10, reaffirmed by the skeptic judge):
scrape LinkedIn/Indeed · auto-submit · inflate scores · buy LinkedIn-sourced data ·
promise interview-skipping to recruiters · pivot to a data/standards company before
copilot density exists · build WhatsApp now · chase AI mock interviews (crowded,
distrusted).

---

## 9. Immediate hygiene fixes found during research (do these this week)

1. **Three-way price mismatch (live trust bug):** `PLAN_PRICES` in models.py says
   $19/$49/$99; pricing.html says $0/$29/$99; upsell strings in server.py say $19.
   Render everything from one corrected table.
2. **landing.html meta description says "auto-apply to jobs"** — contradicts the
   product's core doctrine and poisons the positioning; also remove the
   "cryptographic database checks" overclaim and the three pseudo-testimonials.
3. **pricing.html FAQ describes the legacy single-user Telegram flow** ("You tap
   Approve — then it submits") — reads as auto-submit; rewrite around
   review-in-browser + receipts.
4. **`linkedin_rapidapi` source** conflicts with the compliance doctrine — remove from
   any user-facing surface.
5. **Ashby compensation payload is requested and dropped** (`ashby.py` uses
   `includeCompensation=true`) — one parser away from the most-demanded missing datum.
6. **`/api/public/job-check` has no web surface**; the hidden marketplace has no nav
   entry — both are finished assets earning nothing.

---

## 10. Verification caveats

Competitor scale/pricing figures are vendor self-reports unless a study is named.
Survey stats cite the named source + year; the Wave-1 research pass extracted them from
primary or secondary coverage without a separate adversarial verification round (the
July 10 doc's verified claims stand). Lottery-odds percentages come from the published
DHS weighted-lottery rule as covered in 2025–26 sources — re-verify the exact FY2027
tables against the Federal Register text before shipping the Visa Odds methodology
page. Effort estimates assume one engineer who knows this codebase.
