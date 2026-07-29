# Autofill Next-Gen Roadmap

> Synthesis of three inputs: a code-level audit of the current extension + Playwright agent,
> the "G1/G2 layered-recovery" architecture proposal, and the "topological embeddings /
> Gen-3 vision" proposal. This document keeps the ~20% of those designs that pays off at
> SpotApply's scale (10→10k users, see SCALING.md) and defers or rejects the rest with
> explicit unlock triggers. Written 2026-07-29.

## 0. Scope & principles

- **The copilot stays a copilot.** Human reviews every page and clicks Submit
  (CLAUDE.md compliance stance). The human is the free, safe bottom layer of the
  recovery pyramid — design around that, don't design it away.
- **Cache meanings, not selectors.** The current system already keys on semantic field
  signatures (label ⊕ aria ⊕ name ⊕ placeholder, `content.js getFieldSignature`), which
  is immune to dynamic IDs and most DOM churn. Keep that substrate; add layers to it.
- **Mappings are global, answers are private.** A `(host, field-signature) → slot`
  mapping contains zero user data and can be shared across all tenants immediately —
  no k-anonymity/DP/OHTTP ceremony needed, because SpotApply's server is first-party
  and already trusted with the résumé and application list. Answers stay per-user
  (`AnswerMemory.user_id` scoping, already enforced).
- **LLM once per novel *shape*, globally — never per user, never per field at runtime.**
  Same cost basis as the existing essay flow (~$0.002, cached forever).
- **Compliance red lines (unchanged):** no LinkedIn/Indeed automation, no hidden ATS
  backend-API submission, no auto-submit. MV3: remote **data** is legal, remote **code**
  (JS/WASM fetched at runtime) is not.

## 1. North-star metrics (built in Phase 0, gate everything after)

| Metric | Definition | Source |
|---|---|---|
| Assist rate | filled / (filled + needUser) per page | counts already computed in `fillCurrentPage` (content.js ~:2032), currently discarded |
| **Zero-touch rate** | % pages where user typed 0 fields (review-only) | page telemetry + observeField |
| **Correction rate** | user edited a field WE filled, per slot × host | new `hirepathAutofilled` marker + input listener |
| Novel-field rate | signatures with no mapping, per day | recall/mapping lookups |
| Silent-error estimate | mismatch rate in the randomized audit slice | Phase 3 audit overlay |
| Teacher spend | LLM $ per 1k pages | Phase 4 logging |

Everything is keyed by `(host, ats_family, rules_version)` so regressions are attributable.

## 2. Phases

### Phase 0 — Measure (1–2 days)
1. `reportTelemetry(pack, 'page_filled', {host, filled, needUser, platform})` in
   `runCopilotStep` after `fillCurrentPage`. Endpoint exists (`/api/extension/telemetry`
   → FunnelEvent, server.py:3837).
2. Mark auto-filled fields (`el.dataset.hirepathAutofilled = '1'` in `fillInput`); when a
   user later edits one, emit `field_corrected` with the signature hash. (Today
   `hirepathUserModified` fires on *any* input — it can't distinguish corrections.)
3. Minimal per-ATS fill-rate/correction-rate view (SQL over FunnelEvent; analytics/
   funnel.py has the pattern).

**Acceptance:** baseline dashboard exists; we know today's assist rate per ATS.

### Phase 1 — Learn from humans: value back-inference + slots (2–4 days)
The highest-leverage idea across all three docs, and we're 80% there: `observeField`
(content.js:1391) already saves user-typed answers on blur; the fill pack already holds
email/phone/name/URLs/location/salary client-side.

1. **Slot registry v0 (~50 slots, not 400)** — see §5. Scope prefix (`applicant.` /
   `emergency_contact.` / `reference.` / `previous_employer.`) is part of the slot.
2. In `observeField.save()`: before POSTing, compare the typed value against pack fields
   (email exact; phone last-10-digits; names; URL host+path; city/state; salary numeric).
   On match, include `inferred_slot` + `match_kind` in the `/api/save-answer` payload.
   Free, instant, language-independent labeling — works on a Japanese form with zero
   dictionary work.
3. **New global table `FieldMapping`** (§6) — upserted by save-answer; NOT per-user.
4. Recall path returns mappings alongside answers: slot-mapped identity fields fill from
   the **pack (profile ref)**, not from a stored answer string. Volatile slots
   (years_experience, notice_period, salary) are **derived at fill time, never stored** —
   this quietly fixes a real staleness bug in today's AnswerMemory.

**Acceptance:** % of unknown fields auto-labeled by back-inference (expect majority);
mappings visibly accumulating per host.

### Phase 2 — Rules as data, OTA (3–5 days)
1. Serve a versioned JSON rules blob with the fill pack (or `GET /api/fill-rules?host=`
   with ETag): `[{host_pattern, signature_pattern|hash, slot, hints}]` + `version` +
   `killswitch`. The extension interprets it and merges with built-in regexes (server
   rules win). JSON data + shipped interpreter = MV3-legal. **Do NOT fetch WASM/JS**
   (the "OTA WASM payload" idea violates Chrome Web Store remote-code policy).
2. Founder-only admin route to hot-add a rule.
3. Fire drill: break-fix one ATS mapping end-to-end and time it. Target MTTR: minutes,
   zero Chrome review. (Today a Greenhouse redesign requires an extension release.)

**Acceptance:** demonstrated hotfix without shipping the extension.

### Phase 3 — Kill silent wrong fills structurally (4–6 days)
1. **Section scoping** in `fillUniversal` + recall: derive section context (nearest
   fieldset legend / preceding heading / aria-group) per field; include it in the
   signature AND as a hard mask — `applicant.*` identity slots are blocked inside
   sections matching /emergency|reference|previous employer|guarantor/. The
   "emergency-contact first name" failure becomes unrepresentable, not just unlikely.
2. **Type validation before fill** (email/URL/numeric/date sanity) + a cross-check that
   warns when an identity value (user's email/phone/name) lands under a signature whose
   type doesn't match.
3. **Severity tiers:** work-auth/sponsorship/salary/EEO/veteran/disability are
   always-confirm — highlighted for explicit user confirmation even when auto-filled,
   first encounter per company. Getting `middle_name` wrong costs nothing; getting
   `requires_sponsorship` wrong costs the job.
4. **`options_sig`:** add a sorted-option-set hash to select/button signatures — a select
   whose options are {male, female, non-binary, decline} is EEO gender in any language.
5. **Audit slice:** on ~5% of pages, the overlay asks the user to explicitly confirm 1–2
   randomly chosen auto-filled fields → `audit_result` telemetry. The only unbiased
   silent-error estimator (you can't measure silent errors from data the system itself
   generated), and it doubles as exploration data that keeps the learning loop honest
   as coverage rises.

**Acceptance:** correction rate on critical slots ≈ 0; silent-error baseline measured.

### Phase 4 — Teacher LLM per novel shape, cached globally (2–3 days)
1. After Phases 1–3, batch the still-unknown signatures of a page (+ section context +
   slot list) into ONE Haiku call, keyed by page shape (host + fields-hash), server-side,
   async — results land in `FieldMapping` for the next encounter (optionally a ~2s
   follow-up fill pass on the same page). Never blocks the fill.
2. Budget guards: reuse the `llm_budget_exhausted()` pattern; daily cap; log teacher $
   per 1k pages (expect pennies).
3. Trust score per mapping (§7); thresholds by severity.

**Acceptance:** novel-field rate declines week-over-week; teacher spend ≈ pennies/1k.

### Phase 5 — Better recall: server-side embeddings + trust metadata (3–4 days)
1. Replace the client-side `keywordOverlap > 0.4` fuzzy match (content.js:1482 — the
   remaining mis-recall hazard) with MiniLM cosine similarity inside
   `/api/recall-answers`. Reuse `matcher._get_embed_model()` — NEVER a second
   SentenceTransformer instance (MEMORY.md discipline).
2. Essay intent clustering: embed normalized questions, match to nearest cached intent —
   raises the `{company}`-template hit rate; French/German phrasings of "why do you want
   to join us" land in the same cluster for free. Answers stay per-user.
3. `AnswerMemory` gains `source` (user|llm|back_inferred), `verified_count`, decay class.
   User-typed beats LLM-generated on collision; volatile classes expire.

**Acceptance:** recall hit-rate up, corrections on recalled fields down.

**Total: roughly 3–4 focused weeks for Phases 0–5.** Order matters: 0 first (baseline),
1 feeds 4 and 5.

## 3. Deferred bets — with unlock triggers

| Bet | Build when (trigger) |
|---|---|
| Local distilled encoder (4–10MB, WASM) | teacher spend > ~$200/mo OR novel-field rate stays >20% at 5k+ users |
| Vision/pixel differential check | telemetry shows meaningful traffic on canvas/obfuscated portals; run server-side via browser-service, not client OCR |
| Extension/Playwright unification (shared DSL interpreter in agent.py) | multi-user server-side autofill becomes a real roadmap item (today founder-only) |
| Cross-user consensus ceremony (k-anon/DP/OHTTP/PIR/P2P) | only if mappings are ever shared outside SpotApply's first-party server (currently: never) |
| Structured-apply integrations (Greenhouse Job Board API, SmartRecruiters Apply API) | where the employer/board enables it AND ToS permits third-party submission — watch, don't bet |
| Full ontology growth past ~80 slots | the `unknown` bucket dominates teacher output |

## 4. Do-not-build list

1. **OTA WASM / remote code** — MV3 policy violation. JSON data + shipped interpreter only.
2. **Client-side MiniLM "vector vault"** — server already runs MiniLM; local-first privacy
   isn't our trust model (we generate the résumé server-side).
3. **P2P consensus / DP noise / OHTTP / PIR** — defenses for poisoning and metadata-leak
   threat models we don't have as a first-party product.
4. **Hidden-API machine-to-machine submission** ("identity wallet negotiating with the
   ATS backend directly") — ToS violation, ban risk for users, against CLAUDE.md
   compliance. Hard no.
5. **Per-field runtime LLM mapping calls** — teacher is per-shape, global, cached, async.
6. **400+-slot ontology upfront** — start ~50, grow from back-inference + teacher output.
7. **Vision-first VLA agent** — pixels are a *verification* fallback, not the substrate,
   while DOM+a11y works and a human reviews every page.
8. **Hand-maintained multilingual dictionaries** — back-inference + embeddings make them
   obsolete before they're finished.

## 5. Slot registry v0 (~50)

Scopes: `applicant | emergency_contact | reference | previous_employer` (prefix).

- identity: first_name, last_name, full_name, email, phone, address_line1,
  address_line2 (never auto-fill), city, state, zip, country
- links: linkedin, github, portfolio, website
- work: current_title, current_company, years_experience (derived), salary_expectation
  (volatile), notice_period (derived/volatile), available_start_date
- authorization [Critical]: work_authorized, requires_sponsorship, visa_status
- eeo [Critical, always-confirm]: gender, ethnicity, veteran_status, disability_status
- education[]: school, degree, field_of_study, start, end, gpa
- employment[]: company, title, start, end, description
- meta: cover_letter, resume_file, referral_source, how_heard
- essay: intent-cluster bucket (Phase 5)
- unknown

## 6. Data model changes

```python
class FieldMapping(SQLModel, table=True):
    """Global (cross-tenant) field-signature → slot mapping. Contains NO user data."""
    __table_args__ = (UniqueConstraint("host", "signature_hash"),)
    id: Optional[int]
    host: str                    # indexed
    signature_hash: str          # indexed; hash of normalized signature + options_sig
    signature_text: str          # for debugging/teacher context
    section_scope: Optional[str] # applicant | emergency_contact | ...
    slot: str                    # SlotURI from §5
    source: str                  # back_inference | teacher_llm | rule | manual
    votes: int = 0
    conflicts: int = 0
    confidence: float = 0.0      # §7, recomputed on write
    first_seen: datetime
    last_seen: datetime
    last_verified: Optional[datetime]
```

`AnswerMemory` additions: `source: str`, `verified_count: int`, `decay_class: str`.

## 7. Confidence & trust

```
trust = source_prior × votes/(votes + 3) × exp(-Δt / τ_slot)
source_prior: user_verified 1.0 | back_inferred 0.9 | teacher_llm 0.6 | rule 0.95
τ_slot: identity ~5y | address ~1y | employment ~6mo | salary ~3mo | derived → n/a (recomputed)
thresholds θ: trivial 0.55 | normal 0.75 | high 0.90 | critical → always-confirm regardless
```

Below θ → fill provisionally + yellow highlight, or defer to human (current UX). A
conflicting vote on a Critical slot never auto-promotes — it routes to the teacher.

## 8. Known open risks

- **GENERATE-channel injection:** the essay flow necessarily reads the question text from
  the page into an LLM prompt (`/api/answer-question` → `_llm_essay_answer`). No design
  fully closes this; mitigate with defensive prompt templating, treating question text
  strictly as data, and the human review the copilot already guarantees. (The "prompt
  injection is structurally impossible" claim in the G1 doc does not survive its own
  GENERATE op.)
- **Feedback collapse at high coverage:** when autofill covers most fields, humans stop
  typing and the label supply degrades to self-confirmation. The Phase-3 audit slice is
  the cheap standing insurance; revisit exploration budget when zero-touch rate > ~70%.
- **English-only signal regexes:** back-inference (Phase 1) and embeddings (Phase 5)
  handle non-English organically; do not build dictionaries in the meantime.
