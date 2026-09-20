# Production checks after the September audit fixes

Sign in to https://app.spotapply.ai/dashboard with an email included in
`ADMIN_EMAILS`. Open the following links in the **same browser**. They return
JSON; no command line or copying API keys is required. If they return 403,
refresh the signed-in dashboard, then verify the admin email configuration.
Check `/api/admin/whoami` for `is_admin: true`.

| Check | URL | What to read |
|---|---|---|
| Operational health | https://app.spotapply.ai/api/admin/health | `providers`, `lanes`, `degraded` |
| Today's spend | https://app.spotapply.ai/api/admin/spend?days=1 | `metered_share`, `by_provider`, `top_users` |
| Seven-day spend | https://app.spotapply.ai/api/admin/spend?days=7 | Daily trend; includes older estimated rows |
| Billing and activity | https://app.spotapply.ai/api/admin/metrics | `stripe_mode`, `mrr_usd`, sandbox/unknown subscriptions, `active_users_7d` |
| Effective settings | https://app.spotapply.ai/api/admin/settings | Lane intervals, caps, thresholds, dormancy, configured credentials |
| Your feed freshness | https://app.spotapply.ai/api/freshness-stats | `degraded`, coverage/latency, overdue boards; scoped to the signed-in user |

## Reading the metrics correctly

- `metered_share` is a **fraction of recorded calls**, not a percentage of the
  provider invoice: `0.95` means 95%. New scoring calls should have known
  provider/model and metered token usage. Historical rows and tailoring can
  remain estimates. It does not prove that every provider call was recorded;
  the buffer remains best effort and a process crash/database write failure
  can lose unflushed entries. Compare the same UTC day with provider usage.
- In health, `providers.anthropic.configured` and `available` tell you whether
  a key is present and the breaker allows calls. `available: true` alone is
  **not** evidence of recovery. A remaining `down_since` means recovery is
  unconfirmed; check for successful Anthropic calls in spend and lane stats.
  Breakers and budget counters describe the process answering the request.
  With separate web/worker replicas, consult worker logs too.
- `lanes.scoring_cycle.at` and `lanes.pulse_tick.at` should advance while those
  lanes run. Compare their age with the configured interval plus the maximum
  cycle duration, rather than expecting a new timestamp every second. The
  latest stored event is not an active probe of the scheduler.
- For pulse, read `stats.board_cap`, `board_cap_next`, `selected`, `deferred`,
  `fetch_ok`, and `consumer_deadline_hit` together. Falling deferrals after the
  cap shrinks indicate adaptation. A cap stuck at the floor with repeated high
  deferrals and increasing overdue boards warrants DB/fetch investigation.
  `deferred / selected` is the deferral fraction when `selected > 0`.
- For expiry, inspect `expiry_owners_swept`, `expiry_owners_failed`, and
  `expiry_stopped`. The first two may be absent when zero. Repeated
  `statement_timeout` or owner failures need attention; a time-slice stop is
  different from a failed query. Zero expirations can simply mean no stale jobs.
- `degraded: true` or null counts mean unavailable data, **not zero jobs**.
  Freshness results are cached (five minutes healthy; one minute degraded).
- Billing mode describes the current deployment key. Subscription mode is
  stored separately as `user_subscription.stripe_livemode`:
  true = Stripe-confirmed live, false = sandbox, NULL = unverified legacy row.
  Watch `subscriptions_unverified_mode` and `subscriptions_missing_period_end`
  fall to zero after reconciliation. `mrr_usd` remains a plan-price estimate;
  validate real collections, discounts and refunds in Stripe.
- `active_users_7d` is authenticated activity. Background pipeline activity
  is reported separately and must not be counted as returning customers.

## Go-live sequence

1. Deploy the follow-up PR. Startup adds one nullable BOOLEAN column,
   `user_subscription.stripe_livemode`; it does not guess/backfill live payments.
   Confirm the Railway deployment SHA and successful CI.
2. **While the current test keys still work**, let billing maintenance reconcile
   the old sandbox rows. It starts about two minutes after boot when lanes are
   enabled, then follows `BILLING_RECONCILE_INTERVAL_HOURS` (default 24).
   Look for `Billing maintenance: reconcile` in Railway logs and confirm
   `subscriptions_unverified_mode = 0`. If keys have already changed, reconcile
   legacy subscriptions using their original Stripe environment; do not mark
   unknown rows live by hand. An unverified non-free subscription keeps the
   duplicate-checkout guard until its origin is resolved.
3. Resolve Anthropic billing if the reported suspension remains. Confirm a
   successful response after the breaker cooldown, rather than just the
   payment receipt. The historical $0.02 balance has not been re-verified here.
4. In Stripe, finish account activation. Set the matching live
   `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID_PRO`, and `STRIPE_WEBHOOK_SECRET`.
   Use the application's account, with endpoint
   `https://app.spotapply.ai/api/billing/webhook`, subscribed to:
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`, and
   `invoice.paid`. `invoice.payment_failed` is also handled for diagnostics.
   Verify deliveries return 2xx and that period ends update.
5. Make one intentional live checkout only when ready to pay. Verify plan,
   period end, live mode, and the Manage/cancel portal. A sandbox subscription
   does not turn into a real one when keys change; it needs a new live checkout.
   The follow-up allows that checkout without reusing sandbox customer IDs.
6. Set `PLAN_GRANDFATHER_UNTIL` to the intended signup cutoff. Leaving it blank
   grants existing-profile users complimentary Pro indefinitely. It is a
   signup cutoff, **not** the date all old complimentary plans expire.
7. Check metrics after several lane cycles and compare another full UTC day.
   Use Supabase's actual CPU, Disk IO and query latency before buying capacity.
   Verify newly fetched Ashby locations; historical blank locations were not
   repaired by these changes. Avoid an unbounded production backfill.

## Verification scope

The follow-up adds regression coverage for lost webhook retries, late checkout
events, sandbox-to-live transitions, concurrent spend writes, UTC midnight,
ambiguous auth errors, paginated/nested storage, and deletion rollback/retries.
No live customer deletion, payment, database backfill, or credential changes are
part of validation. SQLite tests do not validate live Supabase query plans.

Primary references: [Stripe webhook delivery and retries](https://docs.stripe.com/webhooks),
[Supabase Auth error codes](https://supabase.com/docs/guides/auth/debugging/error-codes),
[Supabase Storage pagination](https://supabase.com/docs/reference/python/storage-from-list).
