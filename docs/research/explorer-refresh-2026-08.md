# Why the job list is slow, and why it shouldn't show 50k jobs

*Read from the code (`app/api/server.py:2789-3007`, `app/templates/dashboard.html:2864`,
`app/db/init_db.py:428-470`). No production timings available from this session — the
causes below are structural, visible in the query shapes and the index list.*

## 1. The product question: you're right

The Explorer defaults to the **entire open pool, all time**. `max_age_days` is only sent
when the user clicks the "Last 7 days" toggle (`dashboard.html:2882`), and the tab badge
shows `total_open` — the whole pool — on purpose.

Nobody acts on 50,000 rows. The board is the product; the Explorer is a search tool. It
should default to **last 7 days + my roles**, with "all time" as a deliberate opt-in. That
is a one-line default change *and* it makes every query below cheaper — but only if the
filter is written so an index can serve it (§2.2).

## 2. Why a refresh is slow — four causes, in order of size

### 2.1 It ships the job descriptions to render a table that never shows them

```python
query = select(Job, Application).outerjoin(...)        # server.py:2823
```
Full `Job` rows and full `Application` rows come back, and the response builds **17 scalar
fields** — id, company, title, location, url, scores, reason, status. `description` is not
one of them, yet it travels on every row of every page.

That column is the biggest thing in the table: retention exists specifically to blank it
("the description column was half the disk", commit `52fc150`), and unprojected reads of it
are what put Supabase at **205% of its egress quota on 2 MB of stored data**
(`docs/CAPACITY.md`). At 25 rows/page this is the dominant cost of the request, paid on
every filter keystroke, every page click, every refresh.

The egress guard misses it. `tests/test_architecture_invariants.py` finds bare
`select(Job)` by AST — `len(node.args) == 1`. `select(Job, Application)` has two args, so
the app's single hottest read is the one query the guard cannot see.

### 2.2 The default sort is an expression no index can serve

```python
order_by(desc(func.coalesce(Job.posted_at, Job.first_seen)), desc(Job.id))   # sort=fresh
```
and the age filter is the same expression:
```python
func.coalesce(Job.posted_at, Job.first_seen) >= cutoff
```
The declared indexes (`init_db._PERF_INDEXES`) are `(user_id)`, `(user_id, is_closed)`,
`(user_id, company, title)`, `(user_id, discovered_at)`, `(user_id, first_seen) WHERE
rerank_score IS NULL`, `(user_id, rerank_score DESC) WHERE is_closed = false`. **None of
them covers a COALESCE expression.** So every Explorer page sorts the user's whole filtered
pool to return 25 rows — and turning on "Last 7 days" does not avoid it, because the filter
is the same unindexable expression.

### 2.3 Three aggregate queries per request

Each call runs the page query, a filtered `COUNT` for pagination, and a second unfiltered
`COUNT` for the tab badge (`total_open`, :2935). On a 50k-row pool with the roles filter
active, the filtered count is the expensive one — see §2.4.

### 2.4 The roles filter and search are `ILIKE '%term%'`

`Job.title.ilike(f"%{t}%")` per target-role term (:2875) and
`title/company/location LIKE '%search%'` (:2830). Leading-wildcard matches can never use a
B-tree index, in the page query and in the count. The "My roles" checkbox is **on by
default**, so this runs on every load.

## 3. What to change

| # | Change | Effect |
|---|---|---|
| 1 | Project the columns the response actually returns (`select(Job.id, Job.company, …, Application.id, Application.status, …)`, or `load_only`). Descriptions stop leaving the database. | Biggest win, zero behaviour change |
| 2 | Default the Explorer to **7 days**, and filter/sort on a plain indexed column (`first_seen`, or `discovered_at` which already has `ix_job_user_discovered`) instead of the COALESCE. Keep `posted_at` for display only. | Turns a full sort into an index range scan |
| 3 | Add `(user_id, is_closed, first_seen DESC)` to `_PERF_INDEXES` | Serves filter + sort + pagination together |
| 4 | Drop or cache `total_open`; skip the exact `COUNT` when it exceeds a threshold and show "1,000+" | Removes one or two scans per request |
| 5 | Make "My roles" a server-side stored predicate (or match on an indexed normalized-title column) instead of N `ILIKE '%…%'` | Removes the only remaining full scan |
| 6 | Extend the AST guard to multi-entity selects (`select(Job, X)`), so #1 cannot regress | Locks the fix in |

Items 1–3 are small and independent; they are where the time actually goes.

> **Status: 1–3 shipped.** The list query is projected to the 22 columns the response
> returns (`server.py:_JOB_LIST_COLS`) — compiled SQL confirmed: one FROM, one LEFT OUTER
> JOIN, no `description`. The Explorer opens on the last 7 days
> (`EXPLORER_DEFAULT_FRESH_DAYS`) and auto-loads on first visit instead of sitting on
> "Loading job database…". `ix_job_user_fresh` indexes the COALESCE **expression** itself,
> so the exact posted-else-discovered semantics survive while the filter becomes a range
> scan and the sort comes out of the index. Guarded by `tests/test_jobs_projection.py`.
>
> Items 4–6 are open. While writing the guard, the AST check found **seven more**
> whole-entity `select(Application, Job)` reads — five in `dashboard` (the Kanban render,
> on the same egress path), plus `sync_emails` and `export_applications_csv`. They are
> listed in the test as a debt register that can shrink but not grow.

## 4. "Is deploying many times a problem?"

Not for correctness — but it is not free, and one failure mode is real.

**Safe.** No data loss: unscored jobs stay Queued, the pulse lane's per-board schedule lives
in the database, and per-user finals spend is now persisted in `UserUsage`
(`app/matching/finals_budget.py`) rather than memory.

**Each deploy costs a full discovery pass.** `_scheduler()` sleeps 120s after boot and then
runs immediately (`server.py:1024-1032`) — it does not wait for the 6h interval. Ten
deploys in a day means ten extra global discovery passes (hundreds of board polls and a few
thousand aggregator postings each) on top of the scheduled four.

**Platform-level LLM counters reset.** `LLM_DAILY_FINAL_CAP` / `LLM_HOURLY_FINAL_CAP` live
in process memory (`reranker._daily_finals`). Every deploy zeroes them, so the *platform*
backstop can be exceeded N× on a day with N deploys. Per-user budgets are unaffected — they
are on disk — which is exactly why they were moved there.

**Small re-spend.** The provider circuit breaker (`_provider_down_until`), the per-job
attempt deferrals (`_deferred_until`), and the prescore memo (`_prescore_memo`) are all
process-local. After a deploy a handful of jobs re-pay a $0.0002 prescore and a
cooling-down provider gets re-probed once.

**The one real danger: deploying faster than the lanes start.** Every lane sleeps before its
first tick — scoring 160s, hot lane 180s, scheduler 120s, registry maintenance 300s. Deploy
every two or three minutes and the scoring lane never reaches a single cycle: the queue
stops draining entirely for as long as the deploy burst lasts, with nothing in the logs
saying so. Space deploys at least ~5 minutes apart, and after a burst confirm one scoring
cycle actually ran.
