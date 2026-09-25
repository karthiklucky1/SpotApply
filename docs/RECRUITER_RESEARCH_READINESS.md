# Recruiter-research readiness

*2026-09-25. Feature status: **ON HOLD**. Nothing here launches, prices or sells
anything. Telemetry: `app/intelligence/contact_research.py`,
`GET /api/admin/contact-research`. Guard: `tests/test_contact_research.py`.*

## 1. Three different features wear the word "recruiter"

Phase 8 asks whether the recruiter portal and hiring-contact research are the
same feature. They are not — and a third thing exists as well.

| | direction | what it is | status |
|---|---|---|---|
| **Recruiter portal** — `/recruiter`, `/api/recruiter/register\|search\|intro` | inbound | a *verified recruiter* searches pooled candidates and requests an intro the **candidate must accept** before contact opens | shipped, free, no checkout path |
| **Hiring context** — `discovery/hiring_context.py` | none | evidence-typed facts a posting **already states** (department, team, req id), captured at ingest from data we hold | shipped, no external call |
| **Contact research** — `intelligence/linkedin_xray.py` + `referral.py` | outbound | Google X-Ray via SerpAPI for public LinkedIn profiles, then outreach drafts | **the feature on hold** |

Only the third is what "recruiter research" would sell. The portal is the
*opposite* direction and shares no code with it. Conflating them would let a
readiness claim rest on the wrong evidence.

## 2. Measurement — and what could not be measured

Phase 8 asks for observed elapsed time and cost per stage **where logs support
it**. On 2026-09-25 they did not. `find_champions` logged only on exception;
`generate_referral_drafts` logged nothing at all; no counter anywhere recorded
how many searches ran, how many returned nobody, or how long any of it took.

That is the finding, not a gap to paper over. **No elapsed time or cost for
contact search, relevance verification or output generation can be reported for
any period before this change**, and a report claiming otherwise would be
inventing numbers.

What has been added is the instrumentation that makes the measurement possible
without launching anything — counters around code that already runs:

| stage | implemented? | instrumented now |
|---|---|---|
| job validation | yes (`strategy/delivery_gate.py`, `discovery/liveness.py`) | already was: **~55 requests in 10.5h, p50 ~335ms** |
| contact search | yes (`linkedin_xray.find_champions`) | **new** — elapsed, outcome, provider calls |
| relevance verification | **NO — does not exist** | reports `implemented: false`, never `0 runs` |
| output generation | yes (`referral.generate_referral_drafts`) | **new** — elapsed, outcome, draft count |

Four states are kept apart, because collapsing any of them into `0` is how a
readiness report claims a capability the product lacks:

* `implemented: false` — never built.
* `observed_runs > 0, measured: false` — attempted, but **no provider was
  called** (no `SERPAPI_KEY`), so there is **no timing**. Not zero time.
* `by_outcome.empty` — ran, succeeded, **found nobody**. The most common real
  result, and not a failure.
* `by_outcome.ok` — returned at least one candidate.

Outcomes are recorded separately as `ok / empty / failed / timeout / quota /
not_configured`. A quota refusal is not filed as a failure: it is recoverable and
says nothing about whether contacts exist.

**Cost.** One SerpAPI search per call; the free tier is 100/month. `provider_calls`
counts only calls that actually left the process, the same rule
`analytics/spend.py` applies to LLM calls — an attempt with no API key is not
billable and is not counted. No cost figure is quoted here because none has been
observed yet.

## 3. Why no useful contacts were found

Not a bug, and not a tuning problem. **Three independent samples, 66 postings,
one answer:**

| sample | n | named recruiter | named hiring manager |
|---|---|---|---|
| `docs/research/hiring-contacts-2026-09.md` | 16 reporting statements | 0 | 0 (16/16 title-only) |
| production audit, shortlisted jobs | 45 | 2.2% (1) | **0/45** |
| five-job pilot, `package-pilot-2026-09-25.md` | 5 | 0 | 0 |

Two near-misses in the pilot, both correctly excluded, illustrate the real
difficulty:

* CrowdStrike's posting contains `recruiting@crowdstrike.com` — an
  **accommodations mailbox**, not a contact for that requisition. Using it for
  outreach would be both ineffective and a misuse.
* A Glassdoor snippet described TeamDynamix's process as including "an interview
  with the hiring manager". That establishes a **process, not a person**.

The reason is structural: **employers do not put these names in postings.** No
amount of parsing extracts what is not there, so a contact-research product must
find people *outside* the posting — which is where every prerequisite below bites.

## 4. Prerequisites, and where each one actually stands

| prerequisite | status | what is missing |
|---|---|---|
| **Job suitability** | ✅ done | the posting is scored and geo-verified before anything else; a contact for a job the user should not apply to is worthless |
| **Employer identity** | ⚠️ partial | Phase 2 scoped `(source, external_id)` per tenant, but the production repair **has not run** — ~3,900 colliding ids, 240+ applications affected. The pilot hit this live: CrowdStrike's row carried **another employer's** requisition id. Searching for a person at the wrong company is worse than finding nobody. |
| **Lawful available sources** | ⚠️ constrained | SerpAPI over Google's public index only. CLAUDE.md forbids LinkedIn scraping or automation, and that stands. A search snippet is thin evidence, and there is no licensed contact-data source configured. |
| **Verifiable current role / relevance** | ❌ **absent** | nothing checks that a person a snippet surfaced still holds the role it implied. This is the `implemented: false` stage. |
| **Stale-contact handling** | ❌ absent | no capture date, no re-verification, no expiry. A snippet can be years old and nothing says so. |
| **Confidence / evidence display** | ⚠️ partial | `hiring_context` is fully evidence-typed and the alumni draft now ships its snippet verbatim plus a verify-before-sending step (§5). Search-derived people still carry no confidence band. |

## 5. A fabrication found and fixed on the way

The alumni draft told a **real, named person**:

> "I noticed you also went to *<university>* and now work at *<company>* as a
> *<role>*."

`role` is the title of the job **the user is applying for** — asserted about
someone else — and the university came from a substring match against a Google
snippet, which can mention a school for any number of reasons. Two invented
claims about a real person, in a message the product then invited the user to
send.

Now: the draft says only what was observed ("your profile came up while I was
looking into …"), the matched snippet travels with it **verbatim** as
`suggested_contact.evidence` so the user can judge the match, and
`verify_before_sending` says outright that a snippet is not proof of attendance
and that we have not checked their current role. When the search returns nobody,
the body keeps a `{Alumni Name}` placeholder — never a guessed name.

## 6. Readiness checklist

Every line is a gate, not a wish. **None may be waived to ship sooner.**

- [ ] **Employer identity repair has run in production** and collisions are zero.
      Blocking: a contact at the wrong employer is a worse product than none.
- [ ] **A lawful, licensed contact source is chosen and contracted**, or the
      feature is explicitly scoped to public search snippets only and priced
      accordingly. SerpAPI's free tier is 100 searches/month.
- [ ] **A relevance-verification stage exists** — currently `implemented: false`.
      It must answer "does this person still hold this role at this employer?"
      from evidence, and record what it checked.
- [ ] **Stale-contact handling**: capture date stored, re-verification interval
      set, expired contacts withheld rather than shown with a caveat.
- [ ] **Every surfaced person carries an evidence class and a confidence band**,
      to the standard `hiring_context` already meets.
- [ ] **≥30 days of telemetry** from `/api/admin/contact-research` showing the
      real `ok / empty / failed / timeout / quota` split and observed latency.
      One run is not an estimate.
- [ ] **A measured hit rate above a stated bar.** The bar is a product decision,
      but it must be set *before* the measurement, and the three samples so far
      put the naive rate near zero.
- [ ] **GDPR/CCPA position written down** for storing third-party personal data,
      including a deletion path for people who never used SpotApply.
- [ ] **No outreach is ever sent by the product.** Drafts only, user sends.
      (Currently true and pinned by test.)

## 7. Remaining engineering — range, with assumptions

Ranges, not point estimates, and each states what it assumes. These are
engineering estimates only; they exclude legal review, vendor negotiation and
any product/pricing work.

| work | range | assumes |
|---|---|---|
| Run + verify the employer-identity repair | **1–3 days** | the Phase 2 script is used as written; the side-table ownership question (hiring context / geography / liveness rows shared by several employers) is decided first |
| Relevance-verification stage | **1–2 weeks** | public sources only; a deterministic evidence rule plus one cheap model call, built to the `geo_verify` pattern already in the codebase |
| Stale-contact lifecycle (capture date, re-verify, expiry) | **3–5 days** | reuses the `JobGeography` backoff/expiry shape rather than inventing one |
| Confidence + evidence display, end to end | **3–5 days** | extends `hiring_context`'s evidence classes rather than adding a second scheme |
| Licensed-source integration | **2–4 weeks, or not at all** | **highly uncertain** — depends entirely on which vendor, their terms and their coverage. Could be zero if the feature stays public-snippet-only. |
| Telemetry soak + readout | **30 days elapsed**, ~2 days work | needs `SERPAPI_KEY` set and real traffic; elapsed time cannot be compressed |

**Total: roughly 4–8 engineering weeks excluding the licensed source**, and the
30-day soak runs in parallel with none of it. That figure carries one dominant
risk which no amount of engineering removes: **if employers do not publish these
names and no lawful source supplies them, the feature cannot be built at any
price.** Three samples of 66 postings currently point that way, and the next
decision should be whether a licensed source changes that — not how fast the
remaining code can be written.
