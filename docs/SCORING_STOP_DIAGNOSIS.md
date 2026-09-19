# Why a scoring cycle reports `scored: 0` — reading the stop

Written from a live production read of **2026-09-19 UTC** (Railway deployment
logs, single active user `93508136…`). The symptom was
`queued: 17, scored: 0, plan_capped_users: 1` with `llm_budget.exhausted: false`.

**What it was.** The user is on **Free**. They were delivered **26 jobs** against
a `shortlist_daily` of **20** — 130% of the plan's daily promise — and spent the
**120-final cost ceiling** doing it. The budget stopped, correctly. Every cycle
from 09:33 to 22:50 is a Tier-1 drain slice, which by construction never buys a
Tier-2 final, so `scored: 0` is the design working.

**What is wrong.** The health panel reported that user as *"stopped SHORT of
their day's shortlist target"* every 30 minutes for thirteen hours, although
they exceeded their target. See "The misreport" below.

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
| Finals per delivered job | **6.5** — exactly the design's measured rate |
| Last final bought | 09:33:07 |
| Cycles reporting `plan_capped_users: 1` | every cycle from 09:33 to 22:50 |
| `target_met_users` | **0, all day** |
| Jobs aged out of the queue unscored | 195 |
| New postings ingested after the stop | 1,997 (pulse lane, 12:40–22:54) |

The plan is **Free**, by elimination: Pro's ceiling (250) and target (35) were
both out of reach of the day's numbers, so a Pro user would have been in fill
mode with `n > 0` and never counted as capped. Free's ceiling (120) was crossed
and Free's target (20) was exceeded.

Efficiency was not the problem — 6.5 finals per delivered job is exactly the
number `finals_budget.py` is sized against.

## The misreport

`allowance()` tests the cost ceiling **before** the delivered branch, so this
user's reason string is

    daily cost ceiling (140/120 finals) at 26/20 delivered

Only a reason starting with `delivered` is counted as success
(`scoring_lane.py:1316`). This one starts with `daily cost ceiling`, so a user
who received 130% of their plan is filed under `plan_capped_users` and warned
about as having "stopped SHORT of their day's shortlist target".

`target_met_users` cannot compensate, because the only reason string starting
with `delivered` is the one `allowance()` returns when
`SLATE_CHALLENGE_ENABLED` is **off**. With it on — the default — that branch is
unreachable and the counter is always absent. It was 0 in all 154 cycles.

## Free cannot fund its own target

At the measured 6.5 finals per delivered job, Free's `shortlist_daily` of 20
costs **~130 finals**. Free's `finals_daily` is **120**. The ceiling is below
what the target costs, so for a Free user it binds first essentially every day,
and the ceiling-stop path is the one that always fires.

Two consequences, neither of them a bug in the spend logic:

* every Free user's normal, successful day ends on a "stopped SHORT" warning;
* **challenge mode never runs for a Free user**. It lives in the
  `delivered >= target` branch, which is evaluated after the ceiling check, so
  the ceiling short-circuits it. The "a 15:12 posting worth 92 waits behind
  thirty-five jobs scoring 71-73" case that `SLATE_CHALLENGE_ENABLED` exists to
  fix is still live on Free — today, 1,997 postings arrived after the stop and
  none could be looked at.

Pro is sized correctly by the same arithmetic (35 × 6.5 = 228 < 250).

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
