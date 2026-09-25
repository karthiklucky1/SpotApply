# Phases 10–11 — verification, evidence, and what is still blocked

*2026-09-25. Guards: `tests/test_auth_boundaries.py` (33),
`tests/test_end_to_end_flow.py` (28), `tests/test_brand_assets.py` (41).*

## 1. Completed checklist

Each item is **fixed/verified**, **not reproducible**, or **blocked** with the
specific reason. Nothing is marked verified on the strength of a local test
alone where the claim was about production.

### Authentication and authorisation

| item | status | evidence |
|---|---|---|
| Temporary Pro cannot bypass authentication | **verified** | anonymous `/api/usage`, `/api/profile`, `/api/jobs` → 401/403 with the flag on |
| Temporary Pro cannot grant admin | **verified** | all five `/api/admin/*` routes refuse; `_require_admin_user` source contains no entitlement reference |
| Temporary Pro cannot grant recruiter permissions | **verified** | `/api/recruiter/search`, `/api/recruiter/intro` refuse |
| Forged / unsigned / `alg=none` / empty bearer tokens authenticate nobody | **verified** | 7 token shapes, all refused |
| A forged cookie authenticates nobody | **verified** | 3 shapes refused |
| The cookie fallback is not a second chance for a forged header | **verified** | bad header + bad cookie → refused |
| Direct protected URL, no session | **verified** | `/dashboard` renders the signed-out shell only |
| Anonymous refusal on every non-public route | **verified (pre-existing)** | `test_route_auth_inventory` |
| Ownership on every id-bearing route | **verified (pre-existing)** | `test_route_auth_inventory` |
| Tenant isolation | **partly verified** | `test_tenant_scoping` covers apply-limits and saved answers; full cross-tenant read testing needs two live Supabase identities — **blocked** |
| Signup, verification email, OAuth callback, password reset, session refresh, logout | **blocked** | needs live Supabase Auth; `api.supabase.com` and `app.spotapply.ai` are refused at CONNECT in this environment. No message was sent to anyone. |

### Reliability and spend under broader Pro access

| item | status | evidence |
|---|---|---|
| Pro ceilings are finite | verified | `PLAN_LIMITS[PRO]` all bounded ints |
| Platform backstops still bound the estate | verified | hourly < daily cap; abuse cap ≥ plan tailor cap |
| Dormancy behaviour preserved | verified | gate reads `is_paid_entitlement`, which the flag never sets |
| Provider-failure handling unaffected | verified | `reranker` has no entitlement reference; breaker cooldown > 0 |
| Queues bounded per cycle | verified | `scoring_drain_cap`, `prescore_cap`, `top_k_rerank` all > 0 |

### Caching

| item | status |
|---|---|
| Authenticated responses kept out of shared caches | **fixed** — see §2 |
| Public pages not de-optimised by the fix | verified |
| Routes that set their own cache header left alone | verified |

## 2. A real defect: authenticated responses were cacheable by proxies

**No route set any `Cache-Control` at all.** That is only half-safe. RFC 9111
§3.5 forbids a shared cache from storing a response to a request carrying an
`Authorization` header — but SpotApply also authenticates by **cookie**
(`sb_token`, for full-page navigations that browsers do not send the
Authorization header on). A cookie-authenticated request has no such header, so
that protection does not apply, and a CDN or corporate proxy applying heuristic
freshness to a `200` could serve one tenant's board, profile or résumé list to
another.

`PrivateCacheMiddleware` now sets `Cache-Control: no-store` and
`Vary: Authorization, Cookie` on every `/api/*`, `/application/*` and
`/dashboard` response that has not already set its own. It decides **by path,
not by whether a user was found**, so an anonymous 401 and a later authenticated
200 cannot share a cache key.

| path | before | after |
|---|---|---|
| `/api/usage`, `/api/jobs`, `/api/profile`, `/dashboard` | *(none)* | `no-store`, `Vary: Authorization, Cookie` |
| `/`, `/pricing`, `/privacy`, `/terms` | *(none)* | unchanged |
| `/favicon.ico` | `public, max-age=86400` | unchanged |

## 3. Performance — LAB metrics, and they are lab metrics

**These are not real-user metrics.** They cannot be: this container has no
outbound network, so the Google Fonts stylesheet and the Tailwind CDN script
both fail to load and are recorded as 0 bytes. Real users pay for both. Treat
every number below as a **lower bound on a same-machine loopback**, useful for
comparing before against after, and useless as an absolute.

**Method.** `uvicorn` on loopback; Chromium 1194 via Playwright; **7 runs per
page**, each in a **fresh browser context** so every run is a cold-cache first
visit; medians reported with min/max; timings from the Navigation Timing and
Resource Timing APIs. No network shaping was applied — a throttled-network
profile is **not** claimed.

### Landing page `/` — before → after

| metric | before | after |
|---|---|---|
| total transfer | **362 KB** | **227 KB** (−37%) |
| hero image | 305 KB PNG | 174 KB WebP (−43%) |
| requests | 8 | 8 |
| TTFB (median) | 4.0 ms | 4.3 ms |
| load (median) | 389 ms | ~355 ms |

The hero PNG was **86% of everything the landing page transferred**. Re-encoding
the same pixels to WebP at q=0.86 keeps the board's card text legible at full
resolution (checked visually at 2556×1400). The PNG stays as the `<source>`
fallback — this adds a format, it does not remove one. `fetchpriority="high"`,
explicit dimensions and the narrow-screen crop are all preserved.

Regenerate with `python -m scripts.build_webp_shots`.

### `/pricing` — a finding, not a fix

`/pricing`, `/privacy` and `/terms` load **`https://cdn.tailwindcss.com/`**, the
Tailwind **Play CDN**, which Tailwind's own documentation says is for
development and not for production. It is a render-blocking third-party script
that compiles CSS in the browser on every page load. In this sandbox it is
blocked, so the pricing page renders **unstyled** — which is also what a user
behind a firewall that blocks the CDN would see.

**Not fixed here, and the reason is specific:** the remedy is a third Tailwind
config compiling `pricing.html` to a committed stylesheet, exactly as
`landing.html` and `dashboard.html` already are. That needs `npm run build`, and
**`node_modules` is not installed in this environment**. The precise remaining
step is in §6.

What *was* fixed: all three pages now `preconnect` to `fonts.googleapis.com`,
`fonts.gstatic.com` and `cdn.tailwindcss.com`. They had no preconnect at all, so
the TLS handshake for two render-blocking origins only began after the HTML was
parsed.

## 4. End-to-end evidence

### The sample — synthetic, and labelled as such

**13 postings**, invented for `tests/test_end_to_end_flow.py`. No real posting,
candidate history, contact detail or immigration status appears anywhere in this
repository. A live sample is **not** claimed: the production database is
unreachable from here.

| dimension | coverage |
|---|---|
| sources | workday, greenhouse, lever, ashby, smartrecruiters, teamtailor (6) |
| work modes | onsite, hybrid, remote, unstated (4) |
| verdicts | 3 eligible / 7 ineligible / 3 held |
| special cases | US state restriction, missing metadata, conflicting evidence, colliding Workday requisition id, incompatible experience, listed-but-unused skill |

Fixtures are the right instrument here *because* the decisions are deterministic
— the same inputs must give the same verdict at intake, adoption, retrieval,
scoring and delivery, and only a repeatable fixture can assert that. What they
cannot tell you is what the live corpus looks like, so **nothing is
extrapolated**.

### Two corrections the fixtures forced on me

Writing the expectations surfaced two things I had assumed wrongly, and the code
was right both times:

1. **On-site or hybrid in another US city is INELIGIBLE** while
   `open_to_relocation` is off — same country and even same state is not enough,
   because the candidate would have to be there. I had expected "same state =
   eligible".
2. **A state restriction is DECIDABLE when the profile names a state.**
   "Must be based in California" against a profile in Cincinnati, OH is
   *ineligible*, not held. It is held only when the profile names no state at
   all — the rule is about what the evidence can decide. Both behaviours are now
   pinned, including the held case.

### One suitable job, walked end to end

`us-onsite-home` (Backend Engineer, Cincinnati OH, on-site):
identity stable → **eligible** with a reason → requirements read as written
(24 and 12 months) → every requirement **supported** by paid work in the
inventory → pre-download review lists them, reports no gap, and contains no
percentage, chance or likelihood anywhere.

### One correctly rejected job, walked end to end

A Principal role asking **12 years** and 8 years of Kubernetes against a résumé
holding under 3: every requirement resolves **short** or **gap**, the review
states the shortfall in the posting's own words, and the master résumé itself
invents nothing (`ok is True`).

### Relocation, both ways

| posting | relocation off | relocation on |
|---|---|---|
| on-site, home city | eligible | eligible |
| on-site, another US city | **ineligible** | **eligible** |
| on-site, another country | ineligible | **ineligible** |

And the résumé line is a **separate consent**: `relocation_resume_optin`
defaults off even for a user who is already `open_to_relocation`.

### History preserved

The three new profile fields default off/empty, so no existing user's board or
history changes. The temporary-Pro flag writes no rows, so turning it on or off
touches no history either.

## 5. Remaining risks

* **The Phase 2 identity repair has not run in production** — ~3,900 colliding
  ids, 240+ applications affected. This blocks contact research (§Phase 8) and
  means some rows still carry another employer's requisition.
* **Temporary Pro is implemented but not enabled.** One env var; a product
  decision, not an engineering one.
* **Live auth flows are unverified** (blocked, §1). The code paths are
  unchanged by this work, but "unchanged" is not "tested".
* **`/pricing` still depends on a development CDN** at runtime.
* **No production deployment is claimed.** Everything here ran locally.

## 6. Exact remaining manual steps

In order, each independently verifiable:

1. **Compile `/pricing` off the CDN.** `npm ci && npm run build`, add a third
   Tailwind config for `pricing.html` → `app/static/tailwind-pricing.css`,
   swap the `<script src="https://cdn.tailwindcss.com/">` for the compiled
   stylesheet, re-run `tests/test_landing_assets.py`. Requires `node_modules`.
2. **Run the identity repair.** `python -m scripts.diagnose_job_identity` (no
   flags) where the database is reachable; decide the side-table ownership
   question before `--apply`.
3. **Run the subscription audit.** `python -m scripts.audit_subscriptions`.
   Read-only. Decide the transition from its output (docs/TEMPORARY_PRO_AND_SUBSCRIPTIONS.md §3).
4. **Enable temporary Pro** if wanted: `TEMPORARY_PRO_FOR_ALL=1`, restart.
   Verify `/pricing` shows the notice and `POST /api/billing/checkout` returns
   503.
5. **Exercise the live auth flows** against a staging Supabase project with test
   accounts: signup → verification email → login → OAuth callback → password
   reset → refresh → logout → expired session → direct protected URL.
6. **Re-measure performance against production**, with a stated device and
   network profile, and compare to real-user metrics rather than to §3.
7. **Deploy** per existing authorisation. Nothing in this work has been
   deployed, and no production success is claimed.

## 7. Screenshots

**Not delivered — blocked.** Deliverable 3 asks for redacted screenshots of the
profile relocation settings, pricing/temporary-Pro messaging, Chrome branding,
the privacy date, shortlist explanations and the résumé review. Producing them
honestly requires an authenticated session against a running instance with real
data. This environment cannot reach production, and screenshotting a local
instance with synthetic data would be a picture of a fixture presented as a
picture of the product.

What exists instead, and is stronger for the branding item: the favicon was
rendered and inspected directly (a valid 3-frame ICO, verified by `file` and by
structural test), and every asset, content type and template string above is
asserted against the running application rather than photographed.
