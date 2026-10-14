# Earliness receipt — "Applied 3h 12m after posting"

*2026-10-11. The founder asked for a feature nobody else has that gets users
more replies, small enough to build in days. This is the pick, why, and how it
is measured.*

## The gap

Every auto-apply tool promises speed; none proves it. LinkedIn's "4x more
likely to hear back", Lever's "70% of interviews from first-week applicants",
StartWire's "8x" — none traces to primary data (research 2026-10-11, six
angles, 177 findings). No seeker-side product shows how early a user actually
applied, and none publishes a measured speed number.

What IS established: applications to a posting are front-loaded — about 41%
arrive in the first 48 hours and 56% within four days (Dice.com job-board
microdata, Davis & Samaniego de la Parra, St. Louis Fed); the first week carries
2–3x the weekly volume of later weeks (Ashby, 13M applications); recruiters
read in arrival order and stop once a shortlist forms, and almost no ATS
auto-rejects on content (Enhancv 2025, n=25). So an application inside the
first 48 hours is read alongside fewer rivals, before any shortlist exists.
What is NOT established: callback probability as a function of hours since
posting — so the product states a clock, never odds.

SpotApply already finds postings inside that window (5-minute pulse lane, true
publish dates from five ATSes, fresh alerts with measured latency). Nothing told
the user which shortlisted jobs were still inside it, or afterwards whether they
had been early. Baseline, read-only, 2026-10-11: of 22 applications submitted
through SpotApply, 13 (59%) went in within 48h of the posting's reference time,
median 29.6h, p90 317h. Fresh alerts: 4,829 since launch, 50 in the last week.

## What shipped

* **One reference clock per posting** — `app/strategy/earliness.py`
  `reference(job)`: the EARLIER of a trusted ATS publish date (Greenhouse,
  Lever, Ashby, SmartRecruiters, Workable read a true timestamp) and when we
  first saw it. A `posted_at` later than `first_seen` is an edit or refresh date
  and is ignored, so the rule can only make a receipt less flattering. Workday,
  Personio and aggregators get the "found" clock and the copy says so.
* **The receipt** — `Application.applied_after_min` + `applied_ref`
  ('posted' | 'found'), stamped in `mark_as_submitted`. Email imports never get
  one (their `submitted_at` is the import time). The Submitted and Interviewing
  cards show "⚡ Applied 3h 12m after posting" (or "after we found it"); the
  Submitted pane shows "Early (inside 48h) on K of N applications this month".
* **Apply today** — shortlist rows inside the window carry `data-early`;
  "Freshest" order puts them first, best fit first, under a divider "⚡ Apply
  today — early window (N)". `urgency.assess` reads the same clock: "⚡ Early
  window · 31h left" (so a Greenhouse edit date can never light the hot tier).
* **The first response** — `Application.first_response_at`: a confident email
  signal (rejection / interview / offer, or an assessment invitation) or a
  recorded outcome; first write wins; 'ghosted' never sets it. This is what
  lets reply rate by apply speed be measured later.
* **Landing proof** — the median minutes from a posting going live to the alert
  (`/api/public/freshness` `median_post_to_alert_min`, already computed) is now
  rendered, only when the week holds ≥ 50 alerts, naming the five boards.
* **Honesty guard** — `earliness.FORBIDDEN_WORDS` ("odds", "likely", "x more",
  "×", "more likely"); every earliness and urgency string is tested against it.
  The untraceable "4× reply rate" pill and "maximizes your interview odds" copy
  were removed.

Setting: `EARLINESS_WINDOW_HOURS` (48). Guard: `tests/test_earliness_receipt.py`.

## Measurement

`python -m scripts.apply_latency_report` (read-only): share of SpotApply
submissions within 24h / 48h of the reference, median hours, clock split,
fresh-alert → submit conversion, and — only once a bucket holds ≥ 30
applications older than 21 days — any-response and interview rate for early vs
late. Success for the ordering: the within-48h share rises against the 59%
baseline by ≥ 10 points over four weeks. If it does not move, the receipt and
the landing number remain the honest advertising surface and the follow-up
nudge (below) becomes the next reply lever.

## Not built, and why (judges' and synthesizer's reasons)

* Quiet-application follow-up (runner-up): without a persisted signal ledger it
  fires on status silence alone — the calendar reminder Teal/Huntr/Enhancv ship;
  2.2% of postings name a recruiter, 0/45 a manager. Build slim after
  `first_response_at` has data.
* "You know someone here" from a LinkedIn Connections export: strongest
  evidence (referrals: Burks et al. QJE 2015; Brown, Setren & Topa 2016) but
  Jobright and Simplify+ own the LinkedIn-graph version; ours would be the
  privacy variant, ~4 days, with real capture friction.
* Pre-apply knockout check from the ATS's real questions: good mechanism,
  ~4 days, unverifiable from this container.
* Any "before X% of applicants" label, any multiplier, applicant counts, a
  published outcomes page before the data exists.
