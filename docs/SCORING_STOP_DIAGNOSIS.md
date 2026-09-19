# Why a scoring cycle reports `scored: 0` — reading the stop

Written from a live production read of **2026-09-19 UTC** (Railway deployment
logs, single active user `93508136…`). The symptom was
`queued: 17, scored: 0, plan_capped_users: 1` with `llm_budget.exhausted: false`.

**What it was.** The user is on **Free**. They were delivered **26 jobs** against
a `shortlist_daily` of **20** — 130% of the plan's daily promise — and spent the
**120-final cost ceiling** doing it. The budget stopped, correctly. Every cycle
from 09:33 to 22:50 is a Tier-1 drain slice, which by construction never buys a
Tier-2 final, so `scored: 0` is the design working.

**What was wrong.** The health panel reported that user as *"stopped SHORT of
their day's shortlist target"* every 30 minutes for thirteen hours, although
they exceeded their target. Fixed — see "The misreport" below.

**What is still open.** The plan itself was INFERRED from the shape of the
spend, not read: `api.supabase.com` is blocked by this session's egress policy.
`GET /api/admin/budget-diagnostic` was added to close that, and the dashboard
statement timeouts are tracked separately below.

Read `app/matching/finals_budget.py` first; this note only says how the numbers
reach `/api/admin/health` and which of them can disagree without anything
being broken.

## The two budgets are different counters

`/api/admin/health` → `llm_budget.exhausted` is the **platform** backstop:
`LLM_DAILY_FINAL_CAP` = 15,000 finals/day across every user
(`reranker._daily_finals`, `config.py:llm_daily_final_cap`). It exists to stop a
runaway, and with a handful of users it is never the binding constraint.

What actually stops a user is the **per-plan** ceiling,
`PLAN_LIMITS[plan]["finals_daily"]` (Free 120 / Pro 250), counted per user per
UTC day in `user_usage.finals_count`. Nothing links the two.

> `llm_budget.exhausted: false` alongside `plan_capped_users: 1` is not a
> contradiction and never was. The global budget being open says nothing about
> whether *this* user has spent their plan's ceiling.

## `scored` counts Tier-2 finals only

`stats["scored"]` is incremented once per `("scored", ...)` return from
`_score_one` — the authoritative Claude call. It is **not** incremented by:

| outcome | what happened | stat |
|---|---|---|
| `drained` | Tier-1 judged it a misfit, stamped and gone | `drained` |
| `prescored` | Tier-1 kept it, no final bought (drain-only slice) | `drain_prescored` |
| ghost / rule / expiry stamp | left the queue without a scorer call | `drained` |

So a database count of "scored jobs" built on `rerank_score IS NOT NULL` — or
even on `freshness.terminal_verdict_expr()` — is legitimately far larger than
the cycle's `scored`. That function's own docstring records the production case:
78 terminal verdicts against a cycle stat of `scored=0`, of which 75 were Tier-1
drains. Both numbers were right.

## The drain-only signature

When `_finals_allowance` returns `n <= 0`, the user is **not** dropped from the
cycle. `scoring_lane.py:1329` gives them a Tier-1-only slice of up to
`settings.scoring_drain_cap` (**25**) never-prescored items, so the backlog keeps
shrinking at ~$0.0002/item while the finals budget is closed. In that slice
`_score_one` returns only `drained`, `prescored` or `None` — the `drain_only`
guard at `scoring_lane.py:547` makes falling through to Tier-2 impossible.

Therefore:

```
queued: N (N <= 25)   scored: 0   plan_capped_users: 1   drain_prescored: > 0
```

is one capped user receiving a drain slice. It is the design working, not a
stall. The stall it was built to prevent looks different: `queued: 0` with
`plan_capped_users > 0`, which raises the "stopped SHORT" warning at most once
per 30 minutes (`_last_capped_log`).

## The four stops, in evaluation order

`finals_budget.allowance()`, then the wrapper in `scoring_lane._finals_allowance`:

1. **cost ceiling** — `spent >= finals_daily`. → counted in `plan_capped_users`.
2. **yield collapsed** — today's `finals_hits / finals_count` below
   `FINALS_YIELD_CONTINUE_RATE` (2%), and only once `FINALS_YIELD_WINDOW` (50)
   finals have been bought today. Below 50 finals there is **no verdict** and
   this stop cannot fire. → `plan_capped_users`.
3. **delivered >= target** — with `SLATE_CHALLENGE_ENABLED=1` (the default) this
   does *not* return 0; it returns a full slice with the challenger gate
   (`max(70, today's cutoff)`). → counted in **neither** bucket.
4. **Anthropic prescore allowance** — `finals_daily × 2` Tier-1 calls, and only
   Anthropic ones are counted. Binds only while OpenAI is the down provider.
   → `plan_capped_users`, and this is the one reason that gets **no** drain
   slice (that path is itself metered).

## What 2026-09-19 actually looked like

154 scoring cycles, 00:00–22:50 UTC, one active user.

| | |
|---|---|
| Tier-2 finals, scoring lane | 140 (8 before the 03:22 deploy, 132 after) |
| Tier-2 finals, matching lane | 29 (`Cascade Tier-1: N advanced to Claude`) |
| Tier-2 finals, pulse lane | 0 |
| Jobs delivered | **26** (scoring 22, matching 4, pulse 0) |
| Finals per delivered job | **~6.5** — in line with the design's measured average |
| Last final bought | 09:33:07 |
| Cycles reporting `plan_capped_users: 1` | every cycle from 09:33 to 22:50 |
| `target_met_users` | **0, all day** |
| Jobs aged out of the queue unscored | 195 |
| New postings ingested after the stop | 1,997 (pulse lane, 12:40–22:54) |

## The misreport — fixed

`allowance()` tests the cost ceiling **before** the delivered branch, so this
user's reason string is

    daily cost ceiling (140/120 finals) at 26/20 delivered

The classifier asked whether that sentence began with `delivered`. It does not,
so a user who received 130% of their plan was filed under `plan_capped_users`
and warned about as having "stopped SHORT of their day's shortlist target" —
every 30 minutes, for thirteen hours.

`target_met_users` could not compensate: the only reason string that opens with
`delivered` is the one `allowance()` returns when `SLATE_CHALLENGE_ENABLED` is
**off**. With it on — the default — that branch is unreachable, so the counter
was absent in all 154 cycles.

`allowance()` now answers "did this user get their day's jobs?" where both
numbers are already loaded, and carries it on the `Allowance` as `target_met`.
`_run_scoring_cycle` reads that flag instead of the first word of a sentence, so
a stop at or past the day's target counts as `target_met_users` however the
reason is worded. The flag defaults to **False**, so an `Allowance` built
without it still reads as "we do not know that they got their jobs" and the
warning still fires — not knowing is never evidence that the day went well.

Answering it inside `allowance()` rather than in the lane also keeps it free:
deriving it in the loop cost an uncached `user_subscription` SELECT per capped
user per cycle, because `_plan_budget` does not cache.

The cycle also records `plan_capped_reasons`, naming which stop each capped
user hit, and the 30-minute warning names it too.

## How firm is "Free"? — inferred, not read

**This is an inference, and it is the weakest claim in this note.** The
subscription row could not be read: `api.supabase.com` is blocked by the
session's egress policy. What the day's numbers support:

* a cap DID fire, from 09:33 onward;
* the yield stop is ruled out — ~26 hits over ~169 finals is ~15%, far above
  `FINALS_YIELD_CONTINUE_RATE` (2%);
* the Anthropic prescore allowance is ruled out — that reason suppresses the
  drain slice, and drains ran in every capped cycle;
* so the stop is the cost ceiling, and the ledger must therefore have reached
  it. ~169 finals reaches Free's 120. It does not reach Pro's 250.

Under Pro, with 169 finals and 26 delivered against 250/35, `allowance()` would
have returned a positive `n` and no cap would have fired at all. Free is the
only reading consistent with the observed behaviour.

What would break that inference is a ledger materially higher than the
log-derived 169 — see the reconciliation below. And note the input most likely
to be read wrong from outside: with `PLAN_GRANDFATHER_UNTIL` **unset**,
`_is_grandfathered` returns True for *every* user with a profile, so an account
with no subscription row resolves to **PRO**, not Free. Pre-revenue
(`stripe_enabled()` false) everyone is PRO regardless of any row.

`GET /api/admin/budget-diagnostic` now reports all four inputs to
`_get_user_plan`, so this stops being a deduction.

## Reconciling the logs with the ledger

The authoritative counter is `user_usage.finals_count` — one row per user per
UTC day, incremented by `record_final()` the moment a Tier-2 call returns.
Nothing in this note read it. Everything above is reconstructed from lane logs,
so the two can differ in three known ways:

| | |
|---|---|
| **Charged but not counted** | `_register_final_call` fires BEFORE the response is parsed, so a call that returns unparseable JSON charges the ledger and never appears as `by_claude`. Bounded for 09-19: zero parse failures and zero `overloaded` lines in the day's logs, so this term is ~0. |
| **Counted per lane, charged per user** | All three lanes charge the SAME ledger (`Reranker._user_id` comes from `profile.user_id`, so the matching lane's finals are charged too). The scoring lane's `by_claude` is only its own share — 140 of ~169. Reading the cycle stat as the day's spend understates it by whatever the matching lane bought. |
| **Retry double-charge** | The backend loop charges once per *successful API call*, so a response that parses on the second backend charges twice. A 429 or 500 charges nothing (`_register_final_call` runs after `_note_provider_ok`). |

Log-derived total: **169** (scoring 140 + matching 29 + pulse 0). Treat it as a
lower bound on `finals_count`, tight for this day.

## Why the ceiling was overshot by ~49

The ledger ended ~169 against a ceiling of 120 — 41% over. That is structural,
not a leak:

**The budget bounds slice ASSEMBLY, not individual calls.** Each lane computes
`n = min(per_cycle_cap, ceiling − spent)` once, when it builds its work list,
and then spends that whole slice without re-checking. Three lanes do this
independently against the same ledger:

| lane | its slice cap |
|---|---|
| scoring lane | `scoring_per_user_cap` = 40 |
| matching lane | `min(llm_rerank_cap, allow.n)`, `llm_rerank_cap` = 100 |
| pulse lane | its per-tick score budget |

So the worst-case overshoot is the sum of the slices in flight when the ceiling
is crossed — up to ~140 for the scoring and matching lanes alone. 49 is
comfortably inside that, and is what a scoring slice plus a matching slice
assembled just before the crossing look like.

Two smaller contributors, both by design:

* `_day_cache` holds `day_counts` for 30 s, so a lane can size its slice
  against a reading up to 30 s stale. Within one process the cache is
  incremented in place, so it does not drift further than that.
* Workers inside the scoring lane run concurrently, but the slice bounds the
  number of ITEMS, so worker count does not add overshoot on its own. It does
  mean the slice is spent in a burst rather than spread out, which makes the
  cross-lane race above more likely to land inside one window.

None of this is worth a per-call check: the ceiling is a daily cost guard, and
a 41% overshoot on a $0.0025 final is cents. It IS worth knowing when reading
the numbers back, which is why it is written down here.

## Free's ceiling is tight against its own target — on average

At the measured **average** of 6.5 finals per delivered job, Free's
`shortlist_daily` of 20 costs ~130 finals against a `finals_daily` of 120.

**6.5 is an average, not a guarantee.** It is a ratio measured over a
particular mix of supply; a day whose queue is richer converts at a better rate
and can deliver 20 inside 120 finals, and a day that is 73% ineligible has
measured 15.6 (CLAUDE.md records exactly that). So the correct statement is
that Free's ceiling is *tight against* its target and will bind first on an
average day — not that Free can never reach 20.

Pro has more headroom by the same arithmetic: 35 × 6.5 = 228 against 250.

## Challenge mode on Free: unavailable after exhaustion, not impossible

Challenge mode is reachable whenever the slate is full AND the ceiling still
has room — `allowance()` tests the ceiling first, so it is the *exhausted
budget* that closes the door, not the plan.

On 09-19 the window existed and was narrow: delivery crossed 20 at ~08:48 and
the last final was bought at 09:33, so for roughly 45 minutes a challenger
could in principle have been bought. `target_met_users` was 0 in all 154
cycles only because that counter is written from the reason PREFIX, which the
ceiling stop does not carry — not because the mode was never reachable.

The real consequence stands: once the ceiling is spent, nothing further can be
looked at, and on 09-19 that was 1,997 postings ingested after 12:40 with no
possibility of any of them displacing a weaker entry. On Free that window
closes early because the ceiling is tight; it is a sizing question, not an
impossibility.

## Counts that differ on purpose

`finals_budget.delivered_today()` is `count(*)` over today's non-`email_import`
applications, with **no status filter**. `slate.todays_entries()` — which
`slate.place()` uses for capacity and `slate.cutoff()` for the challenger gate —
takes the same rows and drops the ones the slate itself displaced
(`status == SKIPPED` carrying `slate_replaced`). So:

```
delivered_today  ==  jobs on the board  +  today's displacements
```

The budget's target check therefore reads high by the number of displacements.
It is benign for the stop logic — a displacement can only happen once the board
is already full — but it is a real reconciliation term when comparing the
budget's `delivered` against what the dashboard shows.

Overflow (`SLATE_OVERFLOW_DAILY`, 5/day) moves the same number the other way:
those rows are on the board *and* counted, pushing the day past `target`.

`scripts/diagnose_budget_stop.sql` computes every number above, read-only.

## Answering this next time

```
GET /api/admin/budget-diagnostic?user_id=<uid>      # admin only
```

Returns, for one account, from the SAME functions the lane calls — so its
numbers cannot disagree with the ones the budget acted on:

| block | answers |
|---|---|
| `plan` | `effective`, plus every input to `_get_user_plan`: `stripe_enabled`, `has_subscription_row`, `row_plan`, `current_period_end`, `entitlement_expired`, `is_paid_entitlement`, `grandfathered`, `grandfather_cutoff_set` |
| `limits` | `shortlist_daily`, `finals_daily`, `tailor_daily` |
| `today` | `delivered`, `finals_charged` (the ledger), `finals_hits`, `hit_rate`, `finals_per_delivered`, `yield_has_verdict` |
| `allowance` | `n`, `gate`, the exact `reason`, `target_met`, `stop` (the bucket name), `counts_as` |
| `challenge` | `enabled`, `slate_full`, `budget_remaining`, `cutoff`, `available` |

Redaction: no key, token, Stripe id or email; the Stripe ids are booleans and
the account is a short fingerprint, not a full id. A section that could not be
read reports `null` or an `error` type — never a confident `false`, which is
the mistake that produced the guessed plan in the first place. Guard:
`test_admin_observability`.

`scripts/diagnose_budget_stop.sql` covers the same ground straight from the
database, for when the app is unreachable but Postgres is not, plus the
db-vs-dashboard reconciliation the route does not do.

## Tracked separately: the dashboard statement timeouts

Unrelated to the budget, found in the same logs. **Eight** on 2026-09-19:

```
03:41:29  03:42:28  04:11:41          (deploy at 03:22 — cold)
22:00:19  22:35:48  22:35:49  22:35:55  22:35:55
```

All the same line: `dashboard read exceeded its 5000ms budget — panel
degraded: (psycopg2.errors.QueryCanceled) canceling statement due to statement
timeout`.

**The degradation itself worked as designed.** `_BoundedReads` caught each one,
expunged before rolling back (so panels loaded earlier kept their rows), re-armed
and returned the caller's default. A timed-out count reads `None`/`degraded:
true`, never 0, and is never cached. The page rendered.

**What could not be determined: which panel.** `_BoundedReads.get(default, fn)`
takes no label, so all 23 call sites across five routes (`/dashboard`,
`/api/jobs`, `/api/pipeline/live`, `/api/freshness-stats`, `/api/admin/health`)
emit an identical warning. This is the same defect class this note's main
finding is about — the count is recorded, the identity is not.

The pulse lane is **not** the 2026-09-16 pressure pattern repeating: over 501
ticks the deferral rate was 0.3%, `board_cap` had recovered to its 300 maximum,
and one tick in 501 hit the consumer deadline. It is still steady write
pressure (300 boards/tick at roughly a tick a minute, 1,997 new postings), and
the 22:00–22:35 cluster overlaps a matching-lane pass and several pulse ticks,
but nothing in the logs distinguishes a slow panel from a busy database.

Smallest next step, deliberately NOT in this change: give `_BoundedReads.get` a
`label` and put it in the warning. Until then the timeouts can be counted but
not attributed.
