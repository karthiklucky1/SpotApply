-- Read-only diagnosis of a scoring cycle that reports `scored: 0`.
--
-- WHY THIS FILE EXISTS. `/api/admin/health` surfaces the last scoring cycle's
-- stats dict (scoring_lane.py:1180) and that dict records how MANY users were
-- stopped short (`plan_capped_users`) but never WHICH stop fired. The reason
-- string is built in finals_budget.allowance() and then logged at DEBUG, per
-- user (scoring_lane.py:1319) — so in production the one number you need is the
-- one number you cannot read. These queries recompute the decision from the
-- same rows the lane reads, so the stop can be named without a deploy.
--
-- EVERY STATEMENT IS A SELECT. Safe against production. Set the user first:
--
--     \set uid 'auth-uuid-here'
--
-- or replace :'uid' inline. To find it from an address:
--     SELECT user_id, email FROM userprofile WHERE lower(email) = lower('...');
--
-- All day boundaries are UTC midnight, matching finals_budget._utc_day().
--
-- ENUM LITERALS ARE UPPERCASE ON PURPOSE. SQLAlchemy persists and compares Enum
-- columns by the member NAME ('SHORTLISTED', 'FREE'), not the value
-- ('shortlisted', 'free') — init_db.py:224 says so, and the pg enum labels are
-- created from the names. Lowercasing these silently returns zero rows.


-- ─────────────────────────────────────────────────────────────────────────────
-- Q1. Effective plan and the two limits that bound the day.
--
-- Mirrors server._get_user_plan (:7833). NOTE the two cases SQL cannot see:
--   * Stripe not configured      -> everyone is PRO regardless of this row
--   * no row + grandfathered     -> PRO (_is_grandfathered / PLAN_GRANDFATHER_UNTIL)
-- Read /api/admin/health .billing.stripe_mode alongside this.
-- PLAN_LIMITS (models.py:483):  FREE 20 shortlist / 120 finals
--                               PRO  35 shortlist / 250 finals
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    s.user_id,
    s.plan,
    s.current_period_end,
    (s.current_period_end IS NOT NULL
     AND s.current_period_end < now() AT TIME ZONE 'utc')      AS period_expired,
    (s.stripe_subscription_id IS NOT NULL)                     AS stripe_backed,
    CASE WHEN s.plan = 'FREE' THEN 20  ELSE 35  END            AS shortlist_daily,
    CASE WHEN s.plan = 'FREE' THEN 120 ELSE 250 END            AS finals_daily
FROM user_subscription s
WHERE s.user_id = :'uid';
-- Zero rows here is meaningful, not an error: no subscription row at all.


-- ─────────────────────────────────────────────────────────────────────────────
-- Q2. Jobs delivered today, and final scores consumed today.
--
-- `delivered` mirrors finals_budget.delivered_today() EXACTLY — count(*) over
-- application, email imports excluded, no status filter.
-- `board` mirrors slate.todays_entries() — the same rows MINUS the ones the
-- slate itself displaced. The two are different on purpose and differ by
-- today's displacements; see Q4.
-- `finals_count` / `finals_hits` are the durable ledger the budget spends
-- against (UserUsage, one row per user per UTC day).
-- ─────────────────────────────────────────────────────────────────────────────
WITH day AS (SELECT date_trunc('day', now() AT TIME ZONE 'utc') AS start_ts,
                    (now() AT TIME ZONE 'utc')::date            AS d),
apps AS (
    SELECT a.status, a.notes, a.viewed_at, a.job_id
    FROM application a, day
    WHERE a.user_id = :'uid'
      AND a.created_at >= day.start_ts
      AND a.apply_track <> 'email_import'
)
SELECT
    (SELECT count(*) FROM apps)                                   AS delivered_today,
    (SELECT count(*) FROM apps
      WHERE NOT (status = 'SKIPPED'
                 AND strpos(coalesce(notes, ''), 'slate_replaced') > 0)) AS on_board_now,
    (SELECT count(*) FROM apps
      WHERE status = 'SKIPPED'
        AND strpos(coalesce(notes, ''), 'slate_replaced') > 0)          AS displaced_today,
    (SELECT count(*) FROM apps
      WHERE status = 'SHORTLISTED' AND viewed_at IS NULL)           AS replaceable_now,
    coalesce((SELECT u.finals_count FROM user_usage u, day
               WHERE u.user_id = :'uid' AND u.usage_date = day.d), 0) AS finals_spent_today,
    coalesce((SELECT u.finals_hits FROM user_usage u, day
               WHERE u.user_id = :'uid' AND u.usage_date = day.d), 0) AS finals_hits_today;


-- ─────────────────────────────────────────────────────────────────────────────
-- Q3. THE STOP. Recomputes finals_budget.allowance() branch for branch, in the
-- same order the code evaluates it. `stop_reason` is the answer.
--
-- Substitute the plan's real numbers from Q1 into ceiling/target below.
-- Defaults here are PRO. Settings assumed at their code defaults:
--   finals_yield_window        50      (min finals today before yield may judge)
--   finals_yield_continue_rate 0.02    (2%)
--   shortlist_score_threshold  70      (what counts as a "hit")
--   slate_challenge_enabled    true
--   prescore_budget_multiplier 2       (Anthropic Tier-1 allowance = ceiling x 2)
-- ─────────────────────────────────────────────────────────────────────────────
WITH cfg AS (
    SELECT 250::int  AS ceiling,       -- PLAN_LIMITS[plan].finals_daily
           35::int   AS target,        -- PLAN_LIMITS[plan].shortlist_daily
           50::int   AS yield_window,
           0.02::numeric AS yield_rate,
           true      AS challenge_enabled
),
day AS (SELECT date_trunc('day', now() AT TIME ZONE 'utc') AS start_ts,
               (now() AT TIME ZONE 'utc')::date            AS d),
led AS (
    SELECT coalesce(max(u.finals_count), 0) AS spent,
           coalesce(max(u.finals_hits), 0)  AS hits
    FROM user_usage u, day
    WHERE u.user_id = :'uid' AND u.usage_date = day.d
),
del AS (
    SELECT count(*)::int AS delivered
    FROM application a, day
    WHERE a.user_id = :'uid'
      AND a.created_at >= day.start_ts
      AND a.apply_track <> 'email_import'
)
SELECT
    led.spent, led.hits, del.delivered, cfg.ceiling, cfg.target,
    CASE WHEN led.spent >= cfg.yield_window
         THEN round(led.hits::numeric / nullif(led.spent, 0), 4)
    END                                                        AS yield_today,
    (led.spent >= cfg.yield_window)                            AS yield_has_verdict,
    CASE
        WHEN cfg.ceiling > 0 AND led.spent >= cfg.ceiling
            THEN 'STOP: daily cost ceiling (' || led.spent || '/' || cfg.ceiling
                 || ' finals) at ' || del.delivered || '/' || cfg.target || ' delivered'
        WHEN led.spent >= cfg.yield_window
             AND (led.hits::numeric / nullif(led.spent, 0)) < cfg.yield_rate
            THEN 'STOP: yield collapsed ('
                 || round(100 * led.hits::numeric / nullif(led.spent, 0), 1)
                 || '% over ' || led.spent || ' finals, continue bar '
                 || round(100 * cfg.yield_rate, 1) || '%)'
        WHEN cfg.target > 0 AND del.delivered >= cfg.target AND NOT cfg.challenge_enabled
            THEN 'DONE: delivered ' || del.delivered || '/' || cfg.target
        WHEN cfg.target > 0 AND del.delivered >= cfg.target
            THEN 'CHALLENGE: slate full at ' || del.delivered || '/' || cfg.target
                 || ' — finals only for prescore >= the day cutoff (see Q4)'
        ELSE 'OPEN: delivering ' || del.delivered || '/' || cfg.target
             || ' at the normal gate (40)'
    END                                                        AS stop_reason,
    -- The fourth stop, applied by scoring_lane._finals_allowance AFTER the
    -- three above: Anthropic Tier-1 headroom. Only binds while OpenAI is the
    -- down provider — OpenAI prescores are not counted at all.
    cfg.ceiling * 2                                            AS anthropic_prescore_allowance
FROM cfg, led, del;


-- ─────────────────────────────────────────────────────────────────────────────
-- Q4. CHALLENGE MODE — the gate a late job must clear once the slate is full.
--
-- Mirrors slate.cutoff() -> finals_budget.challenger_gate(). NULL cutoff means
-- the slate still has room and the normal gate (40) applies. Otherwise the
-- Tier-1 gate is max(shortlist bar 70, cutoff) and DELIVERY additionally needs
-- cutoff + slate_displace_margin (5), or cutoff + slate_overflow_margin (15)
-- when nothing is replaceable (capped at slate_overflow_daily = 5/day).
-- ─────────────────────────────────────────────────────────────────────────────
WITH day AS (SELECT date_trunc('day', now() AT TIME ZONE 'utc') AS start_ts),
entries AS (
    SELECT a.status, a.viewed_at, coalesce(j.rerank_score, 0.0) AS score
    FROM application a
    JOIN day ON true
    LEFT JOIN job j ON j.id = a.job_id
    WHERE a.user_id = :'uid'
      AND a.created_at >= day.start_ts
      AND a.apply_track <> 'email_import'
      AND NOT (a.status = 'SKIPPED'
               AND strpos(coalesce(a.notes, ''), 'slate_replaced') > 0)
),
repl AS (SELECT score FROM entries WHERE status = 'SHORTLISTED' AND viewed_at IS NULL)
SELECT
    (SELECT count(*) FROM entries)                AS slate_size,
    35                                            AS slate_capacity,  -- from Q1
    (SELECT count(*) FROM repl)                   AS replaceable,
    CASE WHEN (SELECT count(*) FROM entries) < 35 THEN NULL
         ELSE coalesce((SELECT min(score) FROM repl),
                       (SELECT min(score) FROM entries))
    END                                           AS day_cutoff,
    CASE WHEN (SELECT count(*) FROM entries) < 35 THEN 40
         ELSE greatest(70, coalesce((SELECT min(score) FROM repl),
                                    (SELECT min(score) FROM entries)))
    END                                           AS tier1_gate_now;


-- ─────────────────────────────────────────────────────────────────────────────
-- Q5. DB vs DASHBOARD. `scored` in the cycle stats counts TIER-2 FINALS ONLY.
-- Any count built on `rerank_score IS NOT NULL` also matches Tier-1 drains and
-- the ghost (5.0) / expiry (8.0) / rule (10.0) sentinels, which is how a read
-- of 78 "scored" sat next to a cycle stat of scored=0 with both correct
-- (freshness.terminal_verdict_expr docstring). This splits them.
-- ─────────────────────────────────────────────────────────────────────────────
WITH day AS (SELECT date_trunc('day', now() AT TIME ZONE 'utc') AS start_ts)
SELECT
    count(*) FILTER (WHERE j.rerank_score IS NOT NULL)          AS rerank_score_not_null,
    count(*) FILTER (WHERE j.expired_at IS NULL
                       AND (j.scored_at IS NOT NULL
                            OR (j.rerank_score IS NOT NULL
                                AND j.rerank_score NOT IN (5.0, 8.0))))
                                                                AS terminal_verdicts,
    count(*) FILTER (WHERE j.expired_at IS NULL
                       AND j.scored_at IS NOT NULL
                       AND j.prescore IS NOT NULL
                       AND j.rerank_score = j.prescore)         AS tier1_drains,
    count(*) FILTER (WHERE j.scored_at >= (SELECT start_ts FROM day)
                       AND NOT (j.prescore IS NOT NULL
                                AND j.rerank_score = j.prescore))
                                                                AS tier2_finals_today,
    count(*) FILTER (WHERE j.expired_at IS NOT NULL
                        OR (j.rerank_score = 8.0 AND j.scored_at IS NULL))
                                                                AS expired_never_scored,
    count(*) FILTER (WHERE j.rerank_score IS NULL
                       AND j.is_closed = false)                 AS still_queued,
    count(*) FILTER (WHERE j.rerank_score IS NULL
                       AND j.is_closed = false
                       AND j.prescore IS NULL)                  AS queued_never_prescored,
    count(*) FILTER (WHERE j.rerank_score IS NULL
                       AND j.is_closed = false
                       AND j.eligibility = 'unknown')           AS held_unverified_location
FROM job j
WHERE j.user_id = :'uid';
-- `queued_never_prescored` is the size of the drain slice's candidate pool:
-- _user_queue(only_unprescored=True), capped at scoring_drain_cap = 25. A cycle
-- reporting `queued: N` with N <= 25 and `scored: 0` is that slice, not a stall.


-- ─────────────────────────────────────────────────────────────────────────────
-- Q6. The last 20 scoring cycles as the lane recorded them, newest first.
-- Confirms whether `scored: 0` is one cycle or the shape of the whole day, and
-- whether `drain_prescored` is moving (work happening) or flat (a real stall).
-- ─────────────────────────────────────────────────────────────────────────────
SELECT created_at,
       metadata_json::json ->> 'users'             AS users,
       metadata_json::json ->> 'queued'            AS queued,
       metadata_json::json ->> 'scored'            AS scored,
       metadata_json::json ->> 'drained'           AS drained,
       metadata_json::json ->> 'drain_prescored'   AS drain_prescored,
       metadata_json::json ->> 'shortlisted'       AS shortlisted,
       metadata_json::json ->> 'plan_capped_users' AS plan_capped,
       metadata_json::json ->> 'target_met_users'  AS target_met,
       metadata_json::json ->> 'expiry_stopped'    AS expiry_stopped,
       metadata_json::json ->> 'by_claude'         AS by_claude,
       metadata_json::json ->> 'by_gpt'            AS by_gpt
FROM funnel_events
WHERE stage = 'scoring_cycle'
ORDER BY id DESC
LIMIT 20;


-- ─────────────────────────────────────────────────────────────────────────────
-- Q7. Today's placement decisions for this user — why a qualified job did or
-- did not reach the board. slate.place() writes exactly one of these per
-- decision (reason = the Placement outcome).
-- ─────────────────────────────────────────────────────────────────────────────
SELECT reason, count(*) AS n
FROM funnel_events
WHERE stage = 'placement'
  AND created_at >= date_trunc('day', now() AT TIME ZONE 'utc')
  AND metadata_json::json ->> 'user_id' = :'uid'
GROUP BY reason
ORDER BY n DESC;
