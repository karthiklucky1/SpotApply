# SpotApply Browser Service

Headless Chromium behind a two-endpoint HTTP API, so the main app container
stops paying for it.

## Why

The main app is deliberately **one process**: every lane lock, LLM budget
counter, in-flight claim, and rate limiter is process-local, so a second worker
would double scraping and LLM spend. The cost of that decision is that
everything shares one memory limit — FastAPI, torch, sentence-transformers,
FAISS, five background lanes, and, until now, headless Chromium.

Each Chromium is a ~300–500 MB **child process**: invisible in the app's own RSS
but charged to the container in full. That mismatch is why the OOM crash looked
inexplicable — the Python heap sat at a few hundred MB while the container was
at its ceiling. `app/common/browser.py` capped the bleeding by serializing
launches. This service removes the cost from the web container entirely.

## What moved, and what deliberately did not

**Moved** — the three *stateless* render/search operations. These run on the
background lanes, unattended, on behalf of every tenant:

| Caller | Operation |
|---|---|
| `app/discovery/extractor.py` | `scrape_job_page(url)` → page text |
| `app/discovery/google_search.py` | `_search_google_playwright(q)` → links |
| `app/discovery/sources/search_engine.py` | `_query_playwright(q)` → links |

**Did not move** — autofill and form preview (`app/autofill/agent.py`). They are
stateful, long-lived, interactive sessions: CAPTCHA hand-off, pending-question
collection, a live `Page` held across a human's review before *they* click
Submit. Remoting them needs a session protocol, not a request/response call.

That is a smaller loss than it sounds, because server-side autofill is
**founder-only today** — `autofill_multi_user_enabled` defaults to `False`
(`app/config.py`) and `_set_fill_owner` (`app/autofill/agent.py:624`) refuses any
other user's fill rather than risk submitting under the wrong identity. Every
other tenant autofills through the **MV3 Chrome extension**, in their own
browser, costing the server nothing. The extension — not a remote Playwright
service — is the real multi-tenant autofill path.

## Endpoints

```
POST /render   {url, wait_until, timeout_ms, settle_ms, user_agent, extract}
               -> {ok, status, final_url, title, text, links[], error, elapsed_ms}
POST /search   {query, engine, timeout_ms, settle_ms}   -> same shape
GET  /health   -> {ok, browser:{served, in_flight, connected, ...}, memory:{...}}
```

`extract` is `text` | `links` | `both`. A page that fails to render returns HTTP
**200 with `ok:false`** — that is an answer, not a server error, and it lets the
client tell "the page is dead" apart from "the service is down".

## Design

* **One long-lived Chromium, a fresh `BrowserContext` per request.** Same
  isolation as launch-per-request at a fraction of the cost (a launch is ~1–2 s
  and churns hundreds of MB).
* **Bounded concurrency** (`BROWSER_CONCURRENCY`). This is a *memory budget*,
  not a throughput knob — each concurrent context is real RAM.
* **Periodic recycle** (`BROWSER_RECYCLE_AFTER`). Long-lived Chromium leaks.
  The check runs on request arrival and only while nothing is in flight, so
  `since_recycle` in `/health` drifts past the threshold under sustained load
  until the first quiet moment. That is expected.
* **Crash recovery.** A dead browser is detected via `is_connected()` and
  relaunched; a crash never becomes a permanently failing service.
* **One uvicorn worker, always.** The pool is process-local state — a second
  worker silently doubles the browser count and the memory budget, which is the
  exact bug this service exists to fix. Scale with replicas.

## Security

This service fetches whatever URL it is told to and hands back the response
body. Unprotected, it is an SSRF proxy into your private network.

* **Bearer auth** — `BROWSER_SERVICE_TOKEN`, compared with `hmac.compare_digest`.
  Empty disables auth and logs a loud warning at startup; only acceptable on a
  private network.
* **Scheme allowlist** — `http`/`https` only. `file://` and friends are refused.
* **Private-IP block** (`BROWSER_BLOCK_PRIVATE_IPS`, on by default) — every
  resolved address is checked and loopback / private / link-local / reserved
  targets are refused. This is what stops `http://169.254.169.254/` (cloud
  instance metadata) from being readable through the service.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `BROWSER_SERVICE_TOKEN` | *(empty)* | Bearer secret. Empty = **no auth** |
| `BROWSER_CONCURRENCY` | `2` | Concurrent contexts — a memory budget |
| `BROWSER_RECYCLE_AFTER` | `200` | Relaunch after ≥N requests; `0` = never |
| `BROWSER_MAX_TIMEOUT_MS` | `60000` | Ceiling on any single navigation |
| `BROWSER_MAX_TEXT_CHARS` | `200000` | Cap on returned text (LLM cost guard) |
| `BROWSER_BLOCK_PRIVATE_IPS` | `1` | SSRF guard |
| `BROWSER_EXECUTABLE_PATH` | *(empty)* | Explicit Chromium binary; needed when the host's build revision differs from Playwright's expected one |
| `LOG_LEVEL` | `INFO` | |

## Deploying on Railway

1. New service in the same project, root directory `browser-service/`. It builds
   from this `Dockerfile` and this `railway.toml`.
2. Set `BROWSER_SERVICE_TOKEN` to a long random string on **both** services.
3. Size it at **1 GB** to start (~60 MB Python + ~400 MB per concurrent context);
   raise `BROWSER_CONCURRENCY` only alongside the memory limit.
4. On the main app set:
   ```
   BROWSER_SERVICE_URL=http://<service>.railway.internal:8080
   BROWSER_SERVICE_TOKEN=<the same secret>
   ```
   Use the private network URL — there is no reason to expose this publicly.
5. Verify `/health` shows `"connected": true`, then watch the app's own
   `memory [watch]` log lines: `non_python_mb` should drop to roughly zero
   outside of founder autofill runs.

To roll back, unset `BROWSER_SERVICE_URL`. The app returns to local rendering
behind the `browser_slot` gate with no redeploy of application code.

## Failure behaviour

`app/common/browser_client.py` decides what happens when this service misbehaves:

| Situation | Behaviour |
|---|---|
| Service renders, page fails | Raises — authoritative, no local retry |
| Service unreachable, `BROWSER_SERVICE_FALLBACK_LOCAL=1` (default) | Local render, logged as a warning |
| Service unreachable, fallback `0` | Raises — correct when this container has no room for a browser |
| Search fails, any cause | Returns `[]` — "this source found nothing this pass" |

Set `BROWSER_SERVICE_FALLBACK_LOCAL=0` once the app container is sized *without*
room for a local Chromium. Falling back there is the OOM you were avoiding.
