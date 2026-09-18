# Location eligibility for new postings

*Shipped 2026-09-18. Scope: postings first seen after this deployed. Nothing
here touches, re-fetches or re-scores rows that predate it.*

## The problem it fixes

The 2026-09-17 audit found 7 of 15 opened cards in the wrong country. Each
door into a user's pool ran a string comparison over whatever `Job.location`
happened to hold, and the adapters filled that string differently:

| adapter | what it stored | what the source actually said |
|---|---|---|
| Teamtailor | the RSS title suffix ("POS Integrations") | nothing in the feed; Paris on the page |
| Lever | `allLocations[0]` only | every site, `workplaceType`, `country` |
| Ashby | sites capped at "+N more" | 28 sites, one of them Austin |
| RemoteOK / WWR / Jobicy | "Remote" | "United States", "Europe Only", "USA" |
| Personio / BambooHR | remote from `schedule` / the title | neither is a location field |

"Unknown is kept" then admitted every posting with no country and Tier-1 paid
to reject them.

## The design

```
scrape ──► RawJob.geo (structured evidence, untruncated)
              │
   shared _upsert (NEW keys only) ──► geo_verify.derive()  [step A]
              │                            │
              │                     JobGeography row, keyed (source, external_id)
              ▼                            │  status: resolved | unknown | conflict
   per-user _upsert ──► eligibility.decide(geography, GeoPrefs)
              │            ELIGIBLE   → Job.eligibility="eligible"
              │            INELIGIBLE → dropped at the door (or stamped 10.0 later)
              │            UNKNOWN    → Job.eligibility="unknown"  (HELD)
              ▼
   scoring cycle ──► geo_verify.verify_pending()  [steps B, C], bounded
                        page / ATS detail → JSON-LD JobPosting
                        excerpts → one model call, quote validated
                        → redecide_copies() for every user copy
```

**One decision.** `app/common/eligibility.py` is pure and every door reads
it: intake (`pipeline._upsert`), adoption and the pulse per-user route (same
function), retrieval (`matcher.search_for_resume` excludes held copies),
all three scorers (Phase 1 backstop), and `slate.place()` (refuses
`ineligible` and, while `GEO_HOLD_UNRESOLVED=1`, `unknown`). A fit score never
overrides it.

**Evidence, never inference.** "Remote" and "Homeoffice" establish no
country. A department, a board's home country, an HQ or a currency is not
evidence. A posting with no evidence is UNKNOWN and stays UNKNOWN until the
page or the text says otherwise.

**Remote is not borderless.** A remote role anchored abroad, or restricted to
a region the user is outside, is INELIGIBLE. An explicit restriction the user
satisfies ("US only") is what makes a remote role ELIGIBLE.

**Country is not the whole answer.** Sponsorship and work authorization stay
their own checks. An on-site or hybrid role outside the user's home area is
INELIGIBLE when `open_to_relocation` is off; when the work mode is not stated
the country check decides and the scorer sees the city.

**Once per posting.** `JobGeography` is keyed by `(source, external_id)`, the
identity per-user copies preserve. Unresolved results are cached too, with
`attempts` and `next_attempt_at` (retry_hours × 2^attempts, stop at
`GEO_VERIFY_MAX_ATTEMPTS`). `location_hash` covers the sites, the structured
country, the work mode, the restriction field and the description hash; a
later sighting with a different hash re-derives the row and re-decides every
copy. A re-derivation that finds nothing does not undo a page- or
model-verified result.

## The verification sequence and its bounds

| step | what | cost | bound |
|---|---|---|---|
| A | structured fields + description restriction scan | CPU | new/changed keys only |
| B | official ATS detail (Greenhouse, Lever) or the posting page's JSON-LD | one GET | `GEO_VERIFY_FETCH_TIMEOUT_SECONDS`, SSRF-guarded, direct-ATS URLs only, never LinkedIn/Indeed |
| C | one extraction call on the cheapest configured model | ~1.2k in / 120 out tokens | only when location text exists the rules could not read; `GEO_VERIFY_LLM_DAILY_CAP`; `GEO_VERIFY_LLM_MAX_CHARS` of excerpts, never the whole posting, never a résumé |

The sweep runs inside the scoring cycle, after the provider/budget fast-exit
guards and before the work list is built, and stops at
`GEO_VERIFY_MAX_PER_CYCLE` postings, `GEO_VERIFY_BUDGET_SECONDS_PER_CYCLE`
seconds, or one fetch-plus-model timeout before the cycle deadline. Provider
breakers and the platform LLM budget are honoured before any call. The model
must return `evidence_quote`; a quote not found in the supplied excerpts
discards the answer (`rejected_quote`) and the posting stays UNKNOWN. Spend
is metered under `kind=geo_verify`, user `__shared__`.

Three rules keep the sweep from becoming a per-tick bill:

* a row is **claimed** (`next_attempt_at` pushed one interval out) before any
  network work, so a failed outcome write cannot re-fetch it every 90 s;
* a **platform-level** skip (daily cap, platform budget, every provider in
  cooldown) defers the row without spending one of its attempts — a capped
  day never retires the postings it could not reach;
* a due row **nobody is waiting on** (no open, unscored, held copy) is pushed
  out of the due window instead of occupying its head forever; admitting a
  new unknown copy (`mark_held`) makes it due again at once.

## Operating it

* `GEO_VERIFY_ENABLED=0` — no geography rows, no verdicts; every door falls
  back to the string gate.
* `GEO_HOLD_UNRESOLVED=0` — restore "unknown is kept" (scored, deliverable)
  while keeping INELIGIBLE stamps and verification.
* `python scripts/geo_verification_report.py --days 7` — resolution mix,
  evidence sources, verification delay, cost per new posting and per delivered
  eligible job, and how many INELIGIBLE verdicts users shortlisted manually
  (the cheapest accuracy signal until a labelled sample exists).
* The scoring cycle logs `Geo verification: {...}` once per cycle and carries
  `geo_*` keys in its FunnelEvent stats (`/api/admin/health` → lanes).
* The Job Explorer shows "Location pending" / "Location filtered" with the
  sentence behind the verdict; `/api/jobs` carries `eligibility` and
  `eligibility_reason`.

## Known limitations

* An application already SHORTLISTED when a posting's evidence later turns
  INELIGIBLE is not removed; the copy carries the verdict and nothing new is
  delivered from it.
* `Job.location` on a per-user copy is refreshed only when that copy is
  re-seen by the door that wrote it; the shared row and the geography are the
  truth.
* Step B trusts schema.org JSON-LD as the page states it; a board that embeds
  a wrong `jobLocation` is wrong here too. Nothing overrides a conflict
  automatically.
* Step C's accuracy is unmeasured in production. Until a labelled sample
  exists, watch `ineligible_shortlisted_by_user` and `held_past_scoring_window`
  in the report.
