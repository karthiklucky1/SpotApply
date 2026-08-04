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

## The second OOM — the Python-side climb (Jul 2026)

A later OOM had the opposite signature: `non-python` flat at ~100MB while
**rss itself** climbed 850MB → 7GB over hours, in steps, never shrinking.
Three compounding causes, all fixed:

1. **glibc arena fragmentation.** The scoring lane built a fresh 20-thread
   pool every 90s and the pulse lane a 24-thread pool every 60s, abandoned
   with `shutdown(wait=False)` — every new thread can pin a 64MB malloc arena
   whose high-water mark never shrinks, and glibc ratchets its trim threshold
   upward on torch's large frees. Fixes: both lanes reuse ONE persistent pool
   for the life of the process (`scoring_lane._worker_pool`,
   `pulse_lane._fetch_pool`) and cancel queued work at deadline; the
   Dockerfile pins `MALLOC_ARENA_MAX=2` + trim/mmap thresholds + single-thread
   OpenBLAS/numexpr/tokenizers. Those MUST be process env — glibc reads them
   before Python starts, so setting them in `app/__init__.py` does nothing.
2. **Leaked LLM SDK clients.** 20+ call sites built a throwaway
   `Anthropic()`/`OpenAI()` per call — each an httpx pool + SSL context that
   is never returned to the OS. Every call site now goes through the shared
   process-wide pair in `app/common/llm.py` (`with_options()` for per-path
   timeout/retry — it reuses the same connection pool). Never construct an
   SDK client directly.
3. **Whole-pass buffers.** Discovery held all ~400 boards' postings
   (~300–650MB) for the entire run and the pulse lane all 300 per tick — both
   now release each board as it is processed. The per-user FAISS index grew
   without bound (purged jobs' vectors were never removed) and was re-read
   into RAM every pass — it compacts via full rebuild past 3× REBUILD_MAX_JOBS.

## What to do when it happens again

1. **Look at the numbers first.** `GET /health` includes a memory snapshot, and
   `GET /api/debug/memory` (admin) has the full picture. Start with
   `non_python_mb` — container usage minus our RSS. If it is large, it is
   browsers, and no amount of heap tuning will help. If it is SMALL while
   `rss_mb` climbs and never falls, it is the Python-side pattern above —
   check `env_summary()` still shows `MALLOC_ARENA_MAX=2` and look for new
   per-call SDK clients or per-tick thread pools before anything else.
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
