# HirePath Freshness Strategy — July 2026

Goal: make "freshest jobs, first to apply" the core product and the acquisition
engine for a wide release. Research: deep-research run (105 agents, ~35 sources;
Greenhouse/Lever API claims 3-0 adversarially verified, remainder extracted from
primary docs but unverified due to an API budget limit — flagged inline).

---

## 1. Why freshness is the winning wedge (2026 market data)

- **Ashby, 109M applications / 247K jobs (Q1 2026):** ~291–300 applications per
  hire (3x since 2021). Interview selectivity halved — **3.6–4.7% of
  applications get an interview** vs 7–8% in 2021. But offer-conversion *after*
  an interview is now ABOVE 2021 (10.4% business / 7.3% technical) → **the whole
  bottleneck is getting screened at all — which early applicants win.**
  ([PR](https://www.prnewswire.com/news-releases/new-data-from-ashby-reveals-surge-in-applications-rising-selectivity-and-shifting-recruiter-workloads-302765846.html))
- **Greenhouse benchmarks (640M applications):** applications per job **+111%**
  (≈115 in 2022 → ≈244 in 2025). Popular roles draw **100–250 applications in
  the first 24–48h**.
- **Recruiter behavior:** review in arrival order and stop when the panel fills
  (~8 strong candidates from the first ~40 applications — Teal CEO's "24-hour
  application rule"). Matches our earlier evidence: 70% of interviews go to
  week-one applicants; applying <48h ≈ 3.1x response (LoopCV).
- **Huntr Q1 2026 (240K tracked jobs):** median search-to-offer hit **108 days**
  (+30% QoQ). Users sending 11–20 targeted applications interview at **9.25%
  per application vs 2.58% for 100+ (3.5x)** — fresh + targeted beats volume,
  which is exactly HirePath's thesis vs auto-apply tools.

## 2. The attack vector: LinkedIn is structurally late

LinkedIn aggregates career pages by **crawling**, so a posting is typically
live on the company ATS **18h (large cos) to 36–48h (smaller cos)** before
LinkedIn shows it; its alert digest then fires **once daily (10:00 GMT)** —
up to ~25h more delay (jobstrack's internal research; unaudited but the crawl
mechanics are structural). By the time a LinkedIn alert lands, the job has
been live up to two days and has 100+ applicants. **We read the same ATS
endpoints the career page is served from — there is no crawl lag to beat.**

## 3. Detection engineering (verified per-ATS facts)

One HTTP GET per company returns the whole board. 22K companies polled every
2h ≈ 264K req/day ≈ **3 req/sec sustained — trivial**. HiringCafe rescans 30K
career *pages* 3x/day with headless browsers; our JSON endpoints are far
cheaper. Detection latency is bounded ONLY by our polling cadence.

| ATS | True posted time in public API | Notes (verified) |
|---|---|---|
| Greenhouse | `first_published` (single-job GET); `updated_at` on list | No auth, no documented rate limit, no webhooks; `?content=true` = full JD in one call ([docs](https://developers.greenhouse.io/job-board.html)) — 3-0 verified |
| Ashby | `publishedAt` on list | Cleanest freshness source ([docs](https://developers.ashbyhq.com/docs/public-job-posting-api)) |
| Lever | `createdAt` (ms epoch) — **undocumented**; not in the official schema | Use it but keep first-seen fallback; EU instance `api.eu.lever.co` — 3-0 verified |
| SmartRecruiters | `releasedDate` | ([docs](https://developers.smartrecruiters.com/docs/posting-api)) |
| Workable | `published_on` | |
| Recruitee | `created_at` | |
| Workday | relative only ("Posted 3 days ago") | fall back to our `first_seen` |
| Personio | none | fall back to `first_seen` |

**Pipeline pattern** (industry-standard "monitoring mode"): keep seen job IDs +
`content_hash` (we already have both), emit only NEW postings per scan, stamp
`detected_at`, compute **post-to-detection latency** = detected_at − posted_at
where a true timestamp exists (GH/Ashby/Lever/SR/Workable/Recruitee).

**Adaptive cadence tiers** (the real edge — poll where it pays):
1. **Hot lane (15–30 min):** boards that posted in the last 7 days AND match an
   active user's target roles.
2. **Fresh lane (1–2h):** all boards matched to active users (shipped).
3. **Rotation (6–24h):** everything else via `max_boards_per_run` rotation.
Raise `MAX_BOARDS_PER_RUN` (300 → 1000+) — the constraint was never bandwidth.

## 4. The product: instant fit-matched alerts (free wedge)

Competitor scan: jobstrack sells **alerts alone** — 8,800 companies, claimed
0–3h post-to-alert, email only, no fit scoring. jobalert.world: "every 8h".
Jobright: "posted within 24h" filter. JobCopilot: "every 2h" (stated
inconsistently — soft claim). None combine instant + fit-scored + free.

**HirePath Instant Alerts (free tier):** when a NEW posting appears that scores
above the user's fit threshold → Telegram/email/push within minutes:
*"⚡ Posted 41 minutes ago — Stripe, Backend Engineer, 84 fit. Probably fewer
than 20 applicants. Tailor & apply now?"* We already have the Telegram bot,
matching cascade, and fresh lane — this is wiring, not new infrastructure.
Free alerts create daily habit + urgency; tailoring/autofill convert to paid.

**The credible metric to publish** (Scoutify proved "we timed it" content
works): **median post-to-alert latency** (target: publish "median 47 min")
and **median posting age in your feed** vs LinkedIn's 18–48h. Instrument it
from day one; put the live number on the landing page.

## 5. Growth playbook (HiringCafe-proven)

HiringCafe: direct-from-ATS freshness + free → **1.3M+ MAU with $0 marketing**,
driven by one viral Reddit post converted into an owned community (r/HiringCafe,
68K members). Replicate: launch content = a measured latency benchmark ("we
timed LinkedIn alerts vs ours across 100 postings"), monthly Ghost Job Index
(our ghost_score data), "first-10-applicants" success stories, own subreddit +
r/jobsearchhacks presence. The free ghost-check extension and free instant
alerts are the two shareable wedges.

## 6. Worldwide release — compliant per-country supply

Order of attack:
1. **US (now):** current ATS fleet + USAJobs official API (free keys, posted
   dates, documented rate limits).
2. **Europe (next, biggest unlock):** **EURES ≈ 1.5M jobs across 31 countries**
   — the single largest source in the open dataset (bigger than Workday 449K,
   SmartRecruiters 213K, Greenhouse 170K). Public search endpoint with
   MOST_RECENT sort; caveat: the API is reverse-engineered/unofficial —
   isolate it as a best-effort source. Plus **join.com (23.5K companies,
   public JSON)**, Personio XML (DACH), Teamtailor jobs.rss (Nordics),
   national services (Bundesagentur DE, Arbetsförmedlingen SE), Lever EU
   instance, jobs.cz / InfoJobs ES from the dataset.
3. **Everywhere else:** Adzuna official API (~19 countries incl. IN/AU, posted
   dates included — also our per-country freshness benchmark) + Jooble partner
   API (60+ countries) as aggregator gap-fill (discovery-only links where the
   source ATS is unknown).
Per-country freshness = same pipeline; measure median posting age per country
against Adzuna's posted dates.

## 7. Build list (in order)

1. `posted_at` extraction per ATS scraper (first_published/publishedAt/
   createdAt/releasedDate/published_on/created_at) + `detected_at` stamp +
   latency metric in analytics.
2. Hot-lane adaptive polling + raise `MAX_BOARDS_PER_RUN`.
3. Instant alert dispatch: new-job event → fit gate (existing cascade, cheap
   stages only for speed; LLM rerank async after alert) → Telegram/email.
4. Freshness boost in `blended_score` + "Fresh <24h" default filter + posted-age
   chips everywhere (partially shipped).
5. Latency dashboard + public landing-page counter.
6. EU sources: join.com scraper, Teamtailor RSS, EURES (isolated), Adzuna
   countries config.

**Caveats:** jobstrack's 18–48h LinkedIn-lag figures are its own unaudited
research (the crawl+digest mechanism itself is well documented); Lever
`createdAt` is undocumented and could vanish; EURES API is unofficial;
claims beyond Greenhouse/Lever docs were extracted from primary sources but
missed the adversarial verification pass (API budget).
