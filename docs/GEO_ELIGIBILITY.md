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

**Narrower than a country is narrower** (2026-09-18 review). "Candidates must
be based in California" is kept as a STATE restriction (`Geography.areas`,
`"united states/ca"`, or `"united states/tx/austin"` for a city), beside the
country it also names. A remote role binds the user's home state (and city,
when the rule names one); an on-site role binds it only for a user who will
not relocate. A profile that names no state — or no city, against a city
rule — is HELD (`area_restriction_unresolved`), never read as
anywhere-in-the-country. Only US states and cities are modelled.

**The remote preference is enforced.** With "Include remote roles" off
(`remote_ok=False`) a remote role is INELIGIBLE (`remote_not_wanted`) unless it
also offers an office in the user's own area, in which case it is that
office.

**Evidence is combined at every step, and a contradiction is retained.** The
page probe is derived from the fetched structured fields *together with* the
posting's text, exactly as intake derives a listing, so an official endpoint
restating a structured country cannot overturn a restriction the text states.
A CONFLICT row is never sent to the model (excerpts alone can only restate the
text's side) and is parked for a person (`next_attempt_at` a year out,
`conflict_retained`); it resolves only when a step's own combined evidence is
conflict-free — Lever later saying `country=DE` under "must be based in
Germany" does resolve it.

**The legacy string filters defer to the verdict.** `RuleFilter` and the
retrieval gate (`matcher._passes_legacy_country_gate`) skip their whole-string
country comparison for any row carrying `Job.eligibility` — "Remote · Tiranë,
Albania · Austin, TX" is ELIGIBLE for a US user site by site and Albanian to
the regex — and keep it for rows with no verdict. Seniority, sponsorship and
job type stay independent.

**Once per posting.** `JobGeography` is keyed by `(source, external_id)`, the
identity per-user copies preserve. Unresolved results are cached too, with
`attempts` and `next_attempt_at` (retry_hours × 2^attempts, stop at
`GEO_VERIFY_MAX_ATTEMPTS`). `location_hash` covers the sites, the structured
country, the work mode, the restriction field and the description hash; a
later sighting with a different hash re-derives the row and re-decides every
copy. A re-derivation that finds nothing does not undo a page- or
model-verified result.

**A structured-only change reaches the door.** The shared `_upsert` used to
compare only the description hash and the display string, so Lever's
`country` moving US → GB under the same "Remote" and the same text was
"unchanged, recently seen — no DB work", and every copy kept its verdict.
The shared row now carries `Job.geo_hash` (`geo_verify.evidence_hash`: every
site, the country code, the workplace type, the restriction field, the display
string, the remote flag — everything `derive` reads except the text), the
prefetch compares it, and a move is a location-only update: two small columns
and a re-decision, never a description rewrite or a cleared embedding. Rows
written before the column existed (NULL) adopt the current evidence as their
baseline on the next stale touch — a write that was happening anyway — and are
NOT re-derived; the next move is what becomes visible. The pulse lane's
parsed-list board signature (`_board_signature`) hashes the same evidence, so
an unchanged listing with a moved country is not skipped as unchanged; the
listing-phase signature (N+1 adapters) stays evidence-free on purpose, since
that is detail-phase data there.

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
must return `evidence_quote`, and the quote has to earn the claim twice: it
must appear IN FULL in the supplied excerpts (a trimmed quote is a substring
and passes; a real 40-character opening with an invented tail does not —
`rejected_quote`), and it must itself NAME the place claimed — the country by
name, alias, city, US state or demonym ("within German borders" supports
Germany), a region by its anchor, "worldwide" by a worldwide phrase. A claim
the quote does not support is dropped; when none survives the answer is
discarded (`unsupported_quote`) and the posting stays UNKNOWN. "Our office
provides a comfortable and collaborative place to work" is verbatim and
supports nothing. Spend is metered under `kind=geo_verify`, user `__shared__`.

**The daily cap is a platform-wide number, so it lives in the database.**
`GEO_VERIFY_LLM_DAILY_CAP` is enforced through `platform_counter`
(`app/common/daily_counter.py`): one row per UTC day, and each call RESERVES a
unit with a conditional `UPDATE … WHERE count < cap` before it is made. It
survives deploys and is shared by every replica — the first version counted
in process memory, which reset on every restart and was private to each
process, so "400/day" was really 400 per process per uptime. A reservation
the database cannot record refuses the call (deferred as a platform skip, not
charged to the posting). The cap is a COUNT of calls; what it costs depends on
the model that serves them — read the ledger (`kind=geo_verify`,
`scripts/geo_verification_report.py`), not a per-call estimate.

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
* A profile edit (home location, "Include remote roles", relocation) does not
  re-decide the copies already in that user's pool; new sightings and
  verification outcomes do. A copy HELD on `area_restriction_unresolved` is
  released only when the posting is re-decided for another reason.
* Sub-national restrictions are modelled for US states and cities only; a
  restriction naming a non-US region below country level is an unresolved
  excerpt (a model call at most, otherwise UNKNOWN). "Georgia" alone is never
  read as a state.
* A CONFLICT row waits a year for a person; nothing surfaces it yet beyond
  `conflict_retained` in the cycle log and `status=conflict` in the report.
* The board signature carries location evidence only where the parsed list is
  the signature; an N+1 adapter's listing-phase signature does not, so a
  structured-only move on such a board is seen by the fresh/full lanes' full
  upsert, not by the pulse lane's unchanged-board skip.
