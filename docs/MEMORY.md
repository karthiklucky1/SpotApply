# Memory & the OOM crash

The production deploy was OOM-killed. This is what is resident in that container,
why it crossed the limit, and the knobs that keep it under.

## Why this container is memory-heavy

SpotApply runs as **one process** — deliberately. Every lane lock, LLM budget
counter, in-flight claim, and rate limiter is process-local, so a second worker
would double scraping, LLM spend, and alerts (see the `WEB_CONCURRENCY` guard in
`server.py`'s startup). The cost of that decision is that everything shares a
single memory limit:

| Resident thing | Rough cost | Notes |
|---|---|---|
| `torch` (CPU) + transformers runtime | ~300–400 MB | Loaded via sentence-transformers |
| MiniLM embedding model | ~100 MB | `all-MiniLM-L6-v2`, one shared copy |
| Cross-encoder (`mxbai-rerank-xsmall-v1`) | ~200 MB | **Lazy** — never loaded under a remote rerank provider |
| FAISS index + job rows during a rebuild | ~50–250 MB | Bounded by `REBUILD_MAX_JOBS` (4000) |
| 5 background lanes (discovery, fresh, pulse, matching, scoring) | ~100–300 MB | Peaks when passes overlap |
| **Each headless Chromium** | **~300–500 MB** | A *child process*: invisible in our RSS, fully charged to the container |

That last row is the one that actually killed it.

## The two causes, both fixed

**1. Unbounded concurrent Chromium.** Autofill, form preview, JD page scraping,
and the search-engine discovery source each launched their own browser with
nothing coordinating them. Autofill and preview are user-triggered, so N
simultaneous clicks meant N browsers. Three at once is ~1.2 GB *on top of* the ML
stack — an OOM kill on its own, and the reason the crash looked unrelated to
anything in the Python heap.

Fixed by `app/common/browser.py`: a process-wide gate, default **one** browser at
a time (`BROWSER_MAX_CONCURRENCY`). A caller that waits longer than
`BROWSER_SLOT_WAIT_SECONDS` (120s) gets a clear `BrowserBusy` error instead of
launching anyway — autofill leaves the application untouched so the user can just
click again.

**2. Three copies of the same model.** `matcher.py` cached one MiniLM,
`discovery/title_filter.py` loaded a second, and `GroundingChecker.__init__`
built a *third on every tailor request* — a fresh model load, concurrent with
whatever else was resident, on a user-triggered path. All three now share the
matcher's single cached instance.

## What to do when it happens again

1. **Look at the numbers first.** `GET /health` includes a memory snapshot, and
   `GET /api/debug/memory` (admin) has the full picture. The field that matters is
   `non_python_mb` — container usage minus our RSS. If it is large, it is
   browsers, not the Python heap, and no amount of heap tuning will help.
2. **Read the log trail.** The memory watcher logs every
   `MEMORY_WATCH_INTERVAL_SECONDS` (120s) and flips to `MEMORY HIGH` at
   `MEMORY_WARN_PCT` (85%) of the cgroup limit. An OOM kill leaves no traceback,
   so those lines are the only evidence of the climb.
3. **Raise the container's memory limit** if the steady-state baseline is already
   near the ceiling. The ML stack alone is ~600–800 MB before any work happens;
   **2 GB is a realistic floor** for this container, and headroom for one browser
   means ~2.5 GB.

## Knobs, cheapest first

| Env var | Default | Effect |
|---|---|---|
| `BROWSER_MAX_CONCURRENCY` | `1` | Concurrent Chromium. Raise **only** after raising the memory limit — each +1 is ~400 MB |
| `MEMORY_WATCH_INTERVAL_SECONDS` | `120` | Telemetry cadence; `0` disables |
| `MEMORY_WARN_PCT` | `85` | When the log line escalates to WARNING |
| `RERANK_PROVIDER` | — | A remote provider keeps the ~200 MB cross-encoder unloaded |
| `SCORING_WORKERS` | `20` | LLM workers; each holds a job's full JD + payload in flight |
| `MAX_BOARDS_PER_RUN` | `400` | Boards per discovery run — `800` contributed to an earlier OOM |
| `LANES_ENABLED` | `1` | `0` on extra replicas: web-only, no lanes (and no duplicate spend) |

## If you need to scale out

Do **not** raise `WEB_CONCURRENCY` — the startup guard logs CRITICAL for good
reason. Run additional replicas with `LANES_ENABLED=0` so they serve web traffic
only, and keep exactly one lanes-enabled process.

## The structural fix — now built

`browser-service/` runs Chromium in its own container behind a two-endpoint HTTP
API. The three *stateless* render/search paths that run on the background lanes
for every tenant — JD page scrape, Google board discovery, the search-engine
source — now call `app/common/browser_client.py`, which routes to the service
when `BROWSER_SERVICE_URL` is set and falls back to a local, gate-limited launch
otherwise. Turning it on is one env var; turning it off is unsetting it.

Autofill and form preview deliberately stay local: they are stateful interactive
sessions (CAPTCHA hand-off, pending questions, a live page held across a human's
review), and server-side autofill is founder-only today
(`autofill_multi_user_enabled=False`) while every other tenant autofills through
the MV3 Chrome extension in their own browser at zero server cost.

See `browser-service/README.md` for deployment, sizing, and the security model
(bearer auth + private-IP SSRF blocking — an open "render any URL" endpoint is a
proxy into your VPC).
