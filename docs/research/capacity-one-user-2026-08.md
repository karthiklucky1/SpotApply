# One user, one arc — what the funnel actually costs today

*Aug 2026. Sizing for the CURRENT single active user, not a hypothetical 10 or 100.
Settings read from `app/config.py` / `app/db/models.py`; unit costs from `docs/CAPACITY.md`
§3 (whose §2–3 totals are the pre-per-plan-cap regime — the per-call costs still hold).
The two production inputs (inflow, plan) come from the Railway/DB reading done in the
outage session; this container has no DB or log access, so they are taken as given and
labelled where used.*

## 1. The funnel, per UTC day, for one user

```
on-role jobs adopted into the user's pool      ~135/day     [prod: 814 unscored over 6 days]
  ↓  ghost filter + rule/embedding/door gates — FREE, no LLM
reaching Tier-1 prescore                       ~108/day
  ↓  advance gate 40 (PRESCORE_ADVANCE_THRESHOLD)
Tier-2 Claude finals needed                    18–43/day    ← a = 0.17…0.40, see §4
  ↓  shortlist at rerank_score >= 60
shortlisted onto the board                     3–6/day
  ↓  board's own default filter (>= 65)
visible when the board opens                   2–5/day
```

The observed 5–6 shortlists/day during the stall sits at the bottom of that band; the
11–17/day on Aug 17–19 sits above it, because the 5-min matching lane scores without
consulting the per-user cap at all (`pipeline.py:751`).

## 2. What it costs — the number that decides everything

| Item | Volume/day | Unit | $/day | $/month |
|---|---|---|---|---|
| Tier-2 finals (Haiku 4.5, warm cache) | 43 | $0.0033 | $0.14 | **$4.30** |
| Tier-1 prescores on **OpenAI** (gpt-4o-mini) | 108 | $0.0002 | $0.02 | **$0.65** |
| Tier-1 prescores on **Anthropic** (fallback) | 108 | $0.00185 | $0.20 | **$6.00** |

PRO sells for **$10/month** (`PLAN_PRICES`). So at the current cap of 50 finals/day the
user costs **~$5/month in scoring** — half the subscription — before any tailoring
(Sonnet, and the only ceiling on it is `TAILOR_ABUSE_DAILY_CAP=150`).

**This is the whole decision.** The finals cap is not a technical limit, it is how much
of a $10 subscription you are willing to spend on scoring:

| finals_daily | scoring cost/user/month | share of a $10 PRO sub |
|---|---|---|
| 15 (FREE) | $1.50 | — |
| **50 (PRO today)** | **$5.00** | **50%** |
| 75 | $7.40 | 74% |
| 100 (AGENCY) | $9.90 | 99% |
| 150 | $14.85 | **loses money** |

## 3. Does 50/day actually serve this user?

Yes — with ~13% headroom, and that is the finding.

```
finals needed = on-role inflow × 0.8 (survive the free gates) × a (Tier-1 advance rate)
              = 135 × 0.8 × 0.40 = 43/day       (pessimistic a)
              = 135 × 0.8 × 0.17 = 18/day       (production-measured a)
cap = 50  ⇒  binds at 50 / (0.8 × 0.40) = ~156 on-role jobs/day
```

Today's inflow is ~135/day, i.e. **~87% of what the cap can serve** on the pessimistic
advance rate, and ~37% on the measured one. Nothing that deserves a final is being denied
one: the ~65/day the Tier-1 gate drains scored under 40 — stated-blocker or
wrong-profession jobs — and the age gate (`SCORING_MAX_JOB_AGE_DAYS=5`) clears anything
that ages out.

**So the cap was never the cause of the stall.** The cause was accounting: cheap prescores
were being billed as finals, so ~45 Tier-1 drains consumed a 50-final allowance in
15 minutes. That is fixed (`be62260`). The 814-job backlog would cost ~$1.00 total to
drain ($0.16 prescores + $0.86 finals) — affordable, but per the fresh-first product rule
it should simply expire at 5 days rather than be paid for.

## 4. The one number that is still an estimate

`a`, the Tier-1 → Tier-2 advance rate at gate 40 with the banded prompt. Production
measured **17%** reaching Claude across 335,867 stamped rows (20% ghost, 60% Tier-1
drained) — but at the OLD gate. The banded prompt scores adjacent-role jobs 40–59, so `a`
should have risen; 0.40 is my pessimistic bound, not a measurement.

Everything above holds across that whole range, which is why it does not need resolving
before deciding. If you want it exact: `scripts/eval_scorers.py --mode gate`.

## 5. The arc — decide once, then stop touching it

1. **PRO `finals_daily` stays 50.** It covers today's inflow with headroom and costs half
   the subscription. Raising it does not produce more good jobs — it produces more finals
   on jobs Tier-1 already judged unfit.
2. **No per-user cap on OpenAI prescores.** $0.65/user/month. There is no economic case
   for a cap, and a cap there is what turned a provider outage into a feed outage.
3. **Anthropic prescores keep counting toward the global backstop only** (`_register_final_call(None)`),
   never the user's plan allowance. At $0.00185 each they are 9× an OpenAI prescore, so
   the platform ceiling must still see them — the Jul-15 rule.
4. **Keep `SCORING_MAX_JOB_AGE_DAYS=5`.** It is the backlog release valve; without it every
   outage converts into permanent LLM debt.
5. **The rule for later, so this is not re-litigated:**
   `finals_daily ≈ 0.32 × (on-role jobs/day)`, equivalently one cap serves
   `cap ÷ 0.32` on-role jobs/day. Raise the cap only when inflow rises (more sources,
   wider roles) — and price it first: every +25 finals/day is +$2.50/user/month.
6. **Watch the real exposure, which is not scoring.** 150 tailors/day on Sonnet at
   ~$0.02–0.03 each is up to $3–4.50/day for ONE user — an order of magnitude above their
   whole scoring budget. If anything gets a tighter cap next, it is that.
