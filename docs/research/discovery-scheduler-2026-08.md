# Adaptive discovery scheduling — design report

**Status:** research only. Nothing here is deployed, no production config or data was modified.
**Date:** 2026-08-24
**Artifacts:** `scripts/scheduler_sim.py` (read-only simulator, 10 self-test invariants)

---

## 0. Three findings that change the brief

The brief asks for a scheduler that discovers valuable jobs within 15–60 minutes. Before designing
one, three things need saying, because two of them contradict the premise and the third says the
scheduler is not where the problem lives.

**(1) The 15-minute target is not supported by evidence. The value half-life is ~2 days, not
minutes.** The best empirical anchor is Davis & Samaniego de la Parra, *Application Flows* (NBER WP
32320), on 125M Dice applications in technical roles: ~41% of a posting's applications arrive in the
first 48h, ~56–60% within 96h. Combined with the measured posting-duration distribution (mean 9.4
days), the composite value of discovering a job at age *a* is well approximated by `V(a) ≈
exp(-a/2.9 days)`. Integrated over a uniform sweep of period *P*, that gives:

| sweep period P | value retained |
|---|---|
| 15 min | 0.998 |
| 1 hour | 0.993 |
| 6 hours | 0.958 |
| **14 hours (today)** | **0.906** |
| 24 hours | 0.846 |

Moving from today's ~14h full-registry sweep to 1 hour is worth **~9 percentage points**. Moving
from 1 hour to 15 minutes is worth **~0.5 points**. The minute-scale urgency folklore ("apply within
10 minutes = 4×") traces to a defunct startup's blog with n=1,610 self-selected users of its own
paid product; LinkedIn's widely-quoted "3×" has no locatable primary source; and the SEO corpus on
this question is mutually contradictory, which is itself diagnostic. **Target hours, not minutes,
and spend the saved effort elsewhere.**

**(2) The bottleneck is not the scheduler. It is 229 milliseconds of wasted CPU per board.**
Measured against SpotApply's own shipped functions (`_strip_html` in `app/discovery/greenhouse.py:23`,
`_board_signature` in `app/strategy/pulse_lane.py:157`):

| 200-job Greenhouse board | payload | serial CPU |
|---|---|---|
| today — `?content=true`, BeautifulSoup every JD | 1,170,632 B | **229.3 ms** |
| light list (drop `content=true`) | 46,124 B | **0.24 ms** |
| conditional GET returning 304 | 0 B | **0 ms** |

That is a **958× difference**, and the production serial budget is 60 s ÷ 236 completed boards =
**254 ms per board**. One 200-job Greenhouse board consumes **90% of the entire per-board budget** —
and every millisecond of it produces a job description that `_board_signature` never reads (it
hashes `external_id` + `title[:80]` only). This single fact explains `deferred_unconsumed`, the
serial-consumer bottleneck, and the ~21% of paid-for fetches that are discarded.

**(3) ~96.6% of all polls find nothing.** So the lever that matters most is not *which* board to poll
but *what an unchanged board costs*. Cheap probes multiply effective capacity; scheduling only
re-allocates it. They compose, and **neither alone reaches an hourly promise**.

---

## 1. The current mathematical bottleneck

### 1.1 One equation reproduces every published number

Let `B` = completed fetches/hour, `N_live` = boards with `job_count > 0`, `N_zero` = zero-yield
boards on cadence `H` hours. When the lane is saturated, EDF over near-uniform deadlines degenerates
to round-robin, so every live board gets an equal share and:

```
effective_revisit  =  N_live / (B - N_zero/H)
```

| zero-yield cadence | zero demand | live capacity | revisit | published |
|---|---|---|---|---|
| 24 h (before) | 1,300.8/h | 2,473.2/h | **8.68 h** | 8.7 h ✓ |
| 72 h (now) | 433.6/h | 3,340.4/h | **6.43 h** | 6.4 h ✓ |
| never poll zero-yield | 0/h | 3,774/h | **5.69 h** | 5.7 h ✓ |

All three match. The model is right, and it says something important: **even deleting all
zero-yield polling leaves a 5.7-hour revisit against a 1-hour promise.** The zero-yield cadence was
never the binding constraint.

### 1.2 The policy is infeasible by 5.8×

```
demand = N_live × (60/floor_min) + N_fast × (60/fast_min) + N_zero × (1/H)
       = 21,479 × 1  +  ~0 × 12  +  31,219/72
       = 21,910 polls/hour     against a capacity of 3,774
```

**Ratio 5.81×.** This is the real diagnosis, and it is not a tuning error — it is a category error.
Under infeasibility EDF cannot express intent: every board is overdue, so "earliest deadline first"
becomes "most overdue first", which is round-robin. **The 5-minute fast lane does not exist in
production.** It is a number written into `next_poll_at` that the server never honours.

A useful corollary: the fast lane is only real while `N_fast × 12 < B`, i.e. **fewer than ~314
boards**. Past that, fast-lane boards starve each other.

### 1.3 Why the observed latency is worse than 6.4h

For periodic polling at interval `T` with Poisson arrivals, discovery delay is **Uniform(0, T)**:

```
median = T/2      p90 = 0.90 T      p95 = 0.95 T
```

So a 6.4h revisit implies a ~3.2h median — but production measures a **91.5h median detection lag**.
The gap is not scheduling. It is (a) newly-registered boards dumping their entire existing backlog,
all of which looks "old" at first sight, and (b) unreliable `posted_at`. 36.7% of postings are
already >7 days old when first seen. **Do not treat 91.5h as a scheduler metric** — it is dominated
by intake composition, and fixing the scheduler will barely move it. Section 8 defines the metric
that *should* be tracked instead.

### 1.4 A reconciliation the numbers demanded

Three independent research strands flagged that 236 completed/tick at a 60 s tick implies
14,160/hour, not 3,774. The repo resolves it: commit `ad4d311` records that the loop slept a full
`pulse_tick_seconds` *after* the body returned, so the real period was body + interval ≈ 197 + 60 =
257 s → 14 ticks/h × 236 = **3,306/h ≈ the measured 3,774**.

**So 3,774/h is a pre-fix number.** The pacing fix alone should lift capacity to ~4,300/h
(period → ~197 s), and if the body is brought under the 150 s cap, ~5,660/h. Re-measure before
sizing anything: `C_max` = completed-**and-consumed** boards/second over 24h is the single budget
constant every formula below depends on.

---

## 2. The best scheduling model, and why

### 2.1 What the objective actually is

Not freshness of a corpus. **Update capture**: each new posting is a discrete item with a value that
decays. The right objective is freshness-weighted expected discovery value per unit cost:

```
maximise   Σ_i  μ_i · λ_i · E[ V(delay_i) ]        subject to   Σ_i 1/T_i ≤ B
```

where `λ_i` = relevant postings/day on board *i*, `μ_i` = value weight (users served × relevance),
`V` = the decay from §0, `T_i` = poll interval.

Because `V` is nearly flat over the operationally reachable range (0.993 at 1h, 0.958 at 6h), the
freshness term barely discriminates between boards. **The dominant term is μ_i — user demand — not
freshness.** That is the single most useful consequence of the evidence review.

### 2.2 The convergence result

Linearising `V` for `T ≪ τ` gives cost `Σ μ_i λ_i T_i / 2`, and the Lagrangian optimum is
`f_i ∝ √(μ_i λ_i)` — the square-root allocation. Independently:

- **Binary freshness** (Azar, Horvitz, Lubetzky, Peres & Shahaf, *PNAS* 2018; restated as SIGIR 2019
  Eq. 3): `ρ_i = max(0, √(μ_i Δ_i/λ) − Δ_i)`
- **Harmonic staleness** (Kolobov, Peres, Lu & Horvitz, *NeurIPS* 2019, Prop. 2/Eq. 5):
  `ρ_w = (−Δ_w + √(Δ_w² + 4 μ_w Δ_w/λ)) / 2`

In SpotApply's regime (poll rate ≫ change rate) **all three reduce to `ρ ∝ √(μλ)`**, verified
numerically to within 1.5%. Three different objectives agreeing is strong justification.

Only the *age* metric differs (`f ∝ λ^{1/3}`), and it is the wrong objective — it measures "how
stale is this board" and cannot distinguish one pending posting from ten.

### 2.3 The trap: do not port the famous result

Cho & Garcia-Molina's celebrated triage — *abandon the fastest changers* — is a theorem about
**binary freshness**, a bounded loss where being stale once costs the same as being stale ten times.
Under **update capture** the two objectives produce opposite policies on fast-changing sources.
Verified numerically:

| μ | Δ/day | binary ρ | harmonic ρ |
|---|---|---|---|
| 1.0 | 0.30 | 0.248 | 0.418 |
| 1.0 | 0.01 | 0.090 | 0.095 |
| **1.0** | **5.00** | **0.000 ← starves** | **0.854** |

The binary form sets `ρ = 0` at the **high-Δ end** — exactly the big Greenhouse boards posting ten
roles a week. NeurIPS 2019 says so explicitly: *"for many sources the optimal ρ* is 0 … this is
unacceptable in practice."* **Use the harmonic form.** Its anti-starvation property is structural
(the concave, unbounded-in-*n* penalty makes `J = ∞` if `ρ_w = 0`), not a bolted-on floor.

### 2.4 What was rejected, and why

| Approach | Verdict |
|---|---|
| **Whittle index / restless bandits** | **No.** Published Whittle-vs-myopic gap is 9.7–14.6% at best, ~0.4% from optimum, and *negative* in at least one model. Mapped to Age-of-Information, the Whittle index `W(a) = μλ·a(a+1)/2` and the myopic index `G(a) = μλ·a` give **identical ordering** for equal `μλ`. It buys nothing here. Weber–Weiss optimality needs statistically identical arms; ours are 59% permanently empty. Papadimitriou–Tsitsiklis means you could not certify the gap anyway. |
| **Contextual bandits / RL** | **No.** The reward signal (a posting appearing) is observable and cheap to model directly with a conjugate prior. RL would learn a rate estimator the long way round. |
| **HOT/WARM/COOL/COLD state machine** | **Simulated, and it loses** — p50 4.6h vs baseline 3.0h, and it spends 47% of polls on zero-yield boards. Discrete tiers cannot express the continuum, and the tier boundaries become the thing you tune forever. |
| **Per-board time-of-day profiles** | **No.** A board producing ~0.3 postings/day yields ~2 events per 24×7 bin per *year*. Learn one **shared** shape per ATS provider and modulate; do not fit per board. |
| **Nutch-style multiplicative backoff** | **Partly.** Good news: it is the closest production analogue. Bad news: the shipped constants (`inc_rate=0.4`, `dec_rate=0.2`) target a ~60% hit rate — on a 59%-empty population that controller would keep dragging every board *toward* wasted polls. And Nutch's multiplicative *decrease* needs ~25 consecutive change observations to climb from 24h back to 5 min. **StormCrawler's convex rule `I ← (1−d)·I + d·I_min` recovers most of the distance on the first observed change** — which is the correct behaviour for "a dormant company starts hiring", the single event this product exists to catch. |

---

## 3. The scheduling equation

Per board *i*, four float columns and one global scalar.

**State** (folds into the existing single-round-trip `_mark_polled` write — no extra DB calls):

```
A_i, B_i    Gamma-Poisson pseudo-counts (arrivals, exposure-days)
last_poll_at
```

**Rate estimate** — Gamma-Poisson posterior mean with hierarchical shrinkage toward the ATS
provider's pooled rate:

```
Δ̂_i = (α₀ + A_i) / (β₀ + B_i)          α₀ = r_provider · β₀ ,  β₀ ≈ 3 days
```

Decay `A_i, B_i` by `e^{-elapsed/half_life}` (half-life ~21 days) on each write, so the estimate
tracks hiring freezes and spikes without a change-point detector.

**Critically: count postings, not "changed / not changed."** We see the full posting-id list, so we
have *complete* observations and can use `λ̂ = (U+1)/t_U` directly. The interval-censored estimator
`λ̂ = −(1/I)·ln((n−X+0.5)/(n+0.5))` (Cho & Garcia-Molina) is **capped at your own polling
frequency** — an under-polled fast board can never be identified as fast. Counting ids avoids that
saturation entirely.

**Value weight:**

```
μ_i = (1 + w_watch·watchers_i) · Σ_u  relevance(u, board_i)     clipped to [μ_min, μ_max]
```

`relevance` is the existing role/country routing already computed in `_title_matches`. Clipping is
not cosmetic — it is the starvation bound (§9).

**Uncertainty bonus** (exploration; keeps a dormant board from being buried forever):

```
μ_i ← μ_i · (1 + c/√(1 + B_i))          c ≈ 1.5
```

**Cadence** — the harmonic closed form, modulated by the shared provider time-of-day shape:

```
Δ_eff = Δ̂_i · s_provider(hour, dow)                      [only for boards with A_i > 0]
ρ_i   = ( −Δ_eff + √( Δ_eff² + 4·μ_i·Δ_eff / λ ) ) / 2
T_i   = clamp( 1/ρ_i ,  T_floor ,  T_cap_i )
```

**The global shadow price `λ`** is solved, not tuned, by a controller that runs every few minutes:

```
find λ  such that   Σ_i 1/T_i(λ)  =  C_max
```

Monotone in λ, so ~40 bisection steps on a 4,000-board sample settles it in milliseconds. This is
the piece that makes the whole thing self-sizing: **add boards or lose capacity and every cadence
re-derives itself.** No knob to re-tune.

**The anti-starvation cap, tied to uniform:**

```
T_cap_i = 1.5 × (N_live / C_max)        for any board that has ever posted
T_cap_i = 72 h                          for never-yielding boards (never ∞)
```

This is the design's most load-bearing empirical result. A fixed cap is correctly tight when
capacity is scarce and needlessly loose once it is not — which is exactly when concentration starts
costing tail latency for nothing. Tying it to what uniform allocation *would* achieve makes the
policy **converge to uniform as capacity grows**, automatically.

**Time-of-day shaping applies only to boards that have posted.** Cold boards keep flat intervals so
their deadlines spread uniformly and **fill the overnight trough** that shaping the warm boards would
otherwise leave idle. Without this the server measured 23% idle at night.

---

## 4. Five boards over 30 days

Run: `python -m scripts.scheduler_sim --example`. The five archetypes are inserted into a full-size
population so the shadow price they compete against is real. Assigned interval, sampled daily:

```
board                                        d1     d3     d5     d7    d10    d14    d15    d16    d17    d20    d25    d29   polls
highly productive (30/day, 8 users)         3.2h   2.0h    86m    81m    70m    61m    59m    60m    58m    48m    51m    47m   2,823
moderately active (2/day, 4 users)          8.5h   8.6h   8.0h   7.2h   5.8h   4.8h   4.8h   4.9h   4.8h   4.0h   4.2h   3.8h     239
weekly            (2/week, 3 users)         8.5h   8.6h   8.6h   8.6h   8.6h   8.7h   8.7h   8.7h   8.7h   8.7h   8.7h   8.7h      99
zero-yield        (never posts)            27.1h  25.7h  28.1h  27.4h  29.8h  30.6h  31.1h  31.5h  31.5h  31.4h  34.2h  34.4h      22
dormant -> wakes  (silent, 3/day from d15)  12.8h  12.2h  12.4h  12.8h  13.2h  13.6h  13.7h   8.7h   8.7h   5.8h   4.8h   4.0h     141
```

Read across the dormant board at **d14 → d16**: it wakes on day 15 and its cadence collapses from
13.7h to 8.7h on the **first poll that observes a posting**, then to 4.0h as exposure accumulates.
No threshold to cross, no state machine to flip, no manual promotion. The zero-yield board is polled
22 times in 30 days — cheap, but **never forgotten**.

---

## 5. Simulation

`scripts/scheduler_sim.py`, 30-day horizon, 7-day warm-up discarded, all policies replayed against
**identical** generated worlds, all spending the **same** capacity.

```
policy                             polls/d  cap% |     p50    p75    p90    p95 |   <60m  waste  zero%  stale
Baseline (current)                  90,566  100% |    3.0h   4.5h   5.4h   6.1h |  16.2%  96.6%  10.7%   7.7h
A  fixed 1h / 72h                   90,566  100% |    3.1h   4.7h   5.7h   6.4h |  17.1%  96.6%  10.9%   7.4h
B  HOT/WARM/COOL/COLD               90,566  100% |    4.6h   7.4h   9.3h   9.9h |  10.7%  96.7%  47.1%   2.2d
C  sqrt(lambda)                     90,553  100% |    1.9h   3.9h   6.8h  10.0h |  30.6%  96.3%  40.2%  25.1h
D  C + time-of-day                  82,156   91% |    1.8h   5.6h  10.8h  14.8h |  34.6%  95.7%  50.9%  30.7h
E  sqrt(demand*lam)+tod+explore     81,044   89% |     73m   3.2h   7.1h  10.3h |  44.7%  95.7%  45.3%  31.5h
F  E + fixed 8h staleness cap       90,467  100% |    1.9h   3.8h   5.7h   6.8h |  33.3%  96.4%  22.9%   8.9h
G  E + cap = 1.5x uniform           90,156  100% |    1.8h   3.8h   6.0h   7.2h |  34.6%  96.3%  26.5%   8.4h
```

**Policy G: 1.7× faster median, 2.1× more relevant jobs found inside an hour, at identical cost.**

Three things worth reading carefully:

- **The uncapped policies (D, E) leave 9–11% of capacity idle and have 30h tails.** Concentration
  without an anti-starvation bound is not a better scheduler; it is a worse one wearing better
  medians. The cap is what makes the index policy safe.
- **B (the intuitive state machine) is worse than doing nothing.** Discrete tiers spend 47% of polls
  on zero-yield boards.
- **The `waste` column never drops below ~95.7%** under any policy. No scheduler fixes that — only
  cheap probes do.

### 5.1 The two levers, separated

```
scenario                                       fetch/h |     p50    p75    p90    p95 |   <60m
today, full fetch    + baseline cadence          3,774 |    3.0h   4.5h   5.4h   6.1h |  16.2%
today, full fetch    + policy G                  3,774 |    1.8h   3.8h   6.0h   7.2h |  34.6%
conditional at 1/4   + baseline cadence         13,691 |     41m    61m    77m   1.6h |  73.5%
conditional at 1/4   + policy G                 13,691 |     29m    58m   1.6h   1.9h |  76.4%
conditional at 1/10  + baseline cadence         28,855 |     17m    24m    38m    61m |  94.6%
```

`B_eff = B / (changed_share + r·unchanged_share)`. **Cheap probes dominate.** At 1/10 cost even the
*current* cadence hits a 17-minute median and 94.6% inside the hour. Scheduling adds to that; it
cannot substitute for it.

### 5.2 Sensitivity

Swept over tail dispersion σ ∈ {1.0, 1.5, 2.0, 2.5}, posting lifetime ∈ {10, 30, 45} days, relevant
share ∈ {5%, 20%} — 24 configurations, `--sweep`.

**Policy G beats the baseline median in all 24**, by 1.1× (σ=1.0, thin tail — little to concentrate
on) to 4.6× (σ=2.5, heavy tail). The gain rises monotonically with tail dispersion, which is the
expected shape: value-weighted concentration pays exactly in proportion to how concentrated the
value is. G's p95 stays within ~1h of baseline everywhere and is *better* than baseline at σ ≥ 2.0.

The **uncapped** policy E is the cautionary column. Its median is often better than G's, but its p95
is **worse than the baseline in 19 of 24 configurations** — up to 13.4h against a 6.4h baseline. At
σ=1.0 it is worse than doing nothing on the tail while gaining little on the median. That is the
whole argument for the cap: an index policy without an anti-starvation bound buys medians with
tail latency, and the trade is bad whenever the tail is thin.

### 5.3 What this simulation is not

It is a **generative model, not a replay.** A replay needs the per-board poll/arrival series;
production records tick-level aggregates and per-board *current* state, but not that series, and
this environment had no production credentials. Every parameter is either a figure from this repo
(cited in the module docstring) or swept. **The policy ranking is robust; the absolute latencies are
model outputs and should be treated as ranges.**

---

## 6. Architecture

The recommended flow. Note that **no new infrastructure appears** — this is a reordering of the
existing pulse lane.

```
                    ┌─────────────────────────────────────────────┐
                    │ SHADOW-PRICE CONTROLLER  (every ~5 min)     │
                    │ solve λ : Σ 1/T_i(λ) = C_max                │
                    └────────────────────┬────────────────────────┘
                                         │ λ
   CompanyRegistry ───────────────────►  ▼
   (Δ̂, μ, next_poll_at)          ┌──────────────┐
                                 │  SELECT due  │  ORDER BY next_poll_at
                                 │  (unchanged) │  LIMIT n
                                 └──────┬───────┘
                                        ▼
                         ┌──────────────────────────┐
                         │  FETCH POOL              │  shared httpx.Client
                         │  conditional GET         │  keep-alive per host
                         │  If-None-Match / IMS     │
                         └────┬────────────────┬────┘
                    304 ──────┘                └────── 200
                     │                                  │
        ┌────────────▼─────────────┐        ┌───────────▼────────────┐
        │ ZERO WORK                │        │ LIGHT LIST parse       │
        │ record poll, reschedule  │        │ (no descriptions)      │
        │ ~96% of all polls        │        │ id-set signature       │
        └────────────┬─────────────┘        └───────────┬────────────┘
                     │                    unchanged ────┤──── changed
                     │                                  │       │
                     │                                  │       ▼
                     │                                  │  ┌──────────────────┐
                     │                                  │  │ FULL FETCH of    │
                     │                                  │  │ NEW IDS ONLY     │
                     │                                  │  │ (descriptions)   │
                     │                                  │  └────────┬─────────┘
                     │                                  │           ▼
                     │                                  │  ┌──────────────────┐
                     │                                  │  │ upsert + route   │
                     │                                  │  │ + fast-path score│
                     │                                  │  └────────┬─────────┘
                     ▼                                  ▼           ▼
              ┌──────────────────────────────────────────────────────────┐
              │ BATCH WRITER — one bulk UPDATE per N boards              │
              │ writes: last_seen, poll_hash, ETag, A_i, B_i, next_poll  │
              └──────────────────────────────────────────────────────────┘
```

Four changes, in order of value:

1. **Drop `?content=true` from the change-detection path.** Fetch the light list, compute the
   signature, and pull descriptions only for ids you have never seen. Saves 229 ms and 1.1 MB per
   200-job board. This is the whole bottleneck.
2. **Add conditional GET** (`If-None-Match` / `If-Modified-Since`) with the validator stored in
   `CompanyRegistry`. Send both headers — RFC 9110 requires ETag to win when both are present, so it
   is safe. **Pin one URL per board**: an ETag validates one representation, so the light and heavy
   URLs have different ETags.
3. **Share one `httpx.Client` per host.** Every scraper currently calls module-level `httpx.get(...)`,
   so production performs ~3,774 TCP + TLS handshakes per hour against ~14 hosts. A 304 whose TLS
   handshake still costs 2 RTTs has saved bytes but not wall clock.
4. **Priority-order the consumer queue** by `μ_i·Δ̂_i`, so the ~21% of results that get dropped are
   the *least valuable* 21% rather than a random 21%. This is a sort key on an existing list.

**Do not add Kafka/Redis/Celery.** The current architecture reaches the target with (1)–(4). A
separate queue tier becomes justified only when a single container can no longer hold the fetch
concurrency — see §10, roughly 250k boards.

### 6.1 A correction to the unit of account

`pulse_max_boards_per_tick` counts **boards**, but cost varies by two orders of magnitude across
providers. Workday paginates 5 pages × 20 and then issues **one detail GET per posting** — up to 105
HTTP requests for one "board poll", serially, in one worker. SmartRecruiters and JOIN are similar
(JOIN paginates 5 jobs/page). **Budget in requests or worker-seconds, not boards.** A per-provider
cost multiplier belongs in the registry.

### 6.2 Cheap-probe support by provider

Evidence grade in brackets. **None of the 304 behaviour is first-hand** — this session's egress proxy
blocked every ATS host (see §12).

| Provider | 304? | Cheap probe | Est. value |
|---|---|---|---|
| Greenhouse | yes [3 sources] | drop `content=true` (25× smaller, measured here) | **highest** |
| Lever | yes [3 sources] | — (JD always inline) | **highest** (5.97 MB → 0 measured by a third party) |
| Ashby | yes [3 sources, validator disputed] | — | high |
| SmartRecruiters | yes [live test, third party] | `?limit=1` → `totalFound` | high |
| Workday | **no** (POST) | `{limit:1}` → uncapped `total` | high via count-gate |
| JOIN | no | `pagination.total` on page 1 | high (3–9× per board) |
| Personio | yes [1 source] | — | med-high (XML parse skipped) |
| Teamtailor | yes [1 source] | switch `.rss` → `.json` | med |
| Breezy | yes [1 source] | — | med |
| BambooHR | no | `/careers/list` JSON instead of HTML widget | med |
| Rippling / Workable | untested | list already description-free | low / unknown |
| Recruitee | **no — confirmed negative** | id-set hash only | low |

**First action: 10 minutes of `curl -sSD- -o/dev/null` against four boards** converts this table from
"corroborated" to "measured". Do that before building anything on it.

No ATS offers a **public** webhook (all are customer-authenticated). WebSub requires the publisher to
run a hub — none do. IndexNow is publisher→search-engine push with no read API. **Polling with
conditional GET is the correct answer for the general corpus**; webhook-grade latency is a BD motion,
not an engineering one.

---

## 7. Migration plan

Each stage is independently revertible and independently measurable. **Stages 1–2 are worth more
than everything after them.**

| Stage | Change | Risk | Expected |
|---|---|---|---|
| **0** | Re-measure `C_max` = completed-**and-consumed** boards/sec over 24h. Fix the 236/tick vs 3,774/h discrepancy first. | none | the budget constant every later stage depends on |
| **1** | Light list + descriptions only for new ids. Shared `httpx.Client`. | low — pure cost reduction, no policy change | 229 ms → 0.24 ms/board; the consumer stops being the bottleneck |
| **2** | Conditional GET; store validator in registry. Measure the 304 rate per provider. | low — falls back to a 200 | ~96% of polls become near-free |
| **3** | Priority-order the consumer queue by `μ·Δ̂`. ~10 lines. | low | the dropped tail becomes the cheap tail |
| **4** | Write `A_i, B_i` into the existing `_mark_polled` round-trip. **Change no cadence.** Shadow-report what the index policy *would* have chosen vs what the current `_cadence` did. | none — read-only | the evidence to justify stage 5 |
| **5** | Switch `_cadence` to the index formula behind `PULSE_INDEX_ENABLED`, with the shadow-price controller and both clamps. | medium | 1.7× median, 2.1× within-hour |
| **6** | Provider time-of-day shape (shared, not per-board). | low | tail improvement; keep the cold sweep unmodulated |

Stage 4 is the important discipline: **ship the estimator before the policy**, and let it prove
itself in shadow against real traffic before it controls anything. Same pattern as the existing
`CARD_MATCH_SHADOW` work.

---

## 8. Instrumentation

The current metrics cannot prove any of this. Specifically, **`91.5h median detection lag` must not
be the headline** — it is dominated by backlog intake, not scheduling.

**The one metric that matters:**

> For postings on boards **already under schedule for ≥24h** (excluding first-ever polls of newly
> registered boards), the distribution of `first_seen − posted_at` restricted to providers that
> publish a trustworthy date (Greenhouse `first_published`, Lever `createdAt`, Ashby `publishedAt`),
> **weighted by `μ`**.

Report p50/p75/p90/p95 and the **fraction inside 60 minutes**. That last number is the product
promise, and it is the one the simulation predicts moving 16% → 35%.

Also needed:

- `C_max`: completed-and-**consumed** boards/sec, 24h rolling. One number, everything depends on it.
- **Feasibility ratio** `Σ(1/T_i) / C_max`. If this exceeds 1.0 the scheduler is not scheduling. It
  should be on the dashboard permanently, because it silently went to 5.81 and nobody saw it.
- 304 rate and bytes saved, per provider.
- Consumer stage timings (already added in `1976f33`) — keep, and add a *changed-board* split.
- Poll-outcome buckets, extended: a fetch that is **dropped unconsumed must not update `A_i, B_i`**.
  This is the same invariant as `_defer_boards` and for the same reason: a poll you didn't consume
  taught you nothing, and counting it silently biases every rate estimate downward.
- Staleness p99 among boards that have ever posted (the starvation alarm).
- Shadow-mode agreement (stage 4): |current cadence − index cadence| distribution.

---

## 9. Failure modes

| Failure | Mechanism | Protection |
|---|---|---|
| **Value-weight starvation** | `μ` concentrates on a few users' interests; everything else starves | `T_cap = 1.5 × uniform` (hard); `μ` clipped to `[μ_min, μ_max]`; simulated — G's worst live board is 8.4h vs baseline 7.7h |
| **Rich-get-richer** | A board polled rarely accumulates little evidence, so `Δ̂` stays low, so it is polled rarely | Uncertainty bonus `1 + c/√(1+B_i)`; and the cap bounds the loop regardless |
| **Estimator saturation** | Interval-censored estimator is capped at your own poll rate; a fast board polled slowly reads as slow | **Count posting ids** (complete observations), not a changed/unchanged bit |
| **Failure read as "no change"** | A 500 or a timeout looks like an empty board and decays it | A failed fetch must not touch `A_i, B_i` — mirrors Nutch, which routes failures to `setPageRetrySchedule`/`setPageGoneSchedule`, not the adaptive path |
| **304 lies** | A 304 means "body unchanged", not "no new posting" — if the validator covers a different representation | Pin one URL per board; re-validate fully on a schedule (e.g. every 20th poll) |
| **ATS migration** | Board goes 404; company still hiring elsewhere | Existing 404 retirement + registry resolver; alert when a board with `A_i > 0` retires |
| **Hiring freeze / spike** | `Δ̂` stale | Exponential decay on `A_i, B_i` (half-life ~21d) — no change-point detector needed |
| **Controller oscillation** | λ over-corrects, cadences swing | Solve on a sample, rate-limit λ movement (≤20%/step), clamp both ends. Nutch's own javadoc warns rates >0.4 destabilise — a real, documented instability |
| **Deploy/restart** | Schedule state lost | `next_poll_at` is already persisted; `A_i, B_i` persist in the same row. **In-memory counters reset on every deploy** — this repo already learned that with `UserUsage.finals_count` |
| **Thundering herd after restart** | Everything due at once | Existing id-derived jitter; keep it |
| **Duplicate boards** | Same company on two ATSes double-counts `μ` | Existing `cross_source_slug` dedupe; dedupe `μ` at company level |
| **Politeness breach** | Work conservation polls below the floor | Hard `T_floor` per provider — **the simulator hit this exact bug**: a floored high-value board sat at the heap minimum and drew 4× the permitted requests |
| **Long Workday pagination** | One board monopolises a worker | Per-provider cost multiplier + per-fetch timeout; count requests, not boards |

---

## 10. Scaling curve

Assuming stages 1–3 land (`C_max` ≈ 25–30k effective polls/hour on one container):

| Boards | Live (~41%) | Uniform sweep | With policy G | Verdict |
|---|---|---|---|---|
| 52.7k (today) | 21.5k | 43 min | **p50 ~17 min** | comfortable |
| 100k | 41k | 82 min | p50 ~35 min | fine; watch the consumer |
| 250k | 102k | 3.4 h | p50 ~1.4 h | **inflection** — one container's CPU for parsing binds; split fetch from consume, or shard by provider |
| 500k | 205k | 6.8 h | p50 ~2.8 h | needs a real queue tier + horizontal fetchers; the registry write path becomes the constraint |
| 1M | 410k | 13.7 h | p50 ~5.5 h | multi-node; per-provider shards; `μ`-based admission (stop polling boards no user can match) |

The honest scaling statement: **the algorithm scales; the container does not.** The index policy's
cost is `O(n)` per controller solve and `O(log n)` per poll — trivial at 1M. What breaks is parsing
CPU and DB write throughput. And note the *shape*: because `μ`-weighting concentrates spend, the
degradation with N is far gentler than uniform sweep, which is the strongest argument for the policy
at scale — not its behaviour today.

---

## 11. Final recommendation

### Option A — safest
Stages 0–3 only. Light list, conditional GET, shared client, priority-ordered consumer. **No
scheduling change at all.**
→ p50 ~17 min, 94.6% inside the hour *with the current cadence*. Days of work, near-zero risk.

### Option B — recommended
Option A **plus** stages 4–6: the harmonic index policy with a solved shadow price, hierarchical
Gamma-Poisson rates, uncertainty bonus, adaptive anti-starvation cap, shared provider time-of-day
shape. Shipped in shadow first.
→ **p50 13 min, p95 54 min, 96.9% inside the hour** (measured in simulation at the 1/10-cost
operating point), and it **self-sizes** as the registry grows.

### Option C — maximum scale
Option B plus a distributed fetch queue, horizontal fetch workers, per-provider shards, `μ`-based
admission control.
→ Warranted at ~250k boards. **Not now.**

### What I would deploy today: **Option A immediately, then Option B.**

Not because B is unproven — the simulation supports it — but because of the arithmetic. Option A is
worth ~10× effective capacity for a few days of low-risk work and **requires no new concepts**.
Option B is worth a further 1.7×, needs a new estimator, a controller, and two clamps, and its
benefit is *larger once A has landed* only in the sense that it reallocates a bigger budget. Doing B
first would be optimising the allocation of a budget that is 96% wasted.

And one thing I would not do at all: **chase the 15-minute SLA.** The evidence puts it at ~0.5
percentage points of product value over a 1-hour SLA. Option A alone clears the bar that actually
matters.

**Proposed SLAs** — what the capacity can defensibly support after Option A:

| Tier | p50 | p90 | p95 |
|---|---|---|---|
| Watchlist + high-`μ` boards | < 15 min | < 40 min | < 60 min |
| Normal productive boards | < 45 min | < 2 h | < 3 h |
| Long-tail productive | < 4 h | < 10 h | < 12 h |
| Never-yielding | 24–72 h, never retired | — | — |

---

## 12. What I could not verify

Stated plainly, because it bounds how much weight parts of this deserve.

- **No production database access.** No credentials in this environment. Every production figure is
  taken from what is recorded in this repository (commit messages, config comments, code) and is
  cited inline. The capacity model reproducing all three published revisit numbers is the main
  evidence that those figures are mutually consistent.
- **No live ATS measurement.** The session's egress proxy blocks every ATS host (403 on CONNECT) —
  `boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com`, `api.smartrecruiters.com` and the
  rest. I did not route around it. **Every 304/ETag claim in §6.2 is second-hand**, from vendor docs
  or third-party crawler source. The four-line `curl` check is the first thing to run.
- **The CPU benchmark is first-hand** and was verified against SpotApply's own shipped `_strip_html`
  and `_board_signature`, loaded verbatim from source. The payload is synthetic (200 jobs × ~4 KB
  HTML); real JD sizes vary, so treat 229 ms as representative of a large Greenhouse board, not a
  universal constant.
- **The V(age) evidence** rests on Davis & Samaniego de la Parra (NBER WP 32320). The adversarial
  check could not re-fetch nber.org (egress-blocked), so the 41%/56–60% figures are as-reported by
  the research pass, not independently re-verified here. The *direction* (days, not minutes) is
  robust — it is corroborated by the posting-duration distribution and by the absence of any credible
  minute-scale source.
- **Formulas that were verified verbatim from primary PDFs:** the SIGIR 2019 and NeurIPS 2019 closed
  forms, and Nutch's `AdaptiveFetchSchedule` constants (shipped config: `inc_rate=0.4`,
  `dec_rate=0.2`, `min_interval=60s`, `max_interval=365d`, `sync_delta=true/0.3`). Note Nutch is
  **not** a pure MI/MD controller — there is an on-by-default `SYNC_DELTA` term that overrides the
  interval to the observed change age and phase-shifts the next fetch anchor.
- **The simulation is a model, not a replay** (§5.3).
