# Delivery architecture — eligibility, the daily slate, and what we buy finals for

> Written 2026-09-12 from a full read of the pipeline against six days of production
> logs (deploy 28a77eeb, 2026-09-05 → 09-11). Supersedes the parts of
> docs/CAPACITY.md and CLAUDE.md that describe the daily target as a stop.
> The evidence is the Railway deploy log for service `jobagent` over those six
> days, read alongside the code; every production number quoted below comes
> from it. Nothing in that read was modified.

## 0. The objective, stated so it can be optimised

SpotApply is not trying to reach 35. It is trying to maximise

    how many of the best realistically available opportunities we surfaced
    while they were still actionable

subject to a per-plan money ceiling. Those are different objectives and the old
one produced the wrong behaviour three days out of six: the board filled by
mid-afternoon, every scorer switched off, and a posting that appeared at 15:12
waited for 00:00 UTC.

Two quantities were fused and must be separated:

| | Governs | Bound by |
|---|---|---|
| **Slate size** | how many recommendations the user *sees* today | `PLAN_LIMITS[plan]["shortlist_daily"]` (Free 20 / Pro 35) |
| **Attention** | whether we keep *looking* and what we pay to look | `finals_daily`, and the value of what we would learn |

Reaching the slate size must end the first and must not end the second.

## 1. Root causes (the 300-odd audit findings collapse to these)

**RC1 — three front doors, three filters.** `_upsert` is the only writer into a
user's pool, and it applies the country gate only when `preferred_country` is
passed and the role gate only when `role_gate_terms` is passed. Full discovery
passes both. Adoption passes country only when the profile holds one and never
passes a role gate. The pulse lane passes neither. The pulse lane delivers most
shortlists, so most of the per-user pool arrives through the least-filtered door.

**RC2 — location detection is ordered wrong and is too small.** `detect_country`
tested foreign city names before the `, XX` US state code, so real US markets
named after foreign cities were classified foreign. Coverage was 21 countries, so
most of Europe returned "unknown", which the gate keeps. "Remote (Europe)" was
not a region anchor.

**RC3 — role routing matches on generic domain tokens.** Any ≥4-character token
of a target role that is not in `_GENERIC_TOKENS` becomes a standalone accepted
term: `full`, `data`, `software`, `cloud`, `site`, `mobile`, `product`.

**RC4 — freshness measured from the copy, not the posting.** Adoption drops
`first_seen`, `_build_job` stamps `now`.

**RC5 — the daily target is a kill switch.** `allowance()` returns `n=0`; the
pulse fast path returns before Tier-1; three lanes duplicate the cap; nothing can
be replaced because no engagement state exists.

**RC6 — board health conflates throttled / empty / gone.** Adapters return `[]`
for every `httpx.HTTPError`; `job_count=0` demotes to the 72-hour tier; the
retire path counts a 429 toward the five-strike retirement.

Everything else in the audit is a consequence, a duplicate, or independent
infrastructure work (retention on the event loop, the public freshness endpoint,
billing mode, badge semantics).

## 2. Eligibility is deterministic and happens once, before money

`_upsert` is already the only writer into a user's pool and it already holds the
gate. The bug was never a missing filter — it was that the gate is driven by
what the CALLER passes, and the three callers passed different things. So the
fix is not a new module; it is that every door passes the same four arguments:

    _upsert(raw, user_id=..., preferred_country=..., remote_ok=...,
            user_keywords=..., role_gate_terms=...)

and that the country comes from ONE resolution,
`app.common.tenant_prefs.effective_country`, which the scoring PROMPT reads too.
A new `eligibility` module would have been a fourth place for the same rule to
drift.

Order inside `_upsert`, cheapest first, all deterministic:

1. **Profession** — the junk/other-department kill list (unchanged semantics).
2. **Role family** — `matches_title` against the caller's role gate, now without
   the weak domain tokens.
3. **Location / work authorisation** — `location_allowed`, now ordered correctly.

Nothing here calls an LLM, an embedding model, or the network. A candidate that
fails is never written into the user's pool, so it can never consume a Tier-1
call, a Tier-2 call, a queue slot or a row of egress. Validity (closed, ghost,
expired) and duplicate suppression stay where they already are — the ghost
detector at scrape time and the dedupe keys inside `_upsert`.

### 2.1 Location, restated

`detect_country` now resolves in tiers, most-specific first:

1. explicit US signals (`USA`, `United States`, `Remote US`);
2. explicit foreign **country names** (`Canada`, `Ireland`, `Germany`);
3. `, XX` where XX is a US state code — **unless** the string also names a
   foreign city whose ISO-2 code is exactly XX (so "Toronto, CA" stays Canadian
   while "Dublin, OH" becomes American);
4. foreign **city** names;
5. unknown.

Region anchors gained bare `Europe` / `EU` / `UK & EU` and the member list gained
the countries the table was missing. Unknown still means *keep* — the gate stays
conservative — but far fewer locations are unknown now.

## 3. The daily slate: FILL, then CHALLENGE

The day has two modes for each user. The switch is `delivered_today >= target`.

### FILL (delivered < target)
Unchanged from today. Tier-1 gate 40, buy finals in promise order, deliver
anything at or above the shortlist bar (70) that passes the company cap.

### CHALLENGE (delivered >= target)
Discovery, routing, adoption and Tier-1 continue exactly as before — they are
cheap and they are how we learn a good job exists. Two things change:

**What we pay for.** `Allowance.gate` rises from `normal_gate()` to the
**challenger gate**: the fit score of the weakest *replaceable* job on today's
slate. Tier-1 and Tier-2 are asked for the same 0–100 fit judgement, so the
cutoff transfers directly. A candidate Tier-1 rates below the current cutoff
cannot win a slot, so we do not buy its final. This needs no new plumbing:
`allow.gate` already reaches all three lanes as `spend_gate` (`scoring_lane`
`_Ctx.spend_gate`, `pulse_lane` line 527, `pipeline` `keep_gate`), and the band
between `gate` and `spend_gate` has been empty since it was built.

**What we show.** A challenger whose final beats the cutoff by
`slate_displace_margin` displaces the weakest replaceable slate entry. The slate
stays at `target`. If every entry is protected, a challenger that beats the
cutoff by `slate_overflow_margin` is delivered anyway, up to
`slate_overflow_daily` extra jobs.

**Replaceable** means: status SHORTLISTED, `viewed_at IS NULL`, created today.
Anything the user opened, saved, tailored, auto-filled or applied to is
protected, permanently. This mirrors the company-cap displacement rule that has
been in production since August (`_displace_weaker_shortlisted`), which already
refuses to evict anything past SHORTLISTED.

### Why not the alternatives

**A — reserved slots.** Hold 5 of 35 for late arrivals. On the three of six
observed days where the target was never met, the reserve is irrelevant. On the
three where it was met, the reserve caps early delivery at 30 and pays out only
if a challenger arrives; production shows 0–3 jobs per day scoring above the
day's own cutoff, so the expected cost is roughly 2–5 jobs a day *not shown* to
buy back 0–3. Strictly dominated by replacement, which costs nothing when no
challenger arrives.

**B — time buckets.** Arrival is not uniform: new postings peak 13:00–22:00 UTC
at 1.1–1.8k/hour against under 400/hour overnight. Fixed per-bucket allocations
starve the peak and waste the trough. Rejected.

**C — continuous top-K alone.** Correct delivery semantics, silent on spend. With
the gate left at 40 we would buy finals all evening for candidates that cannot
beat a cutoff of 76.

**E — dynamic gate alone.** Correct spend semantics, but with a hard cap of 35
shown, a 92 arriving at 15:12 has nowhere to go.

**C + E** is the design above: E decides whether to pay for the judgement, C
decides whether the judgement wins a slot.

### Worked example

Pro user, target 35, slate full at 14:00 with a weakest replaceable entry of 71.

| Time | Event | Gate | Outcome |
|---|---|---|---|
| 14:05 | repost, Tier-1 44 | 71 | no final bought; job stamped and drained. Cost 0. |
| 15:12 | direct-ATS role, 10 min old, Tier-1 88 | 71 | final bought → 92. 92 ≥ 71 + 5, so the 71 (unviewed) is displaced and the 92 is delivered. Slate still 35. Cost one final. |
| 16:40 | good role, Tier-1 74, final 73 | 72 (cutoff moved) | 73 < 72 + 5 → not delivered, keeps its score, stays visible in All Jobs. |
| 19:00 | strong role, final 90, but every slate entry has been viewed | 72 | 90 ≥ 72 + 15 → delivered as overflow (1 of 5). Slate 36. |
| 20:00–00:00 | nothing above the cutoff | 72 | no finals bought. The old design also bought none, but it also stopped Tier-1. |

## 4. Ranking utility

Ordering the slate, and deciding what a challenger must beat, uses a bounded
utility rather than the raw final score:

    U = fit × V × E × D + F

* `fit` — the authoritative 0–100 final.
* `V` validity ∈ [0.5, 1.0]: direct ATS 1.0; aggregator copy 0.9; ghost score
  above 0.4 → 0.7; link unverified → 0.5.
* `E` eligibility ∈ {0.8, 1.0}: exact country/work-auth match 1.0, ambiguous 0.8.
  Ineligible never reaches this function.
* `D` diversity ∈ [0.7, 1.0]: 0.9 per extra active application at the same
  employer, 0.7 for a known staffing firm or re-poster.
* `F` freshness ∈ [0, 6], additive and capped: +6 under an hour of known age,
  +4 under six hours, +2 under a day, 0 beyond.

Freshness is deliberately additive and capped so it can only break near-ties:
a 95 at 14 hours scores 97, a 72 at 10 minutes scores 78. It cannot invert a
quality gap, which was the explicit requirement.

## 5. Board scheduling

A board's next poll comes from a class, and the class comes from the **outcome**
of the last fetch, not from the row count:

| Class | Entered when | Cadence |
|---|---|---|
| HOT | posted a new job within 7 days, or a user watches the employer | 5 min |
| WARM | live, has jobs, no recent new posting | 60 min |
| COLD | fetched OK and genuinely empty | 12 h |
| ZERO_YIELD | fetched OK and empty for 30 days | 72 h |
| THROTTLED | HTTP 429 / rate-limit | exponential from 15 min, **no failure count** |
| INVALID | 404 / 410 / slug gone | retired |

The critical correction is that an adapter now reports *why* it returned nothing.
`FetchResult(status, jobs)` distinguishes OK-and-empty from throttled from gone,
so a rate limit can no longer demote a live board to the 72-hour tier or retire
a real employer after five busy afternoons.

## 6. What this does not do

It does not add a lane, a timer, or a background process. It removes the
duplicated shortlist cap from three lanes into one module, removes the second
final-purchase ordering policy, and turns one stop into one gate.

It does add four settings, all of them thresholds on behaviour that already
existed rather than new machinery: `slate_displace_margin`,
`slate_overflow_margin`, `slate_overflow_daily`, and `slate_challenge_enabled`
— a kill switch, because this changes what the product does every evening and a
revert should not need a deploy. `default_intake_country` is a fifth, and exists
so the gate and the prompt cannot disagree again.

## 7. What shipped, and what did not

Implemented and under test (2026-09-12):

* eligibility at every door, the tiered country resolution, the domain-token
  fix and the eight role families (§2);
* FILL/CHALLENGE, `slate.place()` as the one placement path, `viewed_at`, the
  challenger gate (§3);
* one final-purchase ordering across both lanes (`order_by_promise`);
* `first_seen` carried through every copy (§2, RC4);
* fetch outcomes and the 429 rule (§5, the correction only — not the full
  adaptive priority scheduler);
* batched shared-pool retention off the event loop, and a cached failure on the
  public freshness route;
* score-kind semantics on the board;
* dormancy: paid searches keep running, free users are told.

DESIGNED HERE, NOT IMPLEMENTED — deliberately, and each for the same reason:

* **§4, the utility function.** The multipliers (validity, diversity, the
  staffing penalty) are guesses until they are fitted against real outcomes.
  Shipping guessed weights would move which jobs get delivered, and the one
  quality number we have — 15.2% of finals clearing the 70 bar — currently
  matches the design. Fit V and D against `Application` outcomes first, then
  turn it on for the slate ordering and the cutoff.
* **§5, the priority scheduler** (`user_demand x expected_yield x freshness_need
  / fetch_cost`) and the HOT/WARM/COLD/ZERO_YIELD classes as a stored column.
  The 429 correction removes the destructive half; the rest needs the registry
  census (how many boards sit in each tier, and what each actually yields),
  which is a query against production, not a code change.
* **Requisition canonicalisation across ATS + aggregator copies.** Needs a
  fingerprint experiment on real duplicates before a key is chosen; picking one
  wrong merges distinct reqs, which is worse than showing two.
* **The per-1000-jobs funnel counters.** `FunnelEvent` already has stages
  nothing writes; adding more before reconciling that would make the analytics
  less trustworthy, not more.
