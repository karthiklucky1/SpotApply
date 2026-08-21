# Pre-launch check pack — before 10 friends start using SpotApply

*Aug 2026. Copy each block below into a Claude session that has the access it names
(Railway logs, Supabase SQL, a browser on app.spotapply.ai, or this repo). They are ordered
so a failure in an early one makes the later ones pointless — run them in order.*

**Give every session this context first:**

> SpotApply (app.spotapply.ai) is a multi-tenant FastAPI + SQLModel app on Railway with
> Supabase Postgres + Auth. One container runs the web app AND five background lanes
> (discovery, fresh, pulse, matching, scoring). Read `CLAUDE.md` and `docs/ARCHITECTURE.md`
> first. Six changes shipped on 2026-08-21 (`e9a4289`, `c3d5bc1`/`7efd6f9`, `43774c5`,
> `b0fc5f7`, `90034ab`, `6008f8d`): an adaptive per-user LLM budget with a persisted
> ledger, three new `job` columns, one new expression index, a projected job-list query,
> and a dashboard that no longer server-renders job descriptions. Report findings as
> SEVERITY / WHAT / EVIDENCE / FIX, and do not change code unless I ask.

---

## 1. Deploy health and schema migration — Railway logs + Supabase SQL

> Verify the 2026-08-21 deploys landed cleanly.
>
> **In the Railway logs since the last deploy:** confirm there are no tracebacks, no
> `Schema migration FAILED`, no `statement timeout`, no `QueuePool limit`, no
> `marked DOWN (credit/quota)`, and no `MULTIPLE WORKERS DETECTED`. Find the lines
> `Ensured index ix_job_user_fresh …` and `Schema migration: added …` and tell me exactly
> which columns were added and when. Confirm the scoring lane logs a cycle roughly every
> 90–210s and that `plan_capped` / "no finals allowance" warnings are absent or explained.
>
> **In Supabase, run and show me the output:**
> ```sql
> SELECT column_name, data_type, column_default, is_nullable
> FROM information_schema.columns
> WHERE table_name IN ('job','user_usage')
>   AND column_name IN ('on_role','salary_text','sponsorship_json','prescore',
>                       'finals_count','finals_hits');
>
> SELECT c.relname, i.indisvalid, i.indisready
> FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
> WHERE c.relname LIKE 'ix_job%';
> ```
> `finals_count`/`finals_hits` MUST have `DEFAULT 0` (a NULL there makes the spend counter
> silently stop counting = unlimited spend). Any index with `indisvalid = false` is a
> half-built `CREATE INDEX CONCURRENTLY` that the planner ignores forever while
> `ensure_performance_indexes` keeps skipping it — tell me if you find one.

## 2. Did the backfills actually finish? — Supabase SQL

> Three columns are filled by bounded per-boot backfills (`app/strategy/on_role.py` 20,000
> rows/user/pass, `app/strategy/job_facets.py` 5,000/user/pass). Until a row is stamped the
> board falls back to the old slow path (`on_role`) or shows no chip (`salary_text`,
> `sponsorship_json`). Run per user_id:
> ```sql
> SELECT user_id,
>        count(*) AS jobs,
>        count(*) FILTER (WHERE on_role IS NULL)          AS on_role_missing,
>        count(*) FILTER (WHERE sponsorship_json IS NULL) AS facets_missing,
>        count(*) FILTER (WHERE rerank_score IS NULL)     AS unscored
> FROM job WHERE is_closed = false GROUP BY user_id ORDER BY jobs DESC;
> ```
> Tell me how many boots it will take to finish at those batch sizes, and whether the
> unscored queue is draining or growing day over day.

## 3. Cost and capacity for 10 users — code + SQL + my numbers

> This is the one I most want challenged. With Stripe unconfigured, `_get_user_plan`
> returns **PRO for everyone** (`server.py:6424`), so 10 friends get PRO limits, not FREE.
>
> Check my arithmetic against the code and the live data:
> - Scoring: PRO = 50 finals/day soft, ×2 burst, ×7 weekly (350). At ~$0.0033/final that is
>   **~$5/user/month ⇒ ~$50/month for 10**. Confirm `FINALS_*_MULTIPLIER`,
>   `PLAN_LIMITS["finals_daily"]` and the Haiku pricing in `docs/CAPACITY.md` §3.
> - Platform backstop: `LLM_DAILY_FINAL_CAP=5000` and `LLM_HOURLY_FINAL_CAP=400`. Ten users
>   bursting = 1,000 finals/day, so the backstop should not bind — verify, and note that
>   these counters live in **process memory** and reset on every deploy.
> - **Tailoring is the real exposure and is NOT in the adaptive budget.**
>   `TAILOR_ABUSE_DAILY_CAP=150`/user/day on Sonnet 4.6 at roughly $0.02–0.03 per tailor
>   ⇒ up to **$45/day / $1,350/month for 10 users** if they use it hard. Confirm the price
>   per tailor from `app/tailoring/tailor.py` token counts and tell me what cap you would
>   set before inviting anyone.
> - DB: `DB_POOL_SIZE=10` + `DB_MAX_OVERFLOW=20` against 20 scoring + 16 prescore + 12
>   rerank + 24 pulse-fetch workers plus web traffic. Say whether 10 active users can
>   exhaust it and what the symptom would look like in the logs.
> - Memory: one container holds torch + MiniLM + FAISS + five lanes + Chromium
>   (`docs/MEMORY.md`). Check `/api/debug/memory` (admin) and the memory-watcher lines for
>   headroom at 10 users. `ADOPT_MAX_JOBS=400`/user/pass × 16 passes/day is the row faucet.
>
> Give me a single number: expected $/month at 10 users, and the cap changes you would make
> first.

## 4. New-user onboarding, end to end — browser, a throwaway account

> Sign up as a brand-new user and walk the whole path, screenshotting each step:
> signup → email confirm → résumé upload → target roles → first jobs appear → first scores →
> shortlist → open a job drawer → tailor → cover letter → autofill/extension → mark applied.
>
> Specifically confirm:
> - Nothing shows a spinner forever. `ONBOARDING_MIN_JOBS=25` triggers a targeted scrape —
>   how long until the first card appears, measured?
> - A user with NO résumé is told to upload one, not left waiting for matches that cannot
>   come (matching is résumé-gated).
> - The Job Explorer opens on **Last 7 days** and loads on first visit (this changed today).
> - Opening a drawer fetches the posting body from `/application/{id}/description` — check
>   the network tab; the JD must NOT be in the initial dashboard HTML.
> - The salary chip and sponsorship badge appear on cards (they now come from
>   `job.salary_text` / `job.sponsorship_json`; if they are missing, the backfill in check 2
>   has not reached those rows).
> - Every empty state has copy that tells the user what to do next.
>
> Report anything that would make a friend give up in the first ten minutes.

## 5. Multi-tenancy isolation — two accounts, browser + SQL

> With two real accounts open side by side, prove no data crosses:
> - `/api/jobs`, `/dashboard`, `/api/stats`, `/application/{id}/*`, `/api/account`: request
>   account B's application id while logged in as A — it must 404, not 403 and never 200.
> - Try each of those with NO auth header at all. `_get_user_id` returns None when
>   anonymous and scoping off it FAILS OPEN, so a non-public route must refuse first —
>   `CLAUDE.md` names the guards (`_require_user`, `_require_owned_application`,
>   `_require_admin_user`, `_require_admin`).
> - Run `pytest tests/test_route_auth_inventory.py tests/test_tenancy_fail_closed.py -q`
>   and tell me whether any route reached `PUBLIC_PATHS` without a reason.
> - In SQL: `SELECT user_id, count(*) FROM job GROUP BY 1;` — confirm the `__shared__` pool
>   is never returned by a user-facing query, and that no row has a NULL owner in prod.
> - The new endpoint `/application/{id}/description` shipped today: verify its ownership
>   guard by hand, since it serves raw posting text.

## 6. Security review — repo + live

> Run `/security-review` on `origin/main`, then check by hand:
> - Supabase JWT verification: is the signature actually verified (not just decoded), and
>   is the service-role key ever reachable from a request path?
> - Rate limiting (slowapi): which routes are covered, and are the expensive ones
>   (tailor, autofill, insights, discovery triggers) among them? What stops one friend
>   from spending the whole LLM budget?
> - XSS: today's drawer loader injects posting text client-side. Confirm it uses
>   `textContent` (it should) and audit every other `innerHTML` in `dashboard.html` that
>   carries job/company text or LLM output.
> - Admin routes: `_require_admin_user` / `_require_admin` — who is admin, and how is that
>   decided? Can a normal user reach `/api/debug/memory` or any admin JSON?
> - Secrets: no keys in the repo, in logs, or in client-side JS. Check the extension's
>   init-pack broadcast in `dashboard.html` — it posts a Supabase token to the page; verify
>   the origin check.
> - Uploads: résumé parsing (`pypdf`, `python-docx`) on user files — size limits, type
>   checks, and what happens with a malformed or huge file.
> - SSRF: `/api/jobs/{id}/verify` and the JD scrapers fetch user-influenced URLs. Confirm
>   private-host blocking (there is a test named for it — `test_job_check_blocks_private_hosts`).

## 7. Privacy and legal — repo + live + the policy pages

> Ten friends means ten people's résumés, emails and work history. Check:
> - **What we store and where:** enumerate every table holding personal data
>   (`userprofile`, `job`, `application`, `answer_memory`, `user_personal_memory`,
>   `funnel_events`, résumé files in `data/`) and confirm `/privacy` and `/terms` describe
>   it truthfully. Flag anything the pages claim that the code does not do, and anything
>   the code does that the pages do not disclose.
> - **Deletion:** `DELETE /api/account` — does it remove every user-scoped table plus
>   stored résumé files and the Supabase auth user? Run `pytest
>   tests/test_account_deletion.py -q` (it is schema-driven, so a new table fails it) and
>   confirm the three columns added today are covered.
> - **Export:** is there a data-export path? GDPR Art. 15/20 and India's DPDP both expect
>   one. If not, say so plainly — it is a gap, not a blocker for friends.
> - **Legal basis and residency:** where is Supabase hosted, and do the pages name the data
>   controller and a contact? If any friend is in the EU or UK, note what changes.
> - **Third-party processors:** Anthropic, OpenAI, Supabase, Railway, Stripe, Telegram —
>   is each one disclosed? Résumé text goes to the LLM providers; the privacy page must say
>   so.
> - **Email sync** (`sync_emails`) reads a mailbox. Confirm what scope it requests, what it
>   stores, and that this is disclosed.
> - **Scraping compliance:** `CLAUDE.md` says public ATS/feeds only, robots.txt respected,
>   no LinkedIn/Indeed automation. Verify no code path automates a logged-in session on
>   either, and that discovery-only links are just links.
> - **The product's own promise:** tailoring must stay grounded in the real résumé
>   (`app/tailoring/grounding.py`). Run `pytest tests/test_grounding_enforcement.py -q` and
>   tell me whether "never ran" can be mistaken for "passed".

## 8. Visual and layout QA — browser, real devices

> Screenshot and judge, at 1440px, 768px and 390px wide, in both light and dark:
> landing, pricing, auth, dashboard (all tabs: board, Explorer, Boards, Ghost, Skills,
> X-Ray), a job drawer, privacy, terms, extension.
> - Any text unreadable, clipped, overlapping, or below ~12px on mobile?
> - Any horizontal scroll on the page body? Wide tables must scroll inside their own
>   container.
> - Fonts: do all weights load, or is anything falling back to a system font mid-page?
> - **Stale CSS check:** `app/static/tailwind.css` is compiled and committed. Today's
>   template edits were made WITHOUT running `npm run build` (no node_modules in that
>   session). Run `npm install && npm run build`, then `git diff --stat app/static/` — if
>   the stylesheet changes, the live site is running stale CSS and classes built in
>   JavaScript may be unstyled. This exact failure has happened before; `CLAUDE.md`
>   documents it.
> - The new drawer body is built in JS with literal Tailwind classes — confirm it renders
>   styled, not as raw unstyled text.

## 9. Failure drills — what a friend sees when something breaks

> Force or simulate each, and tell me what the user sees and what the logs say:
> - Anthropic and OpenAI both out of credits (the circuit breaker trips; is there a local
>   fallback score, and does the board explain itself?).
> - Supabase unreachable for 60s mid-scoring cycle.
> - A deploy in the middle of a scoring cycle — jobs must stay Queued, never half-scored.
>   Note: every lane sleeps 120–300s before its first tick, so deploying every 2–3 minutes
>   means the scoring lane never runs at all.
> - A user whose résumé fails to parse.
> - A job whose posting 404s after shortlisting (ghost/liveness path).
> - Two users on the same company hitting `COMPANY_CAP=3`.

## 10. The things I am least sure about — check these specifically

> 1. **The prescore drain rate.** Recent cycles drained ~97% of candidates at Tier-1
>    (gpt-4o-mini). If Tier-1 is stricter than the Haiku fallback it replaced, good jobs may
>    be stamped out before Claude sees them. Run `scripts/eval_scorers.py --mode gate` at
>    N≥2000 and tell me whether `PRESCORE_ADVANCE_THRESHOLD=40` is still right.
> 2. **The adaptive budget under real traffic.** Watch `user_usage.finals_count/finals_hits`
>    for a week: does anyone hit the weekly 350, does the burst zone ever open, and does the
>    marginal-yield test stop spending on a genuinely dead day?
> 3. **`ix_job_user_fresh` is actually used.** `EXPLAIN (ANALYZE, BUFFERS)` the Explorer
>    query with `sort=fresh` + `max_age_days=7` and confirm an index scan, not a sort.
> 4. **The middle band.** Jobs prescoring between 40 and 55 during burst are left Queued
>    with their prescore. Confirm they are actually picked up later and not stranded.
> 5. **Single process.** If Railway ever runs 2 replicas, every lane, budget counter and
>    alert doubles. Verify replica count is 1 and that `LANES_ENABLED=0` is set on any
>    extra.
> 6. **10 users × adoption.** `strategy/adoption.py` copies up to 400 rows/user/pass, 16
>    passes/day. Check the row growth rate and when `JOB_PURGE_MAX_AGE_DAYS=60` starts
>    mattering for Supabase storage and egress.

---

## What I would fix before sending the invites

Ranked, from this session's knowledge — have the other session confirm or overrule each:

1. **Cap tailoring.** It is the only uncapped Sonnet spend and dwarfs the scoring budget.
2. **Decide the plan story.** Everyone resolves to PRO while Stripe is unconfigured; ten
   PRO users is a real bill. Either wire Stripe or lower `PLAN_LIMITS[FREE]` and make it
   the default for invitees.
3. **Confirm the backfills completed** (check 2) — otherwise the board looks half-built.
4. **Rebuild the CSS** (check 8) — the committed stylesheet may be stale as of today.
5. **Read the privacy page against the code** (check 7). Ten friends' résumés go to two US
   LLM providers; that must be written down before, not after.
