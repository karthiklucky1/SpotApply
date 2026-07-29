<!-- Produced by a multi-agent design workflow: 4 independent designs
(selection-theory, cost-decomposition, retrieval-native, wildcard), 3 adversarial
judges (cost / quality / engineering lenses) that recomputed all arithmetic, and
one synthesis. All SpotApply numbers trace to docs/CAPACITY.md, docs/ARCHITECTURE.md
and cited file:line anchors. -->

# CardRace — the final SpotApply matching engine

*Merged from the four judged designs. Chassis: **CardMatch** (unanimous judge #1). Certification and stopping: **RACE-K**. Validation discipline and lazy reasoning: **MatchSpec**. Coverage economics and portfolio selection: **Passport & Portfolio**. Every number below is derived from the repo's documented figures (CAPACITY.md, ARCHITECTURE.md, verified `file:line`) or from the four briefs' judge-verified arithmetic; where an input is unmeasured, it is named as such and scheduled for measurement in Phase 0.*

---

## 1. Name and essence

**CardRace: pay the LLM to read, never to judge pairs — then certify the arithmetic and race only the doubt.**

One Haiku call per distinct job turns a posting into a structured **JobCard** (O(jobs), shared by every tenant — "scrape once, serve many" extended to "understand once, serve many"). One extension of the already-existing signup résumé-parse call turns each user into a **UserCard** (O(users)). The per-pair judgment becomes `g(UserCard, JobCard)` — deterministic, 20-microsecond CPU arithmetic computing the *same four factors Claude is already contractually forced to emit* (`skills/experience/location/work_auth`, verified at `app/matching/reranker.py:194-197`), calibrated on the 57,309 stored Claude finals with a proper train/holdout split. Split-conformal bands with Clopper–Pearson certification (alpha = 0.05) sort every job into AUTO-IN (certified >= 95% precision vs the Claude >= 60 bar), AUTO-OUT (certified <= 5% miss), or BAND; only band jobs contending for the user's top-K get real Claude finals — batched per user behind the existing prompt cache, plan-capped, ~5–8/user/day. A stopping rule guarantees no seat is ever filled below the bar; a continuous random audit stream (including auto-rejects) keeps the certificates honest; a facility-location diversity re-rank orders the final K *within the bar-clearing set only*. LLM cost collapses from O(users x jobs) to **O(J + U + K_opened)**, bounded by the size of the job market rather than the user count.

---

## 2. The mechanism, end to end

### 2.1 A job's life

1. **Discovery -> shared pool** — unchanged. Scheduled lanes write once to `Job.user_id == "__shared__"`; dedupe by content hash + `cross_source_slug` (`app/discovery/pipeline.py:128, :358-412`) means aggregator reposts collapse to one row.
2. **Free gates** — unchanged and still first: rule filter, ghost detector, embedding cosine floor, door/RoleBar gate (ARCHITECTURE.md §4 stages 2–5). These already drain 80% of stamped rows for free (CAPACITY.md banner: of 335,867 stamped rows, 60% Tier-1-drained, 20% ghost). In the new engine the ghost gate additionally receives a small audit slice (§2.3), because its false-drop rate has never been measured.
3. **JobCard mint — the one LLM read, lazy and demand-driven.** The first time the job survives free gates *and* enters at least one user's prefilter (role family x country x remote policy), the **card lane** (cloned from `scoring_lane.py`'s read -> LLM -> idempotent-write worker pattern, honoring `llm_budget_exhausted()` and the provider circuit breaker) issues one **Haiku** structured extraction (~$0.0038; Haiku, not gpt-4o-mini, because extraction is the correlated-failure-prone step — judge consensus): canonical role family + axis (the same enum `RoleBar` uses, `door_match.py:67`), years_min/max + level, must-have and nice-to-have skills mapped to a ~2–3k-term ontology with weights, hard disqualifiers (clearance/citizenship/licensure), location + remote-policy enum + timezone band, visa stance (sponsors yes/no/silent), domain tags, salary if stated, and a **per-field confidence flag**. The card is versioned, keyed by content hash/`cross_source_slug` so all sources and all tenants share it, and stored on the shared row; adoption copies a pointer, not a re-analysis. A job nobody's prefilter touches costs $0. At small N a per-user carding budget (~50 cards/user/day, cosine-prioritized) keeps the fixed term near parity with today; the budget stops binding as overlap saturates.
4. **Instant fan-out.** When the pulse lane lands one new job: mint one card, then run `g()` against **every** watching user's UserCard in milliseconds. AUTO-IN -> instant fresh alert with **zero LLM latency and zero budget contention** (this replaces the pulse fast path's per-user prescore->Claude chain and its 10-finals/tick global cap). BAND -> queued for the next batched escalation cycle, unless it would enter the user's current top-3, in which case one immediate final is allowed (cold-priced, capped ~1/user/day). This is the O(1)-LLM "first to apply" fan-out the judges called the best product-fit idea in the field.
5. **Card health.** Cross-user disagreement detector: if >= 2 users' escalated finals disagree with a card-predicted factor by > 20 points, Sonnet re-extracts the card. Single-adopter jobs — which that detector cannot see — are covered by the random audit stream. Low-confidence fields never auto-decide; they route the pair to BAND.

### 2.2 A user's day

1. **UserCard** — compiled by extending the signup résumé-parse Haiku call that already exists (LLM #1, `server.py:1741`, ARCHITECTURE.md §3.1): skills with evidence depth on the same ontology, years overall and per axis, seniority, work authorization, locations/remote/relocation, target role families, domains. Recompiled only on résumé upload or role edit — the exact hooks that already re-trigger adoption. **Replay validation (MatchSpec graft):** before a new/recompiled UserCard goes live, `g()` must reproduce that user's own historical Claude finals with Spearman rho above threshold; failure re-compiles with the misses in-prompt. Cold-start users (no history) get a bounded first-week Claude-final allowance — O(new users), not O(users x jobs).
2. **Every adopted job is scored at adoption time** by `g(UserCard, JobCard)`:
   - `work_auth`: predicate lattice (needs-sponsorship x sponsors yes/no/silent x citizenship/clearance), backed by the existing `H1BSponsor` table and `intelligence/work_auth` — deterministic.
   - `location`: the door gate's existing hand-written decision tree (`door_match.py:101` `_location_finding`) generalized with the card's remote-policy enum — *already deterministic in production today*, and nobody calls that a quality regression.
   - `experience`: piecewise years/level window arithmetic mirroring the RoleBar gap logic.
   - `skills`: weighted must-have coverage + nice-to-have bonus; coverage = exact ontology ID, alias, or precomputed MiniLM skill-phrase cosine >= 0.75 at a 0.7 discount (a synonym lookup over ~2–3k phrase vectors via the existing `matcher._get_embed_model()` — not a pair scorer).
   - Overall = `min(blocker_cap, weighted combination)`, mirroring the hard-blocker rule in Claude's own rubric, mapped to 0–100 by a monotone calibration fitted on the **train split only** (§3.4). Stamped immediately as `rerank_score` + a Claude-shaped `rerank_breakdown` + a templated rule-trace `rerank_reasoning` (DoorFinding-style), so `blended_score`, the 65 board filter, fresh alerts, company-cap displacement, and the dashboard work untouched. **The `rerank_score IS NULL` backlog and "Queued" purgatory cease to exist** — 2,000-job corpus scored in ~40 ms of CPU.
3. **Banding.** The calibrated score falls into AUTO-IN / BAND / AUTO-OUT per the split-conformal thresholds (§3.4). AUTO-OUT is stamped and drained through the existing synthetic-score mechanism. AUTO-IN is shortlist-eligible immediately, flagged `score_source='conformal'`.
4. **The race (escalation).** Once per scoring cycle, per user: sort BAND jobs by calibrated score; escalate to a **real Tier-2 Claude final** — the untouched `Reranker.score()` path — only those contending for the day's top-K, **batched immediately after `prewarm_cache` (`reranker.py:620`) so every call bills warm at $0.0033, not cold at $0.0087** (the pricing correction all three judges demanded; batching is an explicit design decision, not an assumption). **Stopping rule (RACE-K graft):** stop when K seats hold bar-clearing jobs *or* every remaining band job's certified upper bound is < 60. On thin days nothing is spent confirming scarcity and the list is honestly short — **K is a ceiling, not a promise.** Expected escalations ~5–8/user/day, hard-capped by `PLAN_LIMITS["finals_daily"]` repurposed as the escalation budget (`scoring_lane.py:356`; Pro drops 50 -> 20 with headroom).
5. **Seat assembly.** A seat is occupied only by (a) a job with an actual Claude final >= 60, or (b) a certified AUTO-IN. Then — and only then — a greedy facility-location diversity re-rank (Passport graft, demoted per judge consensus to a post-hoc step **within the bar-clearing set**) orders the K seats to cover the user's skill facets, with the company cap enforced as the partition matroid it already is (greedy keeps a 1/2 guarantee). Worst case it seats a certified 62 over an 80 to cover a facet — bounded by the bar, visible in the displayed scores, tunable via lambda. Optional Hedge personalization (skip/save -> multiplicative facet-weight updates, eta = 0.1, weight floors + decay) also re-ranks **within the certified set only**, so it can never promote a below-bar job — closing the certification-drift gap judge 2 flagged.
6. **Reasoning.** Escalated seats carry genuine Claude reasoning (they got real finals). AUTO-IN seats always carry the templated rule trace (a human-readable factor-by-factor "why" the old one-sentence Claude reason never fully gave); a deeper LLM-written analysis is generated **lazily at drawer-open only** (MatchSpec graft), capped ~5/user/day — O(K_opened), never for the drained pool.

### 2.3 Governance — how the certificates stay true

- **Random audit lane (RACE-K graft — the fix for CardMatch's biggest hole):** ~40 real Claude finals/day platform-wide, importance-sampled across AUTO-IN, band-skipped, **AUTO-OUT** (the label-censored region), single-adopter jobs, and a small slice of ghost-gate drops. Cost 40 x $0.0087 (cold, conservatively) = $0.35/day, O(1) in N. Sized to a detection guarantee, not a round number: ~140 samples detect a 95% -> 85% precision drop at 0.8 power (MatchSpec's power framing) = ~3.5 days of stream.
- **Bins update from the random audit stream ONLY.** Escalated finals are authoritative for their own jobs but never re-fit the bins — eliminating the band-selected-label feedback bias judge 2 identified.
- **Weekly binomial test** on realized AUTO-IN precision; violation automatically **widens the band** (raises t_hi) — quality failures convert to escalation cost, never to silent contamination.
- **Recalibration is wired to the Claude model-version string + rubric hash.** A model or prompt bump forcibly invalidates the bins (temporary all-BAND regime) because it redefines ground truth.
- **Per-user fallback trigger:** if a user's audited/escalated finals disagree with card predictions beyond threshold (e.g. factor error > 20 on >= 3 jobs), that user reverts to the full legacy LLM cascade — O(atypical users), the safety valve for career-changers and thin résumés outside the calibration support.

---

## 3. The math

### 3.1 Old cost model (the baseline being replaced)

Per Pro user per day, warm-cache: `C_old = F*c_f + (F/a)*c_p`, with F = 50 finals/day (`PLAN_LIMITS["finals_daily"]`), c_f = $0.0033 warm (CAPACITY.md §3.3), c_p = $0.0002.

CAPACITY.md §7 states the advance rate `a` is **unmeasured**, and the measured 0.3 belongs to the old gate-35 regime; under the current 60/60 gates a is plausibly ~0.15 (judge 2's correction). So the honest baseline is a range:

```
a = 0.30:  50 x 0.0033 + 167 x 0.0002 = $0.198/user/day = $5.94/user/month
a = 0.15:  50 x 0.0033 + 333 x 0.0002 = $0.232/user/day = $6.95/user/month
```

Platform/day = $0.198–0.232 x N: **N=10: $1.98–2.32 · N=100: $19.8–23.2 · N=1000: $198–232.** Strictly O(N) forever — and at N=1000 it demands 50,000 finals/day, past the Anthropic rate-limit wall CAPACITY.md §6 item 4 identifies as binding once the config caps are lifted (~100–150 users is where that wall arrives). The old design is not merely expensive at scale; it is **infeasible**.

### 3.2 New cost model

```
C_new(N)/day = P(N)*c_card  +  N*(eps_b*0.0033 + eps_p*0.0087 + V*0.0087)  +  A*0.0087  +  compile term
```

- **Cards:** `P(N) = J_pool * (1 - (1 - d/J_pool)^N)` — the coupon-collector coverage curve (Passport graft, replacing CardMatch's judge-refuted cov(N) constants). Inputs, both measured in Phase 0 because CAPACITY.md §2.1 says net-new inflow is *not code-determined*: d ~= 100 free-gate-surviving distinct jobs/user/day (from the documented 100–400 adopted/user/day, CAPACITY.md §2.1/§4 step 4), J_pool ~= 4,000 net-new relevant jobs/day. c_card = $0.0038 (Haiku: ~5k-tok cached rubric at 0.1x = $0.0005 + 1,300-tok JD at $1/M = $0.0013 + 400-tok JSON out at $5/M = $0.0020 — judge-verified against CAPACITY.md §3.1 rates).
- **Escalations:** eps_b = 5 batched warm finals x $0.0033 + eps_p = 0.5 instant pulse finals x $0.0087 (cold, honestly priced) = **$0.021/user/day**. Plan-capped at 20 (Pro).
- **Lazy prose:** V ~= 2 drawer-opens/day x $0.0087 cold = $0.017; capped at 5.
- **Audits:** A = 40/day x $0.0087 = $0.35 — flat in N.
- **Compiles:** ~0.1 UserCard recompiles/user/day piggybacking an existing call — negligible.

Marginal per-user: **~$0.038/day = ~$1.15/user/month expected**; every term individually capped, so the worst case (20 escalations + 5 proses + 1 instant) is ~$0.11/day = **$3.4/month hard ceiling — still half the old baseline even when the band swallows everything.**

### 3.3 The numbers

P(N) at d=100, J_pool=4,000: P(10)=895, P(100)=3,682, P(1000)->4,000 (cap).

| N | OLD $/day (a=0.30–0.15) | NEW $/day | NEW per-user/month | vs old |
|---|---|---|---|---|
| 10 | $1.98–2.32 | 895 x .0038 + .35 + .38 = **$4.13** (card-budget mode: 473 cards -> **$2.53**) | $12.4 (budget mode **$7.6**) | 1.1–2.1x MORE — honest; you buy recall, zero backlog, instant alerts |
| 100 | $19.8–23.2 | 3,682 x .0038 + .35 + 3.80 = **$18.14** | **$5.44** | 1.1–1.3x cheaper (1.3–1.5x at J_pool=3,000) |
| 1000 | $198–232 (infeasible: 50k finals/day) | 4,000 x .0038 + .35 + 38.0 = **$53.6** | **$1.61** | **3.7–4.3x cheaper — and feasible** |
| 10,000 | $1,980–2,320 (categorically infeasible) | 15.2 + .35 + 380 = **$396** | **$1.19** | ~5–6x, asymptote $1.15/user/mo |

Marginal cost of user N+1 = `d*(1 - P(N)/J_pool)*c_card + $0.038` -> **$0.038/day = $1.15/month** as overlap saturates. The platform card line is **capped at J_pool x c_card ~= $15/day forever** — cost bounded by the job market, not by N. Feasibility, not just price: at N=1000 CardRace makes ~5,540 Claude finals/day (escalations + audits) — *fewer than the old engine makes at N=111* — plus ~4,000 short card calls; comfortably under the rate-limit wall the old design hits at N~100–150. Sensitivities: eps=15 -> marginal $1.6/mo (still >= 3.5x at N=1000); J_pool=10,000 -> card line $38/day, crossover vs old moves from N~85 to N~180; d=250 -> small-N card-budget mode simply stays binding longer. CPU: 3,000 x N x 20 us = 60 CPU-s/day at N=1000, while *retiring* the 120-pair x ~1 s cross-encoder wall, the ~7x-wasteful embedding-gate re-encodes, and adoption's <= 1,501-encode x 16-passes semantic tax (~370 s/user/day) documented in CAPACITY.md §5.4.

### 3.4 The quality-guarantee construction

Ground truth = Claude final >= 60 (the current shortlist bar; CAPACITY.md banner: 44.5% of the 57,309 finals >= 35, 11.6% >= 65, interpolating ~1.1%/point -> **P(>=60 | reached Claude) ~= 17%**, exactly computable from the dataset).

1. **Pre-flight (Phase 0):** (a) count how many of the 57,309 finals still have JD text — `JOB_PURGE_MAX_AGE_DAYS=60` hard-deletes closed unapplied jobs (`config.py:279`, CAPACITY.md §1.6), the shared blind spot judge 3 caught; shortfall triggers the bootstrap path (shadow-period labels: the live cascade produces N x 50 fresh finals/day, ~500–750/day today -> a replacement holdout in 3–4 weeks). (b) Measure **Claude's own test-retest self-agreement** on ~500 repeat-scored pairs (~$4.35): every target below is stated relative to that real noise floor, not a fictional 100%.
2. **Backfill:** card the ~30–40k unique jobs behind the surviving finals: 30–40k x $0.0038 = **$114–152, one-time**.
3. **Split-conformal calibration (MatchSpec's discipline, RACE-K's certificates):** fit the ~30–50 named factor weights + isotonic map on **45,847 finals**; place band thresholds on the **held-out 11,462** never touched during fitting. Mondrian cells: ~20 (role-family-group x score band), average ~570 holdout points/cell, floor 400 (thin cells default to BAND). Per-cell Clopper–Pearson at alpha = 0.05: t_hi = smallest score with certified P(y >= 60 | s >= t_hi) >= 0.95 (binomial CI half-width at p=0.95, n=570: 1.96*sqrt(.95x.05/570) = ±1.8% — statistically real); t_lo = largest score with certified P(y >= 60 | s <= t_lo) <= 0.05.
4. **Launch gates, all measured offline before anything ships:** (i) per-factor MAE vs Claude's stored factor scores — location and work_auth expected near-exact (one is *already* rule-decided in production), skills/experience are the real test; (ii) decision agreement at the 60 bar on the holdout, measurable to **±1.3% at 95%** (Hoeffding: sqrt(ln(2/0.05)/(2 x 11,462)) = 0.0127); (iii) precision@K >= 0.90 relative to the measured noise floor; (iv) **top-decile rank-recall >= 90% into band-or-better** — judge 2's correction that count parity is not quality parity: the 80+ scorers must at least reach escalation, where Claude decides them; (v) measured band mass eps-hat within budget; (vi) extraction error e_J from 500 Haiku-vs-Sonnet double-extracts below target (~$8).
5. **Recall framing (Passport's reframing, judge-endorsed):** the bar is not "Claude on everything" — it is "Claude on 50 jobs/day," which surfaces ~50 x 17% ~= **8.5 true >= 60 jobs/user/day**, while daily adopted inflow of 100–400 contains at least ~3–12 (0.17 reach-rate x 0.17, a lower bound — the drained 83% holds unmeasured extra mass, which the drain audits now measure). g() evaluates **100% of the carded pool** instead of 120 CE slots from a 2,000-cap corpus (ARCHITECTURE.md §4) — the recall gain the old architecture cannot buy at any LLM budget.
6. **Shipped-shortlist guarantee, by construction:** every seat = a literal Claude final >= 60 (escalated) or a certified >= 95%-precision auto-in; expected sub-60 contamination on a 15-seat day with ~half auto-in ~= 0.05 x 7.5 ~= **0.4 jobs, dialable to ~0.1 at alpha = 0.01** at a known escalation cost; recall floor certified at t_lo (<= 5% certified miss among carded jobs) with the honest caveat in §4; thin days produce short lists, never below-bar filler.

### 3.5 Where the remaining LLM spend lives — and why it can never be O(U x J)

| Term | Scales as | Bounded by |
|---|---|---|
| JobCards | O(J x coverage), sublinear amortization, **capped at J_pool x $0.0038/day** | the job market, not N |
| UserCards | O(U x résumé edits), piggybacked on an existing call | edit rate |
| Escalations | O(U) with a small constant eps (band mass), **independent of pool size** | plan cap (Pro 20/day) |
| Audits | O(1) | fixed 40/day |
| Lazy prose | O(K_opened) | 5/user/day cap |

The per-(user x job) operation is 20-microsecond CPU arithmetic. No term multiplies users by jobs; doubling adopted inflow from 100 to 400 jobs/user/day costs **$0.00** of additional LLM (the old system: +$0.13–0.20/user/day if it could even score them — it cannot; CAPACITY.md §2.6 documents the 20–300x discovery-vs-scoring imbalance this dissolves).

---

## 4. Quality preservation — every surviving judge attack, answered

| # | Attack (judge consensus) | Answer in CardRace |
|---|---|---|
| 1 | *No audit of the auto-drain region; label censorship where nobody can see failures* | The random audit lane samples AUTO-OUT (importance-weighted), single-adopter jobs, and ghost drops; first month over-samples (~500/week); bins recalibrate from this stream. |
| 2 | *Correlated card failure poisons a job for all tenants at once* | Haiku (not mini) extraction; per-field confidence -> BAND routing; cross-user disagreement -> Sonnet re-mint; audits cover single-adopter jobs; e_J measured pre-launch. Detectors are reactive — acknowledged as residual risk R2. |
| 3 | *Wilson >= 0.9 admits up to 10% contamination* | Tightened to Clopper–Pearson alpha = 0.05 (>= 95% certified precision), computed on held-out data only; alpha is a dial with a priced escalation cost. |
| 4 | *Thin days filled with best-of-a-bad-lot (MatchSpec's fatal flaw)* | Stopping rule: seats require bar-clearing evidence; K is a ceiling. |
| 5 | *Certified thresholds selected on the same data that certifies them (RACE-K's flaw)* | Explicit 45,847/11,462 split; thresholds placed on the holdout only. |
| 6 | *Re-fitting on band-selected escalation labels biases the tails* | Bins update from the random audit stream only. |
| 7 | *57k labels come from ~10–15 résumé archetypes (CAPACITY.md §4)* | Guarantee honestly scoped to current archetypes; per-user replay gate + audit-disagreement fallback route atypical users to the full LLM path; concordance re-verified as user diversity accrues. |
| 8 | *Ontology decay silently deflates skills scores* | Skills-factor MAE on the audit stream is the canary metric with an alarm; monthly refresh from card outputs; MiniLM synonym bridge as backstop. A permanent chore — owned, not hidden. |
| 9 | *Warm-cache pricing is an artifact unless batching is explicit* | Escalations are batched per user per cycle behind `prewarm_cache`; the two unbatchable call types (instant pulse, prose, audits) are priced cold at $0.0087 in §3.2–3.3. |
| 10 | *Auto-decided jobs have no "why" text* | Rule-trace reasoning always; real Claude reasoning on every escalated seat; lazy LLM prose at open, O(K_opened). |
| 11 | *Diversity/personalization trade score for coverage off the certified ranking* | Both operate strictly within the bar-clearing set; worst case bounded by the bar itself. |
| 12 | *Untrained token-maxsim as a scoring engine* | Rejected outright — not grafted. MiniLM stays in its proven roles: retrieval and phrase-level synonym lookup. |
| 13 | *Constraint-2 boundary: isotonic + ~50 named weights is statistical fitting* | Stated for explicit founder ratification: it is counting-based, monotone, hand-auditable, non-neural — every parameter a named number in a config file. If the founder draws the line tighter, the fallback is Passport's hand-set rubric weights + single threshold, at a measured accuracy cost. This is a decision, not a footnote. |
| 14 | *cov(N) internally inconsistent; J_pool conflated with the adoption window; a=0.3 stale* | Replaced by the coupon-collector model with d, J_pool, and a all measured in Phase 0; baseline presented as a range. |

**The two real risks, stated plainly:**

- **R1 — the skills factor is the load-bearing unknown.** Factorized coverage arithmetic may systematically miss holistic judgment ("strong generalist compensates for missing skill X"), concentrated on career-changers (false drops) and keyword-dense JDs (false admits). The entire design is **falsifiable before cutover** for ~$150 and a 3–6-week shadow: if skills/experience concordance misses the gates, the band widens and the engine degrades gracefully into "cards + more Claude" — cost drifts up toward the plan-capped $3.4/user/month, never past the old baseline, and never below its quality, because widening the band moves decisions *to* Claude, not away from it.
- **R2 — correlated errors and guarantee scope at launch.** One bad card is a systematic error where today's noise is independent; mitigations detect rather than prevent. And the t_lo recall certificate is proven on cascade-survivor labels and extrapolated into the auto-out region until the audit stream accumulates (~weeks). Related honesty: at today's N~10–15, CardRace is **cost parity to ~1.3x more** (card-budget mode) — the purchase at current scale is full-pool recall, a dead backlog queue, zero-latency fresh alerts, and the only curve that survives N=1000.

---

## 5. Migration path

- **Phase 0 — measure (week 0, < $15 total):** purge-survival count of the 57k finals; Claude test-retest on ~500 duplicates; 500 Haiku-vs-Sonnet double-extracts (e_J); measure d, J_pool, dedupe hit rate, and the current advance rate a; join existing cheap signals to the 57k to get the residual sigma. All are counts or free joins.
- **Phase 1 — schema + card lane (weeks 1–2):** `JobCard` (keyed content-hash/`cross_source_slug`, versioned, per-field confidence) + `UserCard` JSON on `UserProfile` in `app/db/models.py`; `app/matching/cards.py` (schemas + mint prompts); card lane cloned from the scoring lane's worker/budget/idempotency pattern; backfill surviving finals' jobs ($114–152).
- **Phase 2 — scorer + calibration (weeks 2–3):** `app/matching/card_match.py` (g(), reusing `door_match.py`'s location tree, `intelligence/work_auth` + `H1BSponsor`, RoleBar gap logic); `scripts/build_calibration.py` -> `data/calibration.json`; `app/matching/conformal.py` (array lookups, no torch).
- **Phase 3 — shadow (weeks 3–6):** `CARD_MATCH_SHADOW=1`: g + bands computed and logged for every job the live cascade scores; the current pipeline stays authoritative. Reuses the `scripts/shadow_report.py` agreement harness (the *harness* — the distilled model it was built for stays rejected). Cutover gates = §3.4 items i–vi.
- **Phase 4 — staged cutover (weeks 6–8):** (a) flip location + work_auth deterministic (near-zero risk — one already is); (b) enable AUTO-OUT stamping only — drains the backlog while Claude still confirms every IN, a zero-quality-risk step; (c) enable AUTO-IN + stopping rule + audit lane; scoring lane becomes the band batcher behind `prewarm_cache`; pulse fast path becomes card-mint + fan-out; adoption's semantic pass (`adoption.py:55-79`) becomes a card-predicate DB copy — the "cheap DB copy" CLAUDE.md always claimed.
- **Phase 5 — polish (week 8+):** facility-location re-rank + matroid caps in `strategy/daily_engine.py`; Hedge re-rank (floors + decay); lazy prose on the existing drawer endpoint; optional pairwise duels for near-tie ordering among certified seats, `DUELS_ENABLED=0` by default.
- **Rollback:** `CARD_MATCH_ENABLED=0` reverts wholesale — the old cascade is kept intact as the fallback lane and the atypical-user path.

## 6. What stays, and why

- **The free gates** — rule filter, ghost detector, embedding floor, door gate — still run first; they are O(cheap) and already drain 80% for $0. The ghost gate gains an audit slice.
- **The Tier-2 Claude path (`reranker.py`) survives verbatim** as the escalation + audit engine: prompt cache and padding, `prewarm_cache`, `_register_final_call` budget counters, circuit breakers, fail-attempt ceilings — all unchanged. `PLAN_LIMITS["finals_daily"]` becomes the escalation budget.
- **Retrieval (BM25 + FAISS + MiniLM)** stays for adoption prefilters, free-text search, and the synonym table — it just stops feeding an LLM queue. The 6-column egress discipline stands.
- **Company caps + displacement, dedupe, dormancy gate, per-user isolation, lane topology, DB/memory/browser discipline, compliance stance (public ATS only)** — all unchanged; every downstream consumer keeps working because CardRace writes the exact `rerank_score`/`rerank_breakdown` shape they already read.
- **Retired from the hot path** (kept in the fallback lane): the Tier-1 prescore, the 120-pair cross-encoder wall, the embedding-gate re-encodes, and per-user FAISS as a scoring dependency — incidentally defusing the unbounded-index issue in ARCHITECTURE.md §7.2.

**Constraint ledger:** (1) no O(users x jobs) LLM — spend is O(J + U + K_opened), proven in §3.5. (2) No trained neural scorer — deterministic named-parameter arithmetic + counting calibration; the one boundary judgment (isotonic fitting) is surfaced for explicit founder sign-off with a hand-set fallback. (3) Quality — every seat is Claude-adjudicated or CP-certified >= 95%, with the residual risk quantified (~0.4 jobs/day, dialable) and the two real risks named. (4) Top-K selection, not pool scoring — stopping rule, seat assembly, portfolio ordering. (5) Shared-pool, CPU-only, compliant — unchanged. (6) The math is shown, every number derivable from CAPACITY.md, the verified code anchors, and the judge-checked briefs.
