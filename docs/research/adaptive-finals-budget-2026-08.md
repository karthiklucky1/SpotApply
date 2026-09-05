# Adaptive finals budget — spend on evidence, not on a counter

*Design spec. Replaces the flat per-plan `finals_daily` cap with a soft/burst/weekly money
budget plus a quality-driven stop rule. Written against the current code; every file
reference is a real call site.*

> **Status: SUPERSEDED 2026-09-05 — historical record.** The soft/burst/**weekly**
> structure this specifies, and the release curves added on top of it, are gone. They
> paced a PRO user to 1.77 finals/hour (measured prescore→final p50 685 min) and on
> 09-03 the weekly curve, applied to a week whose spend had already happened, stopped
> production's finals for 39 hours while reporting itself healthy. The budget now aims
> at a DELIVERY target — score flat out until the plan's `shortlist_daily` jobs are on
> the board — with `finals_daily` demoted to a pure cost ceiling and no window longer
> than a day. What SURVIVES from this spec and is still live: the persisted ledger
> (§6b), the drain-gate/spend-gate separation (§6a), the marginal-yield stop, and
> promise-ordering in `_user_queue` (now global, in SQL). Read
> `app/matching/finals_budget.py` for the current policy; keep this for the reasoning
> behind the parts that stayed.

> **Status when written: SHIPPED.** Implemented in `app/matching/finals_budget.py` with the ledger on
> `UserUsage.finals_count/finals_hits`, the policy read by
> `scoring_lane._finals_allowance` (slice **and** Tier-1 gate), and promise-ordering in
> `_user_queue`. Guard tests: `tests/test_adaptive_finals_budget.py`. Two things changed
> during the build and are worth knowing: the drain gate and the spend gate had to be
> **separated** (§6a), and the ledger increment had to become atomic SQL (§6b).
>
> **Amendment 2026-09-03 — pacing.** The three numbers bound HOW MUCH; nothing bounded
> WHEN, and production answered "all of it in the first hour": per-hour "Scoring cycle"
> stats for 2026-09-01→03 show 176 finals (09-02) and 87 (09-03) in the 00:xx UTC hour
> and **zero** in the other 23 hours of each day — every posting from the US working day
> waited for the next midnight, and the founder account then hit the weekly 350 on
> Wednesday (`weekly budget spent`, nothing Thu–Sun). `finals_budget.allowance` now reads
> every budget through a release curve: `day_fraction` (15% head start at 00:00, linear to
> 24:00; `FINALS_PACE_HEAD_START`) and `week_fraction` (one day's worth at Monday 00:00,
> linear to Sunday). Unspent release carries within the period; at 24:00 / Sunday night
> the curves read the full budget, so the money is unchanged and §3 still holds. Burst is
> therefore limited to money the week has already released. `FINALS_YIELD_CONTINUE_RATE`
> moved 0.20 → 0.10 in lockstep with the 70 bar (§4's 0.20 was written at a 60 bar).
> Guard tests: the "pacing" section of `tests/test_adaptive_finals_budget.py`.

## 1. What is wrong with the flat cap

`PLAN_LIMITS["finals_daily"] = 50` is a fixed **result** limit enforced at
`scoring_lane._remaining_finals_today` (:462) and `pulse_lane.py:164`. It is wrong in both
directions on the same day:

- **Strong Monday:** 100 promising candidates, the 50th final is still returning 70+ fits —
  we stop anyway, and the user never sees jobs we already knew were good.
- **Quiet Saturday:** 20 candidates, all weak — we happily spend 50 finals proving it.

And because the queue is ordered **freshest-first** (`_user_queue`, `:257`), the 50 finals
are not even spent on the 50 most promising jobs. That is the deeper bug: we pay the
expensive model in arrival order.

## 2. The rule

> Spend Claude only while the evidence says the next candidate is worth it. Stop when the
> remaining pool is weak — not when a counter is full, and never to reach a target.

Three numbers, three different jobs:

| Number | Value (PRO) | What it means |
|---|---|---|
| `finals_soft_daily` | **50** | normal spending point — free to spend, no justification needed |
| `finals_burst_daily` | **100** | absolute ceiling for one UTC day — never exceeded |
| `finals_weekly` | **350** | rolling 7-day ceiling — the real economic control |
| `target_good_matches` | 20 | a **goal for reporting only**. Never a floor, never triggers a call |

Between soft and burst, every additional final must be *earned* by the stop rule in §4.

## 3. Why this costs the same as today

```
weekly 350 ÷ 7 = 50/day average = exactly today's flat cap
350 × 4.33 weeks × $0.0033 = $5.00/user/month   (identical to today)
```

The weekly budget is the economic control; the daily soft/burst pair only decides **where
inside the week** the money goes. Monday spends 100, Saturday spends 20, the month costs
what it costs now. This design adds **zero** new cost exposure — it reallocates.

FREE tier: soft 15 / burst 30 / weekly 105 → $1.50/month, same ratio.

## 4. The stop rule — two tests, cheapest first

**Test A — promise floor (free, runs before any Claude call).**
Every queued job already carries a Tier-1 `prescore` (0–100, `Job.prescore`) and, where
minted, a deterministic CardRace `g()` score (`matching/card_match.py`). If the best
remaining candidate's promise is below `promise_floor` (advance gate + margin), **stop**:
no amount of Claude spend turns a 25-prescore job into a match. This alone implements
"remaining candidates are weak → STOP" for $0.

**Test B — marginal yield (after each batch of `yield_window` = 10 finals).**
```
hit = final score >= shortlist_score_threshold (60)

hit_rate(last 10 finals) >= yield_continue_rate (0.20)  → keep spending, up to burst
hit_rate(last 10 finals) <  yield_stop_rate     (0.10)  → stop for the day
in between                                              → spend to the soft cap, then stop
```
Only jobs **past the soft cap** have to pass Test B. Below the soft cap we spend freely —
that is what "normal spending point" means.

Worked, using the user's own examples:

```
Monday    100 candidates, promise-ordered
          finals 1-50   → 20 hits          (hit rate 0.40, above continue)
          finals 51-60  → 5 hits           (0.50 → continue)
          finals 61-70  → 4 hits           (0.40 → continue)
          finals 71-80  → 3 hits           (0.30 → continue)
          finals 81-90  → 0 hits           (0.00 → STOP)
          spent 90 of 100 burst, 32 excellent jobs

Saturday  20 candidates, best remaining promise = 31 < promise_floor
          finals 1-20   → 5 hits, then Test A fires
          spent 20, 5 excellent jobs, STOP — no chasing 20
```

## 5. Ordering: promise-first inside a freshness window

`_user_queue` (`scoring_lane.py:241-259`) must return **the most promising** jobs, not the
newest. Freshness stays as a *filter*, not the sort key:

```sql
WHERE rerank_score IS NULL AND is_closed = false
  AND first_seen >= now() - freshness_window        -- product promise preserved
ORDER BY COALESCE(prescore, 100) DESC, first_seen DESC
```
`COALESCE(prescore, 100)` puts never-prescored jobs first — they are unknown, and finding
out costs $0.0002. Once prescored they sort into their true place. This is also what makes
the 5-day expiry optional (§7).

## 6. What has to change in the code

| # | File | Change |
|---|---|---|
| 1 | `app/db/models.py` | `UserUsage` already has `(user_id, usage_date, week_start)` with a unique constraint — add `finals_count`, `finals_hits`, `prescore_anthropic_count`. **No new table**, so the account-deletion guard already covers it. |
| 2 | `app/matching/reranker.py` | `_register_final_call` writes through to the ledger and records hit/miss; `user_finals_today` reads it. **In-memory counters cannot hold a weekly budget** — today every deploy resets the day (which is why the stall "healed" on restart). |
| 3 | `app/strategy/scoring_lane.py` | `_remaining_finals_today` → `_finals_allowance(uid) -> (n, reason)` implementing soft/burst/weekly + Tests A & B. `_user_queue` gets the promise ordering from §5. |
| 4 | `app/strategy/pulse_lane.py` | :164 already calls the same function — inherits the behaviour, no change. |
| 5 | `app/config.py` | `FINALS_BURST_MULTIPLIER`, `FINALS_WEEKLY_MULTIPLIER`, `YIELD_WINDOW`, `YIELD_CONTINUE_RATE`, `YIELD_STOP_RATE`, `PROMISE_FLOOR`. `PLAN_LIMITS["finals_daily"]` becomes the **soft** number. |
| 6 | `app/analytics/` | log `finals`, `hits`, `$/hit` per user per day — the stop-rule constants get tuned from this, not from guesses. |
| 7 | `tests/` | guard test: burst is never exceeded; weekly is never exceeded across a restart; a weak pool stops early; a strong pool goes past soft. |

Item 2 is the only real engineering; the rest is arithmetic and a sort key.

### 6a. Two gates, not one — found while building

Raising the Tier-1 gate in the burst zone (Test A) is not enough on its own, because a job
below the gate is **stamped and drained out of the queue for good**. With one gate, a job
prescoring 45 would be permanently written off at 15:00 (burst, gate 55) and given a full
Claude look at 09:00 (soft, gate 40) — the same job, a different fate, decided by what time
it happened to be picked up.

So `_Ctx` carries two thresholds. `gate` (40) is the **drain** bar and never moves.
`spend_gate` is the **buy a final now** bar and rises to 55 in the burst zone. Between
them a job is neither drained nor scored: it stays Queued, its prescore memoized so the
wait costs nothing, and the next cycle inside the soft budget pays for it with everyday
money.

### 6b. The ledger increment must be atomic

Twenty scoring workers finish finals concurrently. A read-modify-write on the day row
loses increments under exactly that concurrency, and a lost increment on a money counter
means **overspend** — the one direction this must not fail in. The write is a single
`UPDATE … SET finals_count = COALESCE(finals_count, 0) + :n` with an INSERT only when no
row was updated.

## 7. Two open items from the previous arc

**OpenAI prescore cap.** Already removed (commit `6a4f7dd`, local — not pushed). At
$0.0002 a call this is what makes §4 Test A affordable: prescoring the whole pool costs
~$0.02/day and buys the ordering that saves Claude calls. Say the word if you want it put
back instead.

**5-day expiry (`SCORING_MAX_JOB_AGE_DAYS=5`).** Under this design it stops being
load-bearing. It exists today because the queue is freshness-ordered and undifferentiated,
so an outage backlog competes with fresh jobs for finals. With promise-ordering (§5) a
stale weak job never reaches Claude — it takes a $0.0002 prescore stamp and drains. The
expiry can be turned off (0) or widened without the debt problem it was written for. That
is a change in what the setting *protects against*, so it should be flipped deliberately,
not as a side effect.

## 8. What this does not do

- It does not guarantee 20 matches. If the pool holds 6, the user gets 6.
- It does not make the feed unbounded — burst and weekly are hard ceilings, checked before
  every call.
- It does not touch tailoring, which remains the larger uncapped exposure
  (`TAILOR_ABUSE_DAILY_CAP=150` on Sonnet ≈ $3–4.50/day for one user).
