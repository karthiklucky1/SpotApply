# Temporary Pro for everyone, and the subscription audit

*Added 2026-09-25. Guards: `tests/test_temporary_pro.py`. Audit tool:
`scripts/audit_subscriptions.py` (read-only).*

## 1. What the switch is

One env flag, `TEMPORARY_PRO_FOR_ALL`, read through one function
(`billing.temporary_pro_active`), honoured by the one entitlement lookup every
backend gate and every background lane already resolves through
(`server._get_user_plan`, directly or via `common/plan_limits.plan_limit`).

```
TEMPORARY_PRO_FOR_ALL=1   →  every tenant resolves to PlanTier.PRO
TEMPORARY_PRO_FOR_ALL=0   →  every tenant returns to its own row / grandfathering
```

It writes **nothing**. No subscription row is created, changed or migrated, so
turning it off needs no transition — which is the entire reason it is safe to
turn on.

### What it grants

`PLAN_LIMITS[PRO]`: 35 jobs delivered/day, 35 tailored résumés/day, unlimited
auto-fill, 250 Tier-2 finals/day. **Pro allowances, not unlimited provider
spending** — Pro's own ceilings still apply, as do the platform backstops
(`LLM_DAILY_FINAL_CAP` 15,000/day, `LLM_HOURLY_FINAL_CAP` 2,000/hour) and
`TAILOR_ABUSE_DAILY_CAP`. Usage accounting (`UserUsage`) and the spend ledger
(`LlmSpend`) are unchanged.

### What it must never grant — and does not

`billing.is_paid_entitlement` is the line, and the flag is not on its side of it.
That function answers *is money changing hands?* and three things hang off it:

| consumer | why it matters |
| --- | --- |
| `/api/admin/metrics` → `mrr_usd`, `paid_subscriptions` | revenue reporting |
| `_user_paid_search_is_live` → the dormancy gate | who gets background LLM spend |
| the sandbox-vs-revenue split | test-mode rehearsals are not income |

Production has already been burned by the weaker test. Reading `plan != FREE` as
"paying" let **10 dormant accounts take 90.8% of a week's LLM spend** at the PRO
ceiling, and one test-mode subscription was reported as **$100 MRR**. If
temporary Pro leaked into `is_paid_entitlement`, every account would read as a
paying customer, the dormancy gate would stop applying to anyone, and MRR would
become a headcount. Three tests pin the separation, one of them structurally
(the function's source must not mention the flag).

So: an inactive user still goes dormant during the free month. No premium
background work is created for someone who has not opened the app.

### The copy

`billing.TEMPORARY_PRO_NOTICE` — *"Pro features are temporarily available to
everyone."* One string, rendered server-side on `/` and `/pricing` and returned
by `/api/billing/options` and `/api/usage`. **No end date** (inventing one is a
promise nobody has made, and a date that slips is worse than none) and **no
automatic enrolment** when it ends — `temporary_pro_status()` states both as
fields so no surface has to remember.

### Nothing is for sale while it is on

`purchase_available()` is False, and that closes every path, **server-side**:

* `POST /api/billing/checkout` → 503 before any Stripe call. Hiding a button is
  not closing a route; a stale page, a cached client or a bookmarked URL will
  POST to it.
* `payment_options()` reports `purchase_available: false` and withholds the
  manual bank-transfer details too — that is still someone sending money for
  something they already have.
* `/pricing` and `/` drop the price CTA and the "upgrade for higher caps" promise.

`POST /api/billing/portal` stays **open on purpose**. An existing subscriber must
keep reaching their own card, invoices and cancel button *precisely because* the
features are free this month; closing it would trap someone in a subscription
they can no longer see a reason for. The webhook is likewise untouched, so rows
keep syncing and a flag-off lands on accurate state.

### One defect fixed on the way

The dashboard's Plans card claimed **"Unlimited tailored resumes & cover
letters"** while the code enforced 35/day. `pricing.html` carries a comment about
exactly this drift ("this card said 'Unlimited' … while the code enforced
35/day"); the pricing page was corrected and the dashboard copy never was. Now
35, pinned against `PLAN_LIMITS` by test.

---

## 2. The $100/month recruiter-research offer

**There is no such offer in this codebase, and there never was one to pull.**

Verified across `app/`, `extension/`, `mobile/` and every template:

| what exists | price |
| --- | --- |
| `PLAN_PRICES[PRO]` — the one paid plan | $100/month |
| `/recruiter` portal + `/api/recruiter/register|search|intro` — a **recruiter-facing** product (recruiters finding candidates) | free, no checkout path |
| `intelligence/hiring_contacts.py` — hiring-context extraction inside the candidate product | not priced, not sold separately |

Every `$100` in the repository is the Pro plan price. The Pro feature list on
`/pricing` and `/` promises no recruiter research, and the landing FAQ already
says outright that tailoring "is not the same as a promise that any particular
system or recruiter will move you forward".

Rather than invent a removal, the absence is now **stated** in the payload every
upgrade surface already reads, which is what stops one being added by accident:

```json
"recruiter_research": {"available": false, "for_sale": false,
                       "status": "On hold while we improve matching quality"}
```

This matters because the five-job pilot measured what such an offer would have
been selling: of 45 live shortlisted jobs, **64.4%** yielded a department/team,
**2.2%** a named recruiter and **0/45** a named hiring manager. Phase 8 covers
the readiness work; nothing is to be sold before it.

---

## 3. Subscription audit — are current subscribers still charged?

**Run `python -m scripts.audit_subscriptions` where the database is reachable.**
It is read-only by construction: no `--apply`, no write path, no Stripe mutator
(pinned by test). It prints no user id, email, Stripe id or key — accounts appear
as 8-character fingerprints.

### The answer, stated as logic rather than as an assumption

`TEMPORARY_PRO_FOR_ALL` is **entitlement resolution on our side**. Charging
happens inside Stripe, driven by the Subscription objects Stripe holds. The flag
does not touch Stripe. Therefore:

| row kind | still charged? |
| --- | --- |
| Stripe-backed, key is `sk_live_` | **YES.** Stripe bills on its own schedule. Stopping it is a financial mutation. |
| Stripe-backed, key is `sk_test_` (sandbox) | **No real money.** Test invoices only; nobody is charged. The portal still shows a subscription. |
| Manual / bank activation (no Stripe ids) | **No recurring charge.** A one-off payment already made; nothing to stop. |
| `plan = FREE`, or expired | Nothing. |

### What this deployment is

Established earlier in this session from production logs: the deployed
`STRIPE_SECRET_KEY` is an **`sk_test_`** key — `stripe_live_mode()` False,
`stripe_mode()` `"test"`, `warn_if_stripe_test_mode` logging it at boot, and the
billing portal opening under *"Spotapply llc sandbox"*. Under a test key no live
payment can be taken, so **no subscriber is being charged real money today.**

I could not re-verify the row inventory from here: outbound access to
`api.supabase.com` and `app.spotapply.ai` is blocked in this environment (403 at
CONNECT). The counts are therefore **unknown but directly checkable**, by either:

* `python -m scripts.audit_subscriptions` — full per-row bucketing; or
* `GET /api/admin/metrics` — already reports `paid_subscriptions`, `mrr_usd`,
  `sandbox_subscriptions`, `grandfathered_users`, `subscriptions_missing_period_end`,
  `stripe_mode` and now `temporary_pro_for_all`, with no DB access needed.

### Transition proposal — FOR REVIEW, not executed

Nothing below has been done. No cancellation, refund, plan rewrite or Stripe call
was made, and no code in this change is capable of making one.

1. **Run the audit first.** Everything after this depends on the counts, and
   guessing them is how the 2026-09-19 investigation went wrong.
2. **If `stripe_live` is 0** (expected, given the test-mode key): there is nothing
   to transition. Leave every row alone. Turn the flag on; billing history is
   untouched and the portal still works. Revisit when live keys go in.
3. **If `stripe_live` is non-zero**, for each such subscriber, pick one — and the
   decision is the owner's, not mine:
   - *pause collection* in Stripe for the free period (keeps the subscription and
     its history, stops the invoice), or
   - *cancel at period end* and let them re-subscribe when paid plans reopen, or
   - *leave it and refund* each invoice raised during the free period.
   Whichever is chosen, tell the subscriber before the next invoice date, not
   after. Charging someone for a month in which the product was free to everyone
   else is the specific harm here.
4. **Fix `rows_missing_period_end` first, separately.** A NULL
   `current_period_end` reads as "never expires" to `entitlement_expired` — the
   founder's row sat like that for two weeks. `reconcile_subscriptions` (daily)
   repairs it; confirm it has run before drawing conclusions from any row.
5. **Do not rewrite billing history.** Whatever is decided, the existing rows and
   Stripe's own records are the audit trail.

### When the flag goes off

Each account returns to exactly what its own row and the grandfathering rules
already said — the audit prints that per row as `entitlement_if_flag_off`, and
the total as `would_be_pro_if_flag_off`. Nobody is enrolled in anything, nothing
is charged, and no notification promising otherwise is sent. Users who exceeded
Free limits during the free month keep everything they generated; only the
forward caps change.
