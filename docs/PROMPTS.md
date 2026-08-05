# Scoring prompts — current, proposed, and why

> **2026-08-04 UPDATE — the audited revisions are now APPLIED in code.** The
> extension audit (gate eval N=200, A/B eval N=100, both live) confirmed the
> defects this file predicted and found their live cost: a Claude-72 job
> prescored 25 under the old two-band Tier-1 prompt; 87/200 prescores piled at
> 30-39 with zero in 60-69. What shipped, as one change set:
>
> - **Tier-1**: banded prompt (0-30 stated blocker / 40-59 adjacent / 60+
>   genuine), scoped lean-high, "never infer a blocker", authorized-to-work
>   clause, injection guard — `reranker._prescore_system_prompt`
> - **Gate**: `PRESCORE_ADVANCE_THRESHOLD` 60 → **40** (bottom of the adjacent
>   band). The prompt and gate ship TOGETHER: under the old prompt adjacent
>   jobs scored 60+ and advanced; under the new one they score 40-59, so a 60
>   gate would have turned false highs into permanent false lows. Re-derive
>   the gate from `--mode gate` at N≥2000 once the new prompt has traffic.
> - **Tier-2**: bounded contract (reason≤20w, concerns 0-3×≤8w with
>   specificity, notes≤8w), deterministic blocker cap (factor ≤15 from an
>   explicit blocker → overall ≤25), English-always, degenerate-shape rule,
>   "use 90+" permission — `reranker._JSON_CONTRACT` / `_SCORE_BANDS`
> - **Pipeline**: `_jd_slice` rescues work-authorization lines the JD `[:5000]`
>   cut dropped (US postings put them at the end — the truncation was deleting
>   exactly the work_auth factor's evidence and scoring the silence as
>   favorable)
> - **Regression harness**: `python -m scripts.eval_scorers --mode regress`
>   runs the audit's 20 fixed pairs (tests/regress_pairs.json) through the
>   LIVE prompts (~$0.10) — run it before and after ANY prompt edit.
>
> **2026-08-04 hotfixes after the live regress run (34/40 T1, 32/40 T2):**
> R1 — "onsite/hybrid outside {country}" pattern-matched the word "hybrid"
> alone, scoring a clean in-country hybrid job 20; reworded to "a work location
> outside {country} (onsite or hybrid there)". R2 — an empty JD scored 0;
> added "a posting with no usable description is not a blocker — score it 60".
> R3 — the fixture now carries PER-TIER ground truth (gt_t1/gt_t2): an empty
> JD must advance past the $0.0002 prescreen but must NOT be boarded by the
> authoritative scorer, so scoring both tiers against one GT column made the
> tool cry wolf. `--samples 3` scores each pair three times and takes the
> modal band, so run-to-run variance stops looking deterministic.
>
> **2026-08-04 v3 (structure, not nudging):** the v2 rewordings did NOT clear
> the live regress — T7 and T15 held at exactly 30 across all 3 samples.
> Lessons, both now pinned as guard tests in tests/test_cost_guards.py:
> a parenthetical does not stop an 8B-class model from keyword-matching
> ("(onsite or hybrid there)" still blocked an in-country hybrid job — the
> word must LEAVE the blocker line; an explicit "jobs based in {country} are
> never location blockers" immunity line replaces it), and rule placement
> beats rule content (the empty-JD rescue appended after the "never raise
> above 30" fence lost to the nearer numeric anchor — it is now its own
> band-level bullet, "score exactly 60", listed with the bands). Pass bar for
> the rerun: T7 modal >=60, T15 modal =60, Tier-1 >=38/40, blockers stay 8/8
> (the immunity line must not soften T1-T4/T16).
>
> Sections below this banner describe the PRE-audit state and the reasoning
> that led here; they are kept as the design record.

This file exists so the scoring prompts can be reviewed and iterated OUTSIDE the
code (paste into Claude / an extension / an eval harness). The authoritative
copies live in `app/matching/reranker.py` — if you change one here, change it
there, and re-run `python -m scripts.eval_scorers --mode gate` to measure the
effect before trusting it.

---

## 1. Tier-1 prescore (gpt-4o-mini) — CURRENT

**System** (`_prescore_system_prompt`, profile-aware variant):

```
You are a fast first-pass job-fit filter. Return ONLY a JSON object, no prose,
no markdown: {"score": <0-100 integer overall fit>, "reason": "<max 15 words>"}
Candidate targets: {roles}. Core skills: {skills}. ~{yoe} years. Wants jobs in
{country} (or fully-remote roles open to {country}).{sponsor_note}
Score 0-100 how well THIS candidate fits the job. A hard blocker (onsite in a
different country, explicit no-sponsorship when needed, or an unrelated field)
scores 0-30. Genuine skill/role overlap with no blocker scores 60+. When
unsure, lean HIGHER — a stronger model re-checks every promising job, so only
clear misfits should score low.
```

**User** (`_build_prescore_prompt`): résumé `[:4000]` first (static prefix →
OpenAI auto-caching), then title/company/location/remote + JD `[:1800]`.

### Why a bare ">=75 → MATCH / NO_MATCH" output is NOT the right change

The ask was: make Tier-1 output only match/not-match at a 75 bar to save output
tokens. Three problems, one economic and two structural:

1. **The saving is ~2% of the call.** Output is ~35 tokens of a ~1,700-token
   call, and gpt-4o-mini output is $0.60/MTok — the whole reason field costs
   ~$0.00002. Binarising the output saves nothing measurable.
2. **A binary answer destroys the drain.** Drained jobs are stamped with their
   numeric prescore so they exit the unscored corpus AND so the score is
   auditable ("Pre-screened (Tier-1 fit 41): wrong stack"). MATCH/NO_MATCH
   throws away the number that the whole backlog-drain mechanism stores, and
   makes gate tuning permanently impossible — you can't re-cut a threshold you
   never recorded.
3. **The current prompt is calibrated to LEAN HIGH on purpose** ("when unsure,
   lean HIGHER"). Asking the same model "is this >=75?" against that calibration
   yields an inflated yes-rate; you would be moving the bar and the calibration
   at the same time and could not attribute the change.

**What actually reduces Tier-1 cost:** nothing worth doing — at ~$0.0002/call
Tier-1 is 6% of scoring spend. The lever is how many jobs reach Tier-2, which
is the GATE, and the gate should be chosen from the (prescore, final) pairs the
`Job.prescore` column now records. Run:

```
python -m scripts.eval_scorers --mode gate --limit 200 --user <uid>
```

and read `p05_prescore_among_strong`: a gate at that value keeps ~95% of strong
matches. THAT is how ">=75" gets validated or rejected — not by prompt edit.

### Proposed Tier-1 revision (small, safe)

Keep the numeric contract. One targeted change: the "lean HIGHER" instruction
currently applies to every uncertainty; scope it to borderline fits so hard
blockers stay low even when the model is unsure:

```
Score 0-100 how well THIS candidate fits the job.
- Hard blocker (onsite in a different country, explicit no-sponsorship when
  needed, unrelated field): 0-30, even if other signals look good.
- Adjacent-but-plausible (overlapping stack, one seniority step away): 45-65.
- Genuine role + stack match with no blocker: 70+.
When torn between two bands, choose the higher — a stronger model re-checks
everything that advances. Never raise a hard blocker out of 0-30.
```

Rationale: adds an explicit middle band (the current prompt jumps 30→60, which
clusters borderline jobs right at the old gate), and fences the lean-high rule
away from blockers. Validate with the gate eval before/after; expect the
prescore distribution to spread, which makes any gate sharper.

---

## 2. Tier-2 final (Haiku 4.5) — CURRENT

**System block 1** — rubric (`_get_system_prompt`): candidate targets, YoE,
skills, country/visa posture, `_SCORE_BANDS` (the 0-100 bands + "use the full
range" calibration), and the JSON contract below. ~2.9-3.5K chars.

**System block 2** — résumé (`_resume_context_block`): full résumé + revealed-
preference feedback, PADDED to >=15,500 chars to clear Haiku's 4,096-token
cache minimum. Both blocks carry `cache_control` and are written once per
user/cycle by `prewarm_cache`.

**User**: title/company/location/remote/sponsor-note + JD `[:5000]`.

**Contract** (`_JSON_CONTRACT`):

```json
{"score": <0-100>, "reason": "<one sentence, max 25 words>",
 "concerns": ["<concern 1>", "<concern 2>"],
 "breakdown": {"skills": {"score": <0-100>, "note": "<short why>"},
               "experience": ..., "location": ..., "work_auth": ...}}
```

### On "one fixed prompt for each tier instead of separate"

If this means "stop building the prompt per-user": **no** — the per-user rubric
(roles/skills/YoE/visa) is what makes a score mean "fit for THIS person", and
it is already a *stable* prefix per user, so it caches perfectly. A truly fixed
prompt would have to push the profile into the uncached user message on every
call: quality down, cost UP.

If it means "the same wording template everywhere": **already true** — both
tiers have exactly one template each, parameterised by profile.

### On "JD as a card, grounding as a card, résumé+grounding share the cache"

Mapped to what the API actually prices:

| Piece | Where it belongs | Why |
|---|---|---|
| Rubric ("grounding" for scoring) | cached system block 1 | stable per user ✅ already |
| Résumé | cached system block 2 | stable per user ✅ already |
| JD ("card") | uncached user message | changes per job — caching it is impossible by definition ✅ already |

So the requested architecture IS the current architecture. The one real gap
found while reviewing it: the padding sits inside the résumé block, and the
tailoring/grounding calls (Sonnet path, `tailor.py`) do NOT share this prefix —
they build their own. Unifying scoring + tailoring around one cached résumé
block is real money if pre-tailoring ships (same prefix read by both flows),
but it belongs to the tailoring-merge change, not this one.

### Proposed Tier-2 revision (cost, no quality change)

`concerns` and the four breakdown `note` fields are ~45% of output tokens
(~300 → ~165). The notes render as small captions under the fit bars; concerns
render on the card. Keep both — they are the product per your own decision to
surface weaknesses — but bound them harder:

```json
{"score": <0-100>, "reason": "<max 20 words>",
 "concerns": ["<max 8 words>", "<max 8 words>"],
 "breakdown": {"skills": {"score": <0-100>, "note": "<max 6 words>"},
               "experience": {"score": <0-100>, "note": "<max 6 words>"},
               "location": {"score": <0-100>, "note": "<max 6 words>"},
               "work_auth": {"score": <0-100>, "note": "<max 6 words>"}}}
```

Expected: ~35% fewer output tokens ≈ $0.0005/final ≈ 15% off the per-final
cost, with the same information architecture. Validate by eyeballing 20 cards.

---

## 3. Office-hours polling — decision pending data

Do NOT hard-stop polling outside office hours: a 4:55pm posting would wait 15
hours, inverting the speed goal. If the posted-hour histogram (query in the
session notes) shows a real quiet window, stretch the pulse floor to 180 min in
that window instead of stopping. LLM cost outside office hours is already ~0
(no new jobs → no scoring); the only saving is HTTP/CPU.

---

## 4. Throughput per user per hour (why "jobs/hour" is the wrong unit)

Scoring is bursty, not hourly-continuous: the scoring lane drains each user's
queue within minutes of adoption, then idles. The steady-state constraint is
the PLAN CAP (finals/day), not machine throughput:

| Users | Machine ceiling (finals/hr, hourly cap 400) | What users actually get |
|---|---|---|
| 10    | 400/hr shared | plan cap: 50/day each (Pro) — drained in the first cycles after adoption |
| 100   | 400/hr shared | 100 × 50 = 5,000 wanted vs 5,000 backstop — exactly at the ceiling; raise backstop |
| 1,000 | 400/hr shared | needs backstop ≈ 50,000/day + hourly ≈ 2,100 + rate-limit tier raise + batch API |

At 1,000 users the single-process design (module-global budgets) is the
binding constraint before the LLM is — see docs/SCALING.md.
