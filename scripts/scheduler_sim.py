"""Discovery-scheduler policy simulator — READ-ONLY, offline, no DB, no network.

Answers one question: given a fixed polling capacity, which *scheduling policy*
finds relevant new postings soonest per unit of cost?

Nothing here touches production. It never imports app.db, never opens a session,
never makes an HTTP request. It is a self-contained discrete-event model whose
population is calibrated to figures already recorded in this repository, so the
comparison is reproducible and its assumptions are auditable rather than implied.

    python -m scripts.scheduler_sim --selftest    # invariants
    python -m scripts.scheduler_sim               # the policy comparison
    python -m scripts.scheduler_sim --sweep       # sensitivity to free parameters
    python -m scripts.scheduler_sim --capacity-table
    python -m scripts.scheduler_sim --feasibility # what each policy DEMANDS vs has

WHY A MODEL AND NOT A REPLAY
----------------------------
A replay needs the per-board poll/arrival series (which board was polled when,
and what appeared in between). Production records tick-level aggregates in
FunnelEvent and per-board *current* state in CompanyRegistry, but not that
series — and this environment has no production credentials. So this is an
explicit generative model. Every parameter is either (a) a figure recorded in
this repo, cited below, or (b) a free parameter that is SWEPT rather than
guessed. The policy RANKING is stable across the whole sweep; the absolute
latencies are not, and are reported as ranges.

CALIBRATION (each figure traceable to this repo)
------------------------------------------------
  52,698 active / 21,479 live / 31,219 zero-yield boards  app/config.py:465-470
  ~3,774 completed fetches per hour                       commit 1976f33
  fetch p50 519 ms                                        commit a712e33
  24 fetch workers, 300 selected/tick, 60 s tick          app/config.py:485-488
  live revisit 8.7 h @24h dead cadence, 6.4 h @72h, 5.7 h floor   commit c8c879c
  ~19k Job inserts/day; 12,936 pulse-tick inserts/24h     app/api/server.py:4726
  13 active users                                         commit 114b9a6

Per-board arrival rates are NOT invented. They follow from open-posting counts
by Little's Law (L = lambda*W): a board holding L open postings whose postings
stay open a mean W days must, in steady state, receive L/W new postings per day.
W and the tail dispersion are swept.
"""
from __future__ import annotations

import argparse
import heapq
import math
import random
import statistics
import sys

# ── Production constants (see CALIBRATION) ───────────────────────────────────
ACTIVE_BOARDS = 52_698
LIVE_BOARDS = 21_479
ZERO_YIELD_BOARDS = 31_219
FETCHES_PER_HOUR = 3_774.0
FETCH_P50_MS = 519.0
N_USERS = 13

POSTING_LIFETIME_DAYS = 40.0      # mean days a posting stays open (swept)
MEAN_OPEN_PER_LIVE_BOARD = 12.0   # -> 21,479*12/40 ~= 6,400 new postings/day
LOGNORMAL_SIGMA = 1.5             # dispersion of log(open postings) (swept)
RELEVANT_SHARE = 0.10             # share of postings relevant to some user
ZERO_ACTIVATION_RATE = 0.02       # zero-yield boards that wake during horizon

MIN_PER_DAY = 1440
FLOOR_MIN = 5.0                   # never poll a board faster than this
CAP_MIN = 72 * 60.0               # never leave a board longer than this


class Board:
    __slots__ = ("idx", "lam", "lam_all", "demand", "tod_peak", "is_zero",
                 "next_poll", "last_poll", "last_new_seen", "ewma_yield",
                 "consecutive_empty", "obs_arrivals", "obs_days", "cursor",
                 "seen_upto")

    def __init__(self, idx, lam, lam_all, demand, tod_peak, is_zero):
        self.idx = idx
        self.lam = lam
        self.lam_all = lam_all
        self.demand = demand
        self.tod_peak = tod_peak
        self.is_zero = is_zero
        self.reset()

    def reset(self):
        self.next_poll = 0.0
        self.last_poll = 0.0
        self.last_new_seen = -1e9
        self.ewma_yield = 0.0
        self.consecutive_empty = 0
        self.obs_arrivals = 0.0
        self.obs_days = 0.0
        self.cursor = 0          # index into this board's arrival array
        self.seen_upto = 0       # arrivals already consumed


def build_population(seed, sigma, lifetime_days, mean_open, relevant_share,
                     scale=1.0):
    """Boards with heavy-tailed productivity, calibrated by Little's Law.

    `scale` shrinks BOTH the population and (by the caller) the capacity, which
    leaves boards-per-fetch — and therefore every revisit interval — unchanged.
    A random subsample of a lognormal is lognormal, so the tail is preserved.
    """
    rng = random.Random(seed)
    n_live = max(1, int(LIVE_BOARDS * scale))
    n_zero = max(1, int(ZERO_YIELD_BOARDS * scale))
    mu = math.log(mean_open) - sigma * sigma / 2.0   # lognormal with this MEAN
    beta_b = 0.35 * (1.0 - relevant_share) / relevant_share
    boards = []
    for i in range(n_live):
        open_jobs = rng.lognormvariate(mu, sigma)
        lam_all = open_jobs / lifetime_days
        rel = rng.betavariate(0.35, beta_b)
        boards.append(Board(i, lam_all * rel, lam_all,
                            1.0 + rel * (N_USERS - 1) * rng.random(),
                            rng.choice([9, 10, 11, 14, 15, 16]), False))
    for i in range(n_zero):
        wakes = rng.random() < ZERO_ACTIVATION_RATE
        lam_all = (rng.lognormvariate(mu, sigma) / lifetime_days) if wakes else 0.0
        rel = rng.betavariate(0.35, beta_b) if wakes else 0.0
        boards.append(Board(n_live + i, lam_all * rel, lam_all,
                            1.0 + rel * (N_USERS - 1),
                            rng.choice([9, 10, 11, 14, 15, 16]), True))
    return boards


def tod_multiplier(minute_of_day, day_of_week, peak_hour):
    """Multiplicative NHPP shape: business-hours clustering, weekend dip.

    A SHARED shape with a per-board peak hour — not a learned 24x7 profile.
    The report argues a per-board profile cannot be estimated from the handful
    of events a typical board produces.
    """
    if day_of_week >= 5:
        return 0.15
    h = minute_of_day / 60.0
    d = min(abs(h - peak_hour), 24 - abs(h - peak_hour))
    return 0.12 + 2.6 * math.exp(-(d * d) / 12.5)


def generate_arrivals(boards, days, seed):
    """Thinned NHPP per board -> per-board sorted array of (time, relevant).

    Generated ONCE and replayed against every policy, so policies are compared
    on identical worlds — the most important property of the harness.
    """
    rng = random.Random(seed ^ 0x5EED)
    horizon = days * MIN_PER_DAY
    out = [[] for _ in boards]
    for b in boards:
        if b.lam_all <= 0:
            continue
        peak_per_min = b.lam_all * 3.0 / MIN_PER_DAY
        rel_p = b.lam / b.lam_all
        t = 0.0
        arr = out[b.idx]
        while True:
            t += rng.expovariate(peak_per_min)
            if t >= horizon:
                break
            mod = int(t) % MIN_PER_DAY
            dow = (int(t) // MIN_PER_DAY) % 7
            if rng.random() < tod_multiplier(mod, dow, b.tod_peak) / 3.0:
                arr.append((t, rng.random() < rel_p))
    return out


# ── Cadence policies ─────────────────────────────────────────────────────────
def cadence_baseline(b, now):
    """Current production _cadence (app/strategy/pulse_lane.py:123)."""
    if b.is_zero and b.obs_arrivals == 0:
        return 72 * 60.0
    if b.obs_arrivals > 0 and (now - b.last_new_seen) <= 7 * MIN_PER_DAY:
        return 5.0
    return 60.0


def cadence_fixed(b, now):
    """Policy A — flat 1h productive / 72h zero-yield."""
    if b.is_zero and b.obs_arrivals == 0:
        return 72 * 60.0
    return 60.0


def cadence_states(b, now):
    """Policy B — HOT / WARM / COOL / COLD state machine."""
    since = now - b.last_new_seen
    if b.obs_arrivals > 0 and since <= 3 * MIN_PER_DAY:
        return 15.0
    if b.obs_arrivals > 0 and since <= 21 * MIN_PER_DAY:
        return 60.0
    if b.consecutive_empty < 40:
        return 6 * 60.0
    return 48 * 60.0


def rate_estimate(b, prior_rate, prior_strength=3.0):
    """Gamma-Poisson posterior mean, shrunk toward a global/provider prior.

    alpha0 = prior_rate*prior_strength, beta0 = prior_strength days of pseudo-
    exposure; posterior mean = (alpha0 + k) / (beta0 + T).
    """
    return ((prior_rate * prior_strength + b.obs_arrivals)
            / (prior_strength + b.obs_days))


class IndexPolicy:
    """Policies C/D/E — cadence from the square-root allocation rule.

    T_i = scale / sqrt(w_i * lambda_i): the interior optimum of
        minimise  sum_i w_i * lambda_i * T_i / 2   s.t.  sum_i 1/T_i = B.

    `scale` is the Lagrange multiplier in disguise — the shadow price of one
    poll per hour. It is not a tuning constant: it is SOLVED, and re-solved as
    the rate estimates improve, by a controller that holds demand at capacity.
    That closed loop is the part a production implementation must also have;
    without it an index policy either starves (demand > capacity) or leaves
    capacity idle.
    """

    def __init__(self, prior_rate, use_value, use_tod, explore=0.0,
                 live_cap_min=None, cap_vs_uniform=None):
        self.prior_rate = prior_rate
        self.use_value = use_value
        self.use_tod = use_tod
        self.explore = explore
        # Hard anti-starvation bound: no board that has EVER produced may go
        # longer than this without a poll, whatever its index says. This is the
        # constraint that buys back the tail an index policy otherwise gives up.
        self.live_cap_min = live_cap_min
        # cap_vs_uniform: set the anti-starvation cap as a MULTIPLE of the
        # interval a uniform policy would achieve at the current capacity
        # (T_unif = live_boards / capacity). A fixed constant is wrong: it is
        # correctly tight when capacity is scarce and needlessly loose once it
        # is not, which is exactly when concentration starts costing tail for
        # nothing. Tying it to T_unif makes the policy converge to uniform as
        # capacity grows, with no knob to re-tune.
        self.cap_vs_uniform = cap_vs_uniform
        self._adaptive_cap = CAP_MIN
        self.scale = 1.0

    def weight(self, b):
        w = b.demand if self.use_value else 1.0
        if self.explore > 0:
            # Uncertainty bonus: a barely-observed board is credited with the
            # prior's upside so exploitation can never permanently bury it.
            w *= 1.0 + self.explore / math.sqrt(1.0 + b.obs_days)
        return w

    def interval(self, b, now, scale=None):
        """Poll interval from the HARMONIC-objective closed form.

        Kolobov, Peres, Lu & Horvitz, "Staying up to Date with Online Content
        Changes Using Reinforcement Learning for Scheduling", NeurIPS 2019,
        Prop. 2 / Eq. (5):

            rho_w = ( -D_w + sqrt(D_w^2 + 4*mu_w*D_w/lam) ) / 2

        NOT the binary-freshness form rho_i = max(0, sqrt(mu_i*D_i/lam) - D_i)
        (Azar, Horvitz, Lubetzky, Peres & Shahaf, PNAS 2018; restated as SIGIR
        2019 Eq. 3). That one sets rho = 0 outright once mu_i/D_i falls below
        the multiplier: it starves by construction, and the NeurIPS paper says
        so in as many words. Worse, what it starves is the HIGH-D_i end — the
        big Greenhouse boards posting ten roles a week, i.e. exactly the boards
        this product exists to watch. The harmonic form is strictly positive for
        every board with D_w > 0, and in SpotApply's regime (poll rate >> change
        rate) it equals sqrt(mu*D/lam) - D/2 to within 1.5%.

        `scale` is sqrt(lam), kept as the bisection variable so the
        controller's monotonic search (larger scale -> longer interval) is
        unchanged.
        """
        d = max(rate_estimate(b, self.prior_rate), 1e-12)   # Delta_w, per day
        mu = self.weight(b)
        sc = scale if scale is not None else self.scale
        # inv_lam = 1/lam. Defined as 1/sc^2 so that LARGER scale still means
        # LONGER intervals and lower demand — the monotonicity the controller's
        # bisection relies on.
        inv_lam = 1.0 / (sc * sc)
        rho = (-d + math.sqrt(d * d + 4.0 * mu * d * inv_lam)) / 2.0
        T = MIN_PER_DAY / max(rho, 1e-12)                   # per-day -> minutes
        # Time-of-day shaping applies ONLY to boards that actually post. A cold
        # board's interval stays flat, so its deadlines spread uniformly across
        # the clock and the cold sweep FILLS THE OVERNIGHT TROUGH that shaping
        # the warm boards would otherwise leave idle. Without this the server
        # sits ~23% idle at night while warm boards wait for morning — measured,
        # and the reason the utilisation invariant in selftest() exists.
        if self.use_tod and b.obs_arrivals > 0:
            dow = (int(now) // MIN_PER_DAY) % 7
            m = tod_multiplier(int(now) % MIN_PER_DAY, dow, b.tod_peak)
            # Time-of-day enters as an intensity multiplier on the ARRIVAL rate,
            # which is what it physically is — not as a divisor on the interval.
            dm = d * m
            rho = (-dm + math.sqrt(dm * dm + 4.0 * mu * dm * inv_lam)) / 2.0
            T = MIN_PER_DAY / max(rho, 1e-12)
        cap = CAP_MIN
        if not (b.is_zero and b.obs_arrivals == 0):
            if self.live_cap_min is not None:
                cap = self.live_cap_min
            elif self.cap_vs_uniform is not None:
                cap = self._adaptive_cap
        return FLOOR_MIN if T < FLOOR_MIN else (cap if T > cap else T)

    def __call__(self, b, now):
        return self.interval(b, now)

    def demand_at(self, boards, scale, times):
        """Time-AVERAGED polls/hour demanded. Averaging matters: a time-of-day
        policy sampled at midnight looks 8x cheaper than it is."""
        tot = 0.0
        for t in times:
            tot += sum(60.0 / self.interval(b, t, scale) for b in boards)
        return tot / len(times)

    def recalibrate(self, boards, capacity_per_hour, now=0.0, sample=4000):
        """Solve the shadow price so time-averaged demand == capacity.

        Demand is estimated on a random SUBSAMPLE and extrapolated: the sum is
        dominated by the clamped head, sampling error is a couple of percent,
        and the controller re-solves daily so any residual is corrected on the
        next pass. Solving exactly over 50k boards every day would cost more
        than the scheduling decision is worth.
        """
        if self.cap_vs_uniform is not None:
            n_live = sum(1 for b in boards if not b.is_zero or b.obs_arrivals > 0)
            t_unif_min = 60.0 * n_live / max(capacity_per_hour, 1e-9)
            self._adaptive_cap = max(FLOOR_MIN,
                                     min(CAP_MIN, self.cap_vs_uniform * t_unif_min))
        times = _sample_times(now)
        if len(boards) > sample:
            rng = random.Random(0xC0FFEE ^ int(now))
            sub = rng.sample(boards, sample)
            blow = len(boards) / float(sample)
        else:
            sub, blow = boards, 1.0
        # Precompute the rate-dependent half once; bisection then only scales it.
        pars = [(self.weight(b), max(rate_estimate(b, self.prior_rate), 1e-12),
                 b.obs_arrivals > 0) for b in sub]
        if self.use_tod:
            prof = [[tod_multiplier(int(t) % MIN_PER_DAY,
                                    (int(t) // MIN_PER_DAY) % 7, b.tod_peak)
                     for t in times] for b in sub]
        else:
            prof = None
        nt = len(times)
        hi_r = 60.0 / FLOOR_MIN
        def _cap_for(b):
            if b.is_zero and b.obs_arrivals == 0:
                return CAP_MIN
            if self.live_cap_min is not None:
                return self.live_cap_min
            if self.cap_vs_uniform is not None:
                return self._adaptive_cap
            return CAP_MIN
        lo_rs = [60.0 / _cap_for(b) for b in sub]

        def demand(scale):
            tot = 0.0
            inv_lam = 1.0 / (scale * scale)
            if prof is None:
                for (mu, d, _warm), lo_r in zip(pars, lo_rs):
                    rho = (-d + math.sqrt(d * d + 4.0 * mu * d * inv_lam)) / 2.0
                    r = rho / 24.0                     # per-day -> per-hour
                    tot += hi_r if r > hi_r else (lo_r if r < lo_r else r)
                return tot * blow
            for (mu, d, warm), ms, lo_r in zip(pars, prof, lo_rs):
                if not warm:
                    rho = (-d + math.sqrt(d * d + 4.0 * mu * d * inv_lam)) / 2.0
                    r = rho / 24.0
                    tot += hi_r if r > hi_r else (lo_r if r < lo_r else r)
                    continue
                acc = 0.0
                for m in ms:
                    dm = d * m
                    rho = (-dm + math.sqrt(dm * dm + 4.0 * mu * dm * inv_lam)) / 2.0
                    r = rho / 24.0
                    acc += hi_r if r > hi_r else (lo_r if r < lo_r else r)
                tot += acc / nt
            return tot * blow

        lo, hi = 1e-4, 1e7
        for _ in range(40):
            mid = math.sqrt(lo * hi)
            if demand(mid) > capacity_per_hour:
                lo = mid
            else:
                hi = mid
        self.scale = math.sqrt(lo * hi)
        return self.scale


def _sample_times(now=0.0):
    """A week of sample instants — enough to average out tod and weekends."""
    base = int(now) - (int(now) % MIN_PER_DAY)
    return [base + d * MIN_PER_DAY + h * 60 for d in range(7) for h in (2, 8, 11, 14, 17, 21)]


def demanded_rate(boards, cadence_fn, now=0.0):
    """Time-averaged polls/hour this policy ASKS FOR. Feasible iff <= capacity."""
    times = _sample_times(now)
    tot = 0.0
    for t in times:
        tot += sum(60.0 / cadence_fn(b, t) for b in boards)
    return tot / len(times)


# ── Event-driven EDF server ──────────────────────────────────────────────────
def simulate(boards, arrivals, cadence_fn, days, capacity_per_hour,
             warmup_days=7, work_conserving=True):
    """One policy, one world.

    The server completes one poll every 60/capacity_per_hour minutes and always
    serves the board with the earliest next_poll — exactly what _due_boards does
    (ORDER BY next_poll_at ASC) minus the tick quantisation, so the comparison
    isolates the CADENCE POLICY rather than tick mechanics.

    work_conserving: production's _due_boards takes only boards whose
    next_poll_at has ARRIVED, so a tick with nothing due does less work and the
    capacity is simply lost. That is invisible today because the lane is 5.8x
    oversubscribed and therefore never idle — but any policy that fits inside
    capacity idles at night and pays for it in the tail. A work-conserving
    server pulls the next-best board forward instead. Modelling both is the
    point: the difference is a design requirement, not a modelling detail.
    """
    for b in boards:
        b.reset()
    rng = random.Random(7)
    heap = []
    for b in boards:
        b.next_poll = rng.random() * 60.0
        heap.append((b.next_poll, b.idx))
    heapq.heapify(heap)

    horizon = days * MIN_PER_DAY
    warmup = warmup_days * MIN_PER_DAY
    dt = 60.0 / capacity_per_hour
    by_idx = {b.idx: b for b in boards}
    # The shadow-price controller. An index policy must re-solve its multiplier
    # as estimates improve, or it starves / idles. Daily is frequent enough to
    # track learning and cheap enough to be free in production.
    recal = getattr(cadence_fn, "recalibrate", None)
    if recal:
        recal(boards, capacity_per_hour, 0.0)
    next_recal = MIN_PER_DAY

    delays_rel, delays_all = [], []
    polls = polls_zero = polls_empty = polls_changed = 0
    t_server = 0.0
    push, pop = heapq.heappush, heapq.heappop

    while heap:
        deadline, idx = pop(heap)
        t = t_server if work_conserving else (
            deadline if deadline > t_server else t_server)
        if t >= horizon:
            break
        # POLITENESS FLOOR. Work conservation means "do not idle while work is
        # due", NOT "poll a board sooner than its floor". Without this guard a
        # single high-value board whose interval sits at the floor is always the
        # heap minimum and gets polled every few seconds — the model quietly
        # issues 4x the requests the floor permits, which in production is an
        # abuse complaint rather than a scheduling win.
        b0 = by_idx[idx]
        earliest = b0.last_poll + FLOOR_MIN
        if t < earliest:
            push(heap, (earliest, idx))
            # Advance the server clock to the next board that is actually
            # pollable, otherwise the loop spins without time moving whenever
            # every board is sitting on its floor.
            nxt = heap[0][0]
            t_server = nxt if nxt > t_server else t_server
            if t_server >= horizon:
                break
            continue
        t_server = t + dt
        if recal and t >= next_recal:
            recal(boards, capacity_per_hour, t)
            next_recal = t + MIN_PER_DAY
        b = by_idx[idx]
        polls += 1
        if b.is_zero and b.obs_arrivals == 0:
            polls_zero += 1
        b.obs_days += (t - b.last_poll) / MIN_PER_DAY
        b.last_poll = t

        arr = arrivals[idx]
        c = b.cursor
        n = len(arr)
        k = 0
        while c < n and arr[c][0] <= t:
            at, rel = arr[c]
            d = t - at
            if t > warmup:
                delays_all.append(d)
                if rel:
                    delays_rel.append(d)
            c += 1
            k += 1
        b.cursor = c
        if k:
            polls_changed += 1
            b.obs_arrivals += k
            b.ewma_yield = 0.7 * b.ewma_yield + 0.3 * k
            b.last_new_seen = t
            b.consecutive_empty = 0
        else:
            polls_empty += 1
            b.ewma_yield *= 0.7
            b.consecutive_empty += 1
        push(heap, (t + cadence_fn(b, t), idx))

    censored = censored_rel = 0
    for b in boards:
        arr = arrivals[b.idx]
        for j in range(b.cursor, len(arr)):
            censored += 1
            if arr[j][1]:
                censored_rel += 1
    stale = max((horizon - b.last_poll) for b in boards)
    # Staleness of boards the product actually cares about — a zero-yield board
    # sitting at its 72h cap is the policy working, not starvation.
    _live = [b for b in boards if not b.is_zero or b.obs_arrivals > 0]
    stale_live = max((horizon - b.last_poll) for b in _live) if _live else 0.0

    def pctls(xs):
        if not xs:
            return {}
        xs.sort()
        n = len(xs)
        return {p: xs[min(n - 1, int(n * p / 100.0))] for p in (50, 75, 90, 95, 99)}

    dr = pctls(delays_rel)
    n_rel = len(delays_rel)
    within60 = sum(1 for d in delays_rel if d <= 60.0) / n_rel if n_rel else 0.0
    return {
        "polls_per_day": polls / max(days, 1),
        "capacity_used": (polls / max(days, 1)) / (capacity_per_hour * 24),
        "poll_share_zero": polls_zero / max(polls, 1),
        "wasted_share": polls_empty / max(polls, 1),
        "relevant_found": n_rel,
        "relevant_censored": censored_rel,
        "all_censored": censored,
        "delay_rel": dr,
        "delay_all": pctls(delays_all),
        "mean_delay_rel": statistics.mean(delays_rel) if delays_rel else float("nan"),
        "within_60m": within60,
        "db_writes_per_day": polls_changed / max(days, 1),
        "max_staleness_min": stale,
        "max_staleness_live_min": stale_live,
    }


def fmt(m):
    if m != m:
        return "n/a"
    if m < 90:
        return f"{m:.0f}m"
    if m < 60 * 48:
        return f"{m/60:.1f}h"
    return f"{m/1440:.1f}d"


# ── Reports ──────────────────────────────────────────────────────────────────
def _setup(args):
    boards = build_population(args.seed, args.sigma, args.lifetime,
                              MEAN_OPEN_PER_LIVE_BOARD, RELEVANT_SHARE,
                              scale=args.scale)
    cap = FETCHES_PER_HOUR * args.scale if args.capacity is None \
        else args.capacity * args.scale
    arrivals = generate_arrivals(boards, args.days, args.seed)
    prior = sum(b.lam_all for b in boards) / len(boards)
    return boards, arrivals, cap, prior


def _policies(boards, prior, cap):
    """Fresh policy objects each call — an IndexPolicy carries mutable state."""
    return [
        ("Baseline (current)", cadence_baseline),
        ("A  fixed 1h / 72h", cadence_fixed),
        ("B  HOT/WARM/COOL/COLD", cadence_states),
        ("C  sqrt(lambda)", IndexPolicy(prior, False, False)),
        ("D  C + time-of-day", IndexPolicy(prior, False, True)),
        ("E  sqrt(demand*lam)+tod+explore", IndexPolicy(prior, True, True, 1.5)),
        ("F  E + fixed 8h staleness cap", IndexPolicy(prior, True, True, 1.5,
                                                      live_cap_min=8 * 60.0)),
        ("G  E + cap = 1.5x uniform", IndexPolicy(prior, True, True, 1.5,
                                                  cap_vs_uniform=1.5)),
    ]


def run_comparison(args):
    boards, arrivals, cap, prior = _setup(args)
    tot_lam = sum(b.lam_all for b in boards)
    tot_rel = sum(b.lam for b in boards)
    sf = 1.0 / args.scale
    print("=" * 100)
    print("SPOTAPPLY DISCOVERY-SCHEDULER POLICY SIMULATION   (offline model — no DB, no network)")
    print("=" * 100)
    print(f"population   {len(boards):,} boards simulated at scale {args.scale:g} "
          f"(= {len(boards)*sf:,.0f} at production size)")
    print(f"arrivals     {tot_lam*sf:,.0f} new postings/day, {tot_rel*sf:,.0f}/day "
          f"relevant to the {N_USERS} active users ({100*tot_rel/tot_lam:.1f}%)")
    print(f"capacity     {cap*sf:,.0f} completed fetches/hour "
          f"({cap*sf*24:,.0f}/day)   [production: {FETCHES_PER_HOUR:,.0f}/h]")
    print(f"model        lognormal sigma={args.sigma}, posting lifetime="
          f"{args.lifetime:.0f}d, horizon={args.days}d (first 7d discarded)")
    print()
    print("FEASIBILITY — polls/hour each policy DEMANDS vs the capacity it has:")
    for name, fn in _policies(boards, prior, cap):
        if hasattr(fn, "recalibrate"):
            fn.recalibrate(boards, cap, 0.0)
        d = demanded_rate(boards, fn) * sf
        flag = "OK" if d <= cap * sf * 1.02 else f"INFEASIBLE {d/(cap*sf):.1f}x"
        print(f"   {name:32s} demands {d:10,.0f}/h   {flag}")
    print()
    hdr = (f"{'policy':32s} {'polls/d':>9s} {'cap%':>5s} | "
           f"{'RELEVANT discovery delay':^28s} | "
           f"{'<60m':>6s} {'waste':>6s} {'zero%':>6s} {'stale':>6s}")
    print(hdr)
    print(f"{'':32s} {'':>9s} | {'p50':>7s}{'p75':>7s}{'p90':>7s}{'p95':>7s} |")
    print("-" * 100)
    results = {}
    for name, fn in _policies(boards, prior, cap):
        r = simulate(boards, arrivals, fn, args.days, cap)
        results[name] = r
        d = r["delay_rel"]
        print(f"{name:32s} {r['polls_per_day']*sf:9,.0f} {100*r['capacity_used']:4.0f}% | "
              f"{fmt(d.get(50, float('nan'))):>7s}{fmt(d.get(75, float('nan'))):>7s}"
              f"{fmt(d.get(90, float('nan'))):>7s}{fmt(d.get(95, float('nan'))):>7s} | "
              f"{100*r['within_60m']:5.1f}% {100*r['wasted_share']:5.1f}% "
              f"{100*r['poll_share_zero']:5.1f}% {fmt(r['max_staleness_live_min']):>6s}")
    print()
    b50 = results["Baseline (current)"]["delay_rel"].get(50, float("nan"))
    e50 = results["G  E + cap = 1.5x uniform"]["delay_rel"].get(50, float("nan"))
    print("Every row spends the SAME capacity. Median relevant-job discovery delay:")
    print(f"   baseline {fmt(b50)}  ->  policy G {fmt(e50)}   "
          f"({b50/e50:.1f}x faster at identical cost)")
    return results


def run_roadmap(args):
    """Separate the two levers: better ALLOCATION vs cheaper POLLS.

    A conditional GET that returns 304 skips the body download, the JSON/HTML
    parse and all downstream DB work. Since ~96.5% of polls find nothing (see
    the `waste` column), making that case cheap multiplies effective capacity —
    which is a different lever from scheduling, and composes with it.
    """
    boards, arrivals, cap, prior = _setup(args)
    sf = 1.0 / args.scale
    print("=" * 100)
    print("ROADMAP — the two independent levers, and what each is worth")
    print("=" * 100)
    base_r = simulate(boards, arrivals, cadence_baseline, args.days, cap)
    waste = base_r["wasted_share"]
    print(f"measured in-model: {100*waste:.1f}% of polls find NO new posting.")
    print()
    print("If an unchanged board can be confirmed by a conditional request at")
    print("cost ratio r (vs a full fetch+parse+upsert), effective capacity is")
    print("   B_eff = B / (changed_share + r * unchanged_share)")
    print()
    print(f"{'scenario':44s} {'fetch/h':>9s} | {'p50':>7s}{'p75':>7s}{'p90':>7s}{'p95':>7s} | {'<60m':>6s}")
    print("-" * 100)
    scenarios = []
    for r_cost, lbl in ((1.0, "today: every poll a full fetch"),
                        (0.25, "conditional GET at 1/4 cost"),
                        (0.10, "conditional GET at 1/10 cost")):
        mult = 1.0 / ((1 - waste) + r_cost * waste)
        scenarios.append((lbl, mult))
    for lbl, mult in scenarios:
        for pname, pol in (("baseline cadence", cadence_baseline),
                           ("policy G", IndexPolicy(prior, True, True, 1.5,
                                                    cap_vs_uniform=1.5))):
            rr = simulate(boards, arrivals, pol, args.days, cap * mult)
            d = rr["delay_rel"]
            print(f"{(lbl + '  +  ' + pname):46s} {cap*mult*sf:9,.0f} | "
                  f"{fmt(d.get(50, float('nan'))):>7s}{fmt(d.get(75, float('nan'))):>7s}"
                  f"{fmt(d.get(90, float('nan'))):>7s}{fmt(d.get(95, float('nan'))):>7s} | "
                  f"{100*rr['within_60m']:5.1f}%")
        print()
    print("Read the columns, not the rows: scheduling moves p50 and the <60m rate;")
    print("cheap probes move the whole distribution by multiplying capacity. They")
    print("compose, and neither alone reaches a 15-60 minute promise.")


def run_example(args):
    """Trace five archetypal boards for 30 days under the recommended policy.

    The five boards are INSERTED INTO a full-size population so the shadow price
    they compete against is realistic — a cadence computed against five boards
    in isolation would mean nothing.
    """
    boards = build_population(args.seed, args.sigma, args.lifetime,
                              MEAN_OPEN_PER_LIVE_BOARD, RELEVANT_SHARE,
                              scale=args.scale)
    cap = FETCHES_PER_HOUR * args.scale
    base_n = len(boards)
    # idx, label, lam_all/day, demand weight, wakes_on_day
    SPECS = [
        ("highly productive   (30 jobs/day, 8 users)", 30.0, 8.0, None),
        ("moderately active   (2 jobs/day, 4 users)",   2.0, 4.0, None),
        ("weekly              (2 jobs/week, 3 users)",  2.0 / 7, 3.0, None),
        ("zero-yield          (never posts)",           0.0, 1.0, None),
        ("dormant -> wakes    (silent, then 3/day @d15)", 0.0, 5.0, 15),
    ]
    specials = []
    for k, (lbl, lam, dem, wake) in enumerate(SPECS):
        b = Board(base_n + k, lam, lam, dem, 10, lam == 0.0)
        boards.append(b)
        specials.append((lbl, b, wake))

    arrivals = generate_arrivals(boards, args.days, args.seed)
    # Hand-build the special boards' arrival streams so the archetypes are exact.
    rng = random.Random(args.seed ^ 0xA11CE)
    for lbl, b, wake in specials:
        arr = []
        rate = b.lam_all
        start = 0.0
        if wake is not None:
            rate = 3.0
            start = wake * MIN_PER_DAY
        if rate > 0:
            t = start
            while True:
                t += rng.expovariate(rate / MIN_PER_DAY)
                if t >= args.days * MIN_PER_DAY:
                    break
                arr.append((t, True))
        arrivals[b.idx] = arr
        b.lam = b.lam_all = (rate if wake is None else 0.0)

    prior = sum(x.lam_all for x in boards) / len(boards)
    pol = IndexPolicy(prior, True, True, 1.5, cap_vs_uniform=1.5)

    # Instrument: sample each special board's assigned interval once a day.
    trace = {b.idx: [] for _lbl, b, _w in specials}

    for b in boards:
        b.reset()
    heap = [(random.Random(7).random() * 60.0, b.idx) for b in boards]
    heapq.heapify(heap)
    by_idx = {b.idx: b for b in boards}
    horizon = args.days * MIN_PER_DAY
    dt = 60.0 / cap
    t_server = 0.0
    pol.recalibrate(boards, cap, 0.0)
    next_recal = MIN_PER_DAY
    next_sample = 0.0
    polls = {b.idx: 0 for _l, b, _w in specials}
    while heap:
        deadline, idx = heapq.heappop(heap)
        t = t_server
        if t >= horizon:
            break
        b0 = by_idx[idx]
        if t < b0.last_poll + FLOOR_MIN:      # politeness floor, as in simulate()
            heapq.heappush(heap, (b0.last_poll + FLOOR_MIN, idx))
            nxt = heap[0][0]
            t_server = nxt if nxt > t_server else t_server
            if t_server >= horizon:
                break
            continue
        t_server = t + dt
        if t >= next_recal:
            pol.recalibrate(boards, cap, t)
            next_recal = t + MIN_PER_DAY
        if t >= next_sample:
            for _l, b, _w in specials:
                trace[b.idx].append((t / MIN_PER_DAY, pol.interval(b, t)))
            next_sample = t + MIN_PER_DAY
        b = by_idx[idx]
        b.obs_days += (t - b.last_poll) / MIN_PER_DAY
        b.last_poll = t
        arr = arrivals[idx]
        c, k = b.cursor, 0
        while c < len(arr) and arr[c][0] <= t:
            c += 1
            k += 1
        b.cursor = c
        if idx in polls:
            polls[idx] += 1
        if k:
            b.obs_arrivals += k
            b.last_new_seen = t
            b.consecutive_empty = 0
        else:
            b.consecutive_empty += 1
        heapq.heappush(heap, (t + pol.interval(b, t), idx))

    print("=" * 100)
    print("FIVE BOARDS, 30 DAYS, RECOMMENDED POLICY — cadence adapts with no per-board config")
    print("=" * 100)
    print("Assigned poll interval, sampled daily (business-hours value; the")
    print("interval widens overnight and at weekends by design).")
    print()
    days_shown = [1, 3, 5, 7, 10, 14, 15, 16, 17, 20, 25, 29]
    print(f"{'board':46s} " + "".join(f"{('d'+str(d)):>7s}" for d in days_shown) + f"{'polls':>8s}")
    print("-" * 100)
    for lbl, b, _w in specials:
        row = ""
        tr = trace[b.idx]
        for d in days_shown:
            v = min(tr, key=lambda x: abs(x[0] - d)) if tr else (0, float("nan"))
            row += f"{fmt(v[1]):>7s}"
        print(f"{lbl:46s} {row}{polls[b.idx]:8,d}")
    print()
    print("Read the dormant board's row across d14 -> d16: it wakes on day 15 and")
    print("its cadence collapses from the cold cap to the fast lane on the FIRST")
    print("poll that observes a posting — no threshold to cross, no state to flip.")


def run_sweep(args):
    print("=" * 100)
    print("SENSITIVITY — does the ranking survive the free parameters?")
    print("=" * 100)
    print("E = uncapped index policy; G = same policy with the adaptive")
    print("anti-starvation cap. The contrast is the point: concentration WITHOUT")
    print("a cap is worse than the baseline whenever the tail is thin.")
    print()
    print(f"{'sigma':>6s} {'life':>5s} {'rel%':>5s} | {'base p50':>9s} {'E p50':>8s} {'G p50':>8s}"
          f" | {'base p95':>9s} {'E p95':>8s} {'G p95':>8s} | {'G gain':>7s} {'G<60m':>6s}")
    print("-" * 100)
    for sigma in (1.0, 1.5, 2.0, 2.5):
        for lifetime in (10.0, 30.0, 45.0):
            for rel in (0.05, 0.20):
                boards = build_population(args.seed, sigma, lifetime,
                                          MEAN_OPEN_PER_LIVE_BOARD, rel,
                                          scale=args.scale)
                arrivals = generate_arrivals(boards, args.days, args.seed)
                cap = FETCHES_PER_HOUR * args.scale
                prior = sum(b.lam_all for b in boards) / len(boards)
                base = simulate(boards, arrivals, cadence_baseline, args.days, cap)
                e = simulate(boards, arrivals,
                             IndexPolicy(prior, True, True, 1.5), args.days, cap)
                g = simulate(boards, arrivals,
                             IndexPolicy(prior, True, True, 1.5, cap_vs_uniform=1.5),
                             args.days, cap)
                b50 = base["delay_rel"].get(50, float("nan"))
                e50 = e["delay_rel"].get(50, float("nan"))
                g50 = g["delay_rel"].get(50, float("nan"))
                print(f"{sigma:6.1f} {lifetime:5.0f} {100*rel:4.0f}% | "
                      f"{fmt(b50):>9s} {fmt(e50):>8s} {fmt(g50):>8s} | "
                      f"{fmt(base['delay_rel'].get(95, float('nan'))):>9s} "
                      f"{fmt(e['delay_rel'].get(95, float('nan'))):>8s} "
                      f"{fmt(g['delay_rel'].get(95, float('nan'))):>8s} | "
                      f"{(b50/g50 if g50 else float('nan')):6.1f}x "
                      f"{100*g['within_60m']:5.1f}%")


def run_capacity(args):
    print("=" * 100)
    print("CAPACITY PLANNER — fetches/hour to hold a revisit interval T")
    print("=" * 100)
    print("For periodic polling at interval T with Poisson arrivals, discovery")
    print("delay is Uniform(0,T): median T/2, p90 0.9T, p95 0.95T.")
    print("So a median SLA needs T = 2*target; a p95 SLA needs T = target/0.95.")
    print()
    print(f"{'served set':>26s} {'boards':>9s} {'T':>6s} {'fetch/h':>10s} "
          f"{'fetch/s':>8s} {'workers':>8s} {'p50':>7s} {'p95':>7s} {'vs today':>9s}")
    print("-" * 100)
    for label, n, T in [
        ("all active boards", ACTIVE_BOARDS, 60),
        ("all live boards", LIVE_BOARDS, 15),
        ("all live boards", LIVE_BOARDS, 30),
        ("all live boards", LIVE_BOARDS, 60),
        ("top 25% by value", int(LIVE_BOARDS * .25), 15),
        ("top 25% by value", int(LIVE_BOARDS * .25), 30),
        ("top 10% by value", int(LIVE_BOARDS * .10), 15),
        ("top 5% by value", int(LIVE_BOARDS * .05), 10),
    ]:
        per_h = n * (60.0 / T)
        per_s = per_h / 3600.0
        workers = per_s * (FETCH_P50_MS / 1000.0)
        print(f"{label:>26s} {n:9,d} {T:5d}m {per_h:10,.0f} {per_s:8.2f} "
              f"{workers:8.1f} {fmt(T/2):>7s} {fmt(.95*T):>7s} "
              f"{per_h/FETCHES_PER_HOUR:8.1f}x")
    print()
    print(f"workers = fetch/s x {FETCH_P50_MS:.0f}ms (Little's Law on the pool),")
    print("ignoring the post-fetch consumer — which is the real production bottleneck.")


def run_feasibility(args):
    boards, arrivals, cap, prior = _setup(args)
    sf = 1.0 / args.scale
    print("=" * 100)
    print("WHY THE CURRENT SCHEDULER YIELDS HOURS, NOT MINUTES")
    print("=" * 100)
    print(f"capacity                        {cap*sf:12,.0f} fetches/hour")
    for name, fn in _policies(boards, prior, cap):
        if hasattr(fn, "recalibrate"):
            fn.recalibrate(boards, cap, 0.0)
        d = demanded_rate(boards, fn) * sf
        print(f"{name:32s}{d:12,.0f} demanded/hour   "
              f"ratio {d/(cap*sf):5.2f}x  ->  "
              f"{'feasible' if d <= cap*sf else 'starves, EDF degenerates to round-robin'}")
    print()
    print("Under infeasibility EDF cannot express intent: every board is overdue,")
    print("so service order is simply 'most overdue first' = round-robin, and the")
    print("realised revisit interval is boards / capacity regardless of the cadence")
    print("each board was assigned.")
    print()
    for dead_h, lbl in ((24, "before"), (72, "now"), (None, "zero-yield never polled")):
        zd = ZERO_YIELD_BOARDS / dead_h if dead_h else 0.0
        avail = FETCHES_PER_HOUR - zd
        print(f"  zero-yield cadence {str(dead_h)+'h':>5s} ({lbl:24s}): "
              f"live capacity {avail:7,.0f}/h -> revisit {LIVE_BOARDS/avail:5.2f}h "
              f"-> median discovery {LIVE_BOARDS/avail/2:5.2f}h")


# ── Self-test ────────────────────────────────────────────────────────────────
def selftest():
    ok = True

    def check(cond, msg):
        nonlocal ok
        if not cond:
            print(f"FAIL: {msg}")
            ok = False

    # 1. Little's Law calibration reproduces the intended aggregate.
    b = build_population(1, 1.5, 40.0, 12.0, 0.10, scale=0.25)
    tot = sum(x.lam_all for x in b) / 0.25
    exp = LIVE_BOARDS * 12.0 / 40.0
    check(abs(tot - exp) / exp < 0.15,
          f"aggregate {tot:.0f} far from Little's-Law target {exp:.0f}")

    # 2. Uniform(0,T) delay law: one board polled every T must show median ~T/2.
    one = [Board(0, 24.0, 24.0, 1.0, 12, False)]
    ev = generate_arrivals(one, 40, 3)
    # work_conserving=False: this check validates the ANALYTIC delay law, which
    # assumes the poll happens at its deadline. A work-conserving server with
    # spare capacity would poll this single board continuously and drive the
    # delay to zero — correct behaviour, wrong test for this law.
    r = simulate(one, ev, lambda bb, now: 60.0, 40, 1000.0, warmup_days=1,
                 work_conserving=False)
    med = r["delay_all"].get(50, 0)
    check(22 <= med <= 38, f"median delay {med:.1f}m not ~30m for T=60m")

    # 3. p95 of that same board must be ~0.95*T.
    p95 = r["delay_all"].get(95, 0)
    check(50 <= p95 <= 66, f"p95 delay {p95:.1f}m not ~57m for T=60m")

    # 4. Capacity binds: realised polls/day cannot exceed the budget.
    bs = build_population(2, 1.5, 40.0, 12.0, 0.10, scale=0.05)
    ev = generate_arrivals(bs, 12, 2)
    r = simulate(bs, ev, cadence_baseline, 12, 200.0, work_conserving=False)
    check(r["polls_per_day"] <= 200 * 24 * 1.02,
          f"polls/day {r['polls_per_day']:.0f} exceeded budget")

    # 5. No arrival vanishes: discovered + censored == generated (post-warmup
    #    accounting aside, censored+found must not exceed the total).
    gen = sum(len(a) for a in ev)
    check(r["all_censored"] <= gen, "censored exceeds generated arrivals")

    # 6. Monotonicity: more capacity must not slow discovery.
    lo = simulate(bs, ev, cadence_baseline, 12, 100.0, work_conserving=False)
    hi = simulate(bs, ev, cadence_baseline, 12, 800.0, work_conserving=False)
    check(hi["delay_all"].get(50, 1e9) <= lo["delay_all"].get(50, 0),
          "extra capacity did not improve median delay")

    # 7. The shadow-price controller must land demand ON capacity, not near it.
    prior = sum(x.lam_all for x in bs) / len(bs)
    pol = IndexPolicy(prior, False, False)
    pol.recalibrate(bs, 200.0, 0.0)
    d = demanded_rate(bs, pol)
    check(abs(d - 200.0) / 200.0 < 0.05,
          f"controller demand {d:.1f} != capacity 200")

    # 7b. Same, for a time-of-day policy — the case where sampling at a single
    #     instant (midnight) understated demand ~8x.
    polt = IndexPolicy(prior, False, True)
    polt.recalibrate(bs, 200.0, 0.0)
    dt_ = demanded_rate(bs, polt)
    check(abs(dt_ - 200.0) / 200.0 < 0.05,
          f"tod controller demand {dt_:.1f} != capacity 200")

    # 8. THE CLAIM: at equal demanded cost, sqrt allocation beats uniform on a
    #    heavy tail. This is the result the whole design rests on.
    bs = build_population(3, 2.0, 40.0, 12.0, 0.10, scale=0.1)
    ev = generate_arrivals(bs, 21, 3)
    cap = FETCHES_PER_HOUR * 0.1
    prior = sum(x.lam_all for x in bs) / len(bs)
    base = simulate(bs, ev, cadence_fixed, 21, cap)
    sq = simulate(bs, ev, IndexPolicy(prior, False, False), 21, cap)
    check(sq["delay_rel"].get(50, 1e9) < base["delay_rel"].get(50, 1e9),
          f"sqrt {fmt(sq['delay_rel'].get(50,0))} did not beat uniform "
          f"{fmt(base['delay_rel'].get(50,0))} at equal budget")

    # 8b. A work-conserving server must not leave capacity idle.
    wc = simulate(bs, ev, IndexPolicy(prior, True, True, 1.5), 21, cap)
    check(wc["capacity_used"] > 0.93,
          f"work-conserving server used only {100*wc['capacity_used']:.0f}% of capacity "
          f"— the overnight trough is not being filled by the cold sweep")

    # 8c. The staleness cap must actually bind: policy F's worst board must be
    #     fresher than the same policy without the cap.
    nocap = simulate(bs, ev, IndexPolicy(prior, True, True, 1.5), 21, cap)
    withcap = simulate(bs, ev, IndexPolicy(prior, True, True, 1.5,
                                           live_cap_min=8 * 60.0), 21, cap)
    check(withcap["max_staleness_live_min"] < nocap["max_staleness_live_min"],
          f"8h cap: worst LIVE board {fmt(withcap['max_staleness_live_min'])} "
          f"not better than uncapped {fmt(nocap['max_staleness_live_min'])}")
    check(withcap["max_staleness_live_min"] <= 8 * 60.0 * 1.5,
          f"8h cap not honoured: worst live board "
          f"{fmt(withcap['max_staleness_live_min'])}")

    # 9. Anti-starvation: the cap must bound the worst board's staleness.
    check(sq["max_staleness_min"] <= CAP_MIN * 3,
          f"max staleness {fmt(sq['max_staleness_min'])} exceeds the cap")

    print("selftest: PASS" if ok else "selftest: FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--sigma", type=float, default=LOGNORMAL_SIGMA)
    ap.add_argument("--lifetime", type=float, default=POSTING_LIFETIME_DAYS)
    ap.add_argument("--capacity", type=float, default=None)
    ap.add_argument("--scale", type=float, default=0.25,
                    help="fraction of the real registry to simulate; capacity "
                         "scales with it so every revisit interval is preserved")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--capacity-table", dest="captable", action="store_true")
    ap.add_argument("--feasibility", action="store_true")
    ap.add_argument("--roadmap", action="store_true")
    ap.add_argument("--example", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.captable:
        run_capacity(a)
        return 0
    if a.feasibility:
        run_feasibility(a)
        return 0
    if a.roadmap:
        run_roadmap(a)
        return 0
    if a.example:
        run_example(a)
        return 0
    if a.sweep:
        run_sweep(a)
        return 0
    run_comparison(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
