# Backlog — before going live with real capital

Things that are working and acceptable during the evaluation/testing
phase, but worth revisiting before real money is on the line.

## Build a live/daily runner for the pure price-action strategy (added 2026-08-04)

Only `shadow_price_action.py` (backtest) exists for this strategy --
there is no `main_price_action.py` to run daily analysis the way
`main_live.py`/`main_condor.py`/`main_directional_spread.py` already do
for the other three. Deliberately not built yet: the 2-year backtest
(`daily_hourly` and `intraday` timeframe pairs, see README's 2026-08-04
entries once logged) found only 19-24 distinct qualifying setups across
497 days, a 26% max drawdown on the `intraday @ 1:3` account walk, and a
result that depended on one weekend-gap trade for ~half its total P&L
before that trade was stripped out. None of that is disqualifying, but
none of it clears the bar for "build the live wiring" either.

Build this once (a) the gap-fill blind spot in `shadow_price_action.py`
is fixed (weekend/overnight target hits currently resolve at the next
recorded LTP with no slippage modelling -- see `_walk_forward`'s
docstring) and the corrected sweep still shows a positive, non-outlier-
dependent edge, or (b) a larger sample (more history, or paper-traded
live data) gives the same read. When it is built, it should be
analysis-only and manually approved, mirroring every other strategy's
`AUTO_APPROVE_*` convention in this project -- never auto-execute.

Capital/sizing for the day this gets started: `config_price_action.py`
now carries TOTAL_CAPITAL=50,000 and MAX_LOTS_PER_TRADE=1 (set
2026-08-04, matching the ~Rs 5-7k premium outlays actually seen in the
backtest rather than the momentum scanner's Rs 5L base).

## Run every strategy on Bank Nifty as well as NIFTY (added 2026-08-04)

Currently all four strategies (momentum, condor, directional spread,
price-action) trade/analyze NIFTY only. Extending to Bank Nifty is a
real, multi-part build, not a config flag:

  - `banknifty_context.py` already has Bank Nifty's INDEX security ID
    (25, IDX_I segment) and fetches its candles -- but only as a
    divergence/context signal for the NIFTY scanner, never to trade Bank
    Nifty options itself. That scaffolding does not extend to the
    option CHAIN.
  - `dhan_source.py`'s option-chain fetch, `historical_source.py`'s
    NIFTY_SECURITY_ID=13 reconstruction, and `instrument_master.py`'s
    lookups are all hardcoded to NIFTY today and would need an
    underlying-aware parameter, not a second copy-pasted module per
    strategy per index (that path already caused real bugs once in this
    project when config got duplicated instead of parameterized -- see
    `detect_support_resistance`'s config-coupling bug fixed 2026-08-03).
  - Bank Nifty's lot size is NOT 65 (NIFTY's) and has changed over time
    on NSE's own schedule -- must be looked up fresh from Dhan/NSE at
    build time, never assumed from memory or copied from NIFTY's
    config.
  - Every existing config file (`config.py`, `config_condor.py`,
    `config_directional_spread.py`, `config_price_action.py`) currently
    encodes NIFTY-specific premium bands, strike spacing, and capital
    assumptions that do NOT automatically transfer to Bank Nifty's
    different price level, strike spacing (100pt, not 50pt), and
    volatility profile -- each strategy needs its own Bank Nifty tuning
    pass, not a shared multiplier.
  - `historical_source.py`'s ATM+/-10 strike-offset cap is a hard API
    limit (see its own docstring) -- Bank Nifty's wider 100pt spacing
    means that same +/-10 offset covers a WIDER points-from-spot window
    than NIFTY's 50pt spacing does, which changes each strategy's
    historical-data coverage math and needs re-deriving per strategy,
    not assumed to carry over unchanged.

Scope this as one design pass across all four strategies before writing
code for any single one -- an underlying-aware `Underlying` config/
security-id abstraction shared by every strategy's data source and
scanner, rather than four independent Bank Nifty forks that could each
drift differently from their own NIFTY counterpart. Sequencing: do this
AFTER the price-action live runner above and NIFTY forward-validation of
the two adopted re-tunings (directional spread's strike selection,
momentum's SCORING_MODE) below -- Bank Nifty adds a second full
evaluation surface on top of strategies whose NIFTY behavior isn't fully
forward-validated yet, and stacking that now would make any surprising
result impossible to attribute to "the strategy" vs. "the new index."

## Forward-validate directional spread's re-tuned strike selection (added 2026-08-02)

`config_directional_spread.py`'s SHORT_PREMIUM_MIN/MAX and
HEDGE_DISTANCE_POINTS changed to 40-70/100 (from 30-60/150) based on
`sweep_spread_config.py`'s 493-day, 2-year grid -- see README.md's
2026-08-02 entry for the full evidence (12 cells, all profitable, new
config has the best return/drawdown ratio in the grid).

Same caveat as the SCORING_MODE entry below: this is an IN-SAMPLE
result. The grid was built from data the sweep also selected from, LTP
fills only (no bid/ask in historical data, and a spread crosses 4
leg-transactions per round trip), and no untouched data remains to check
it against. Watch actual live fills, credit collected, and whether the
2:1 win/loss ratio holds forward before trusting the backtested total.
Revert to the old values (in that file's own comment) if it doesn't.

## Iron condor: no config adopted after 3 sweep rounds / 36 configs tested (closed 2026-08-02)

`sweep_condor_config.py` was run three times against the same 493-day
history, progressively narrowing hedges and raising the premium band in
the direction each round suggested:

  1. Original grid (premium 23-90, hedge 150-300): best cell z=0.27.
  2. Finer grid (premium 50-100, hedge 100-200): best cell z=0.95,
     coverage gap down to 25% (from 64-84%) -- confirmed narrower hedges
     genuinely fix the coverage problem, as hypothesized.
  3. Pushed further (premium 80-150, hedge 50-100): best cell (115-150
     premium, 75-100 hedge) z=1.72, Rs 44,096 total.

Best-ever result (z=1.72) still doesn't clear a plain single-test 95%
bar (1.96), let alone the ~3.2 Bonferroni bar 36 cumulative comparisons
demand. More importantly: the cells that scored best did so by pushing
the premium band to 115-150, which drops win rate to 37.5% (from 70% at
the original config) -- that isn't a better-tuned condor anymore, it's a
qualitatively different, near-the-money strangle. A parameter sweep
optimizing one metric shouldn't be the thing that decides to change what
the strategy fundamentally IS; that's a judgement call, not a tuning one.

CLOSED, not left open-ended: narrowing the coverage gap was confirmed to
work (25% vs the original 64-84%) but did not, by itself, surface a
tradeable edge. Nothing here is adopted; the condor stays at its
original baseline config. Only reopen this with a genuinely new input --
historical data wider than the ATM+/-10 (~500pt) reconstruction cap that
removes the coverage constraint entirely, not another parameter sweep
against the same data.

## Forward-validate SCORING_MODE = "momentum_only" before trusting its size (added 2026-08-02)

Adopted live 2026-08-02 as the default scoring mode -- see config.py's
`SCORING_MODE` docstring and README's 2026-08-02 entry for the full
evidence trail (493-day, 2-year forward-return study; momentum ROC
alignment the only component that survived Bonferroni correction across
41 tested; re-weighted variants backtested with the daily-loss and
exposure gates actually enforced).

This is an IN-SAMPLE result. The variant was designed by studying the
same 493 days it was then tested on, the underlying data has no bid/ask
(LTP fills, so every figure is an optimistic ceiling), and it trades
~5x/day versus the legacy scorer's ~1x/day, which compounds the LTP-fill
optimism roughly proportionally. No untouched data remains to check it
against. Forward (live) results are the only real test left -- until a
meaningful number of live sessions confirm it, treat the backtested
total (+Rs 470,031 / 493 days at Rs 5L capital) as a ceiling, not an
expectation, and watch the daily-loss breaker: the backtest breached it
on 11 of 493 days.

Switching back to `SCORING_MODE = "legacy"` is a one-line, fully-tested,
reversible change (see tests/test_scoring_mode.py) if live results
diverge materially from the backtest.

## Fast position-check: lightweight LTP endpoint (added 2026-07-27)

`main_live.py`'s `check_open_trades_fast()` (runs every
`config.FAST_CHECK_INTERVAL_SECONDS`, 5s by default, to catch a
target/stop spike between full 30s scan cycles) currently re-fetches the
**full option chain** each time — simple, reuses fully-tested code, but
heavier than it needs to be at a fast cadence.

Dhan has a lighter endpoint built for exactly this: `POST
/v2/marketfeed/ltp`, which returns just the LTP for a specific list of
security IDs (see `dhanhq.co/docs/v2/market-quote/`). Switching to it
requires:
1. Downloading and parsing Dhan's instrument master file (maps every
   contract to its security ID) — a new data source we don't currently use.
2. Resolving each open trade's (strike, expiry, option_type) to its
   security ID via that master file.
3. Using those IDs in the `/marketfeed/ltp` call instead of the full
   `/optionchain` fetch.

Worth doing before relying on fast polling with real capital, both for
efficiency and to reduce load against Dhan's rate limits.

## Broker-side protective stop-loss

Discussed at length (see README's "silent freeze" incident notes): even
with `supervisor.py` auto-restarting on crash/freeze, there's a gap
between "the process dies" and "the supervisor notices" where an open
position has zero protection. The only way to close that gap completely
is a stop-loss order sitting on the exchange itself (a Dhan GTT order
placed at trade entry) — a deliberate, not-yet-made decision to start
placing real orders, since everything currently here is analytics/
tracking only.

## Order flow — feed BUILT 2026-08-02, not yet wired into any strategy

`orderflow_feed.py` (WebSocket process), `orderflow_packets.py` (binary
decoder), `orderflow.py` (read side) and `instrument_master.py` are
built, tested and validated against live data — see README's 2026-08-02
entry. **Nothing consumes them yet**: no strategy reads the book, and no
scoring or gating uses it. That wiring is the remaining work, and it is
deliberately separate so the feed can be observed on its own first.

Two things worth resolving before wiring it in:

  - **Measure real intraday spreads.** TOOLING BUILT 2026-08-02
    (`orderflow_recorder.py` records every sample tagged with its market
    phase; `spread_study.py` analyses them), but the measurement itself
    still needs a live session — recording is on by default whenever
    `orderflow_feed.py` runs, so it just needs the feed up during market
    hours.

    Why it matters: the only capture so far is post-close (17:16), median
    **0.856%**, p90 1.591%. That is NOT a usable estimate — spreads widen
    when nobody is quoting, and `spread_study.py` deliberately refuses to
    report a trading-cost figure from non-regular-session samples. But it
    sits above the 0.2–0.6% band assumed in the momentum cost-sensitivity
    analysis, where 0.6% already cut net expectancy from +0.104R to
    ~+0.062R per trade. If regular-session spreads land near 1%, **every
    LTP-priced backtest total in this project is more optimistic than
    currently documented** — momentum_only's Rs 470,031 included. Run the
    feed through one full session and check before trusting those totals.
  - **Decide what the signal is actually for.** Dhan's feed carries the
    order BOOK (resting size), not a trade tape with aggressor flags, so
    cumulative-delta / footprint order flow cannot be built from it. Book
    imbalance measures intent to trade at a price, and resting orders can
    be pulled. Whether that predicts anything here is an open question
    that deserves the same forward-return treatment
    `component_study.py` applied to the momentum scorer — not an
    assumption that "order flow is informative".

## Dhan rate limiting across three concurrent processes (observed live, 2026-07-30)

Real log from a live `main_condor.py` session:

```
[10:57:14] (dhan) NIFTY spot 24350.7
  [data source] dhan failed, falling back: 429 Client Error: Too Many Requests for url: https://api.dhan.co/v2/optionchain
  [data source] nse failed, falling back: 404 Client Error: Not Found for url: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
  Error this cycle (will retry next cycle): 404 Client Error: Not Found for url: https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
```

UPDATE: the original version of this entry conflated two separate
failures. The second one -- MTM reading "unavailable" even on cycles
where the chain fetch clearly succeeded -- turned out to be a real bug
in our own code (`STRIKE_RANGE_POINTS` silently dropping an
already-tracked strike as spot drifted), **fixed** -- see the README's
"Fixed: 2026-07-30" entry. What's left in this backlog item is only the
genuine external-service problem: both tiers occasionally failing
together with no snapshot at all for that cycle. Same root causes,
still diagnosed, not yet built:

**1. Dhan 429 (rate limit). FIXED 2026-07-31.** Confirmed the problem
reached beyond `main_condor.py`: on 2026-07-31 the same rate-limit
storm hit `main_live.py` directly while a real trade was open -- 48 of
72 failure-related log lines that day landed during that one position,
including the 5s fast-check itself failing outright ("Both dhan and
nse sources are in cooldown"). Nothing broke only because that trade
never approached its stop/target during the gaps -- luck, not the
system working as designed. A stop/target check silently not running
during a real approach is a risk to the P&L data this project's whole
measurement effort depends on.

  Built both fix directions:
  - `dhan_rate_limiter.py`: a file-lock-based cross-process request
    spacer. Before every Dhan HTTP call, each of the three processes
    now calls `wait_for_slot()`, which checks a shared state file for
    the wall-clock time of the last Dhan request **by any process**
    and sleeps out the remainder of `MIN_INTERVAL_SECONDS` (3.5s) if
    needed. Uses `os.O_CREAT | os.O_EXCL` for an atomic, portable
    advisory lock; force-clears locks older than `STALE_LOCK_SECONDS`
    (10s) so a crashed owner can't deadlock the other two processes;
    gives up and proceeds without the lock past
    `MAX_ACQUIRE_WAIT_SECONDS` (8s) rather than ever blocking a trading
    loop indefinitely over a coordination mechanism. Wired into all 4
    of `dhan_source.py`'s `requests.post()` call sites. Covered by
    `tests/test_dhan_rate_limiter.py` (spacing enforcement, stale-lock
    clearing, fail-open behavior under contention).
  - `config_directional_spread.py`'s `POLL_INTERVAL_SECONDS` widened
    30 -> 90: this strategy's entry signal is a bias score that doesn't
    move within seconds like the momentum scanner's setups do, so
    there's no accuracy cost, and it cuts this process's share of
    shared Dhan request volume to a third of what it was.

  What this does NOT fix: NSE fallback tier blocking (#2 below) --
  unrelated, external, and not being pursued (see that item).

**2. NSE 404 (fallback tier blocked).** Tested the exact endpoint
directly: the cookie warm-up GET to the NSE homepage itself now returns
403 with zero cookies set, before the API call is even reached. The
"404" body served on the API call is a bot-challenge page, not a
genuine NSE 404. NSE's anti-bot detection has tightened beyond what the
current `requests`-based approach (User-Agent + cookie warm-up) can
satisfy -- likely TLS/JA3 fingerprinting or similar, which `requests`
can't replicate. `nse_source.py`'s own docstring already flagged this
endpoint as unofficial and liable to start blocking without notice.

  Not attempting a code fix that tries harder to look like a browser --
  that starts crossing into bot-detection evasion, which is out of
  scope regardless of urgency. If this tier needs to be reliable, the
  real fix is a licensed/paid data feed, not a better disguise for an
  unofficial endpoint. Until then: when BOTH tiers fail together on an
  open position, that's a real blind spot worth a human glancing at the
  log for, especially over an unattended overnight hold.

## Parameter tuning (lower priority, not safety-critical)

Several thresholds across the codebase are explicitly documented inline
as "starting assumptions, not researched optima" — `config.py`'s
`SMART_MONEY_NEUTRAL_BAND`, `NEWS_RISK_ELEVATED_THRESHOLD`,
`VOLUME_PROFILE_BIN_POINTS`, and `config_condor.py`'s
`HEDGE_DISTANCE_POINTS` among them. Worth revisiting once enough real
sessions have accumulated to tune them against actual data rather than
guesses — see each config comment for the specific reasoning already
documented there.
