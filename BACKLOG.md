# Backlog — before going live with real capital

Things that are working and acceptable during the evaluation/testing
phase, but worth revisiting before real money is on the line.

## Score tie-break: made explicit, measured, NOT changed (added 2026-08-06)

Found analysing the 2026-08-05 session: all four momentum trades opened
deep-OTM PEs hugging the premium floor (11.30, 13.80, 15.90, 19.25).

MECHANISM (confirmed, reproduced against the recorded snapshot): momentum
ROC is a CHAIN-WIDE read, so under SCORING_MODE="momentum_only" every
qualifying strike on a side scores identically -- 14 PE strikes all tied
at exactly 6.0 that cycle. Near-ATM 24550/24600 had far stronger OI
buildup (+343%, +203% vs the winner's +111%) and cheap rather than rich
IV (6th/3rd vs 78th percentile), and none of it could break the tie
because none of it feeds the score. The winner fell to Python's stable
sort preserving CHAIN ORDER, which resolves ASYMMETRICALLY by side:
ascending strike = "cheapest, deepest OTM" for a PE but "most expensive
under PREMIUM_MAX" for a CE.

The asymmetry is REAL and PERSISTENT, not a one-day artifact -- over the
full 6 years, chain_order's median entry is Rs 82.2 for CEs but Rs 30.8
for PEs (2.7x).

MEASURED, 1,491 days / ~10,800 trades per mode:

    mode                gross R    spread R    net R    med entry
    chain_order         +0.1227     0.0118    +0.1108      51.0
    nearest_atm         +0.1171     0.0105    +0.1067      67.4
    highest_oi_change   +0.1079     0.0110    +0.0969      61.8

  Significance vs chain_order: nearest_atm z=-0.26, highest_oi_change
  z=-0.70. NEITHER is significant (bar is 1.96). All three modes are
  statistically indistinguishable.

A spread-cost hypothesis was tested and REJECTED. The 6-year backtest
fills at LTP with no bid/ask, and costs.round_trip deliberately omits
spread (it assumes bid/ask fills already carry it), so spread is charged
nowhere. Real spread measured from a live session (2026-08-05, 37,888
quotes with a live book) is >2x wider on deep-OTM than near-ATM:

    under Rs 20  0.60%     Rs 40-70   0.28%     Rs 120+  0.27%
    Rs 20-40     0.34%     Rs 70-120  0.24%

The expectation was that charging this would penalise chain_order's
deep-OTM bias and flip the ordering. It did not: chain_order's aggregate
median entry is Rs 51, not Rs 11, because the CE/PE asymmetry partly
CANCELS (deep-OTM PEs pay wide spreads, near-ATM CEs pay tight ones).
The between-mode spread difference (0.0013R) is smaller than the
already-insignificant gross difference (0.0056R).

DECISION: default stays "chain_order". There is no evidence to justify
changing it, and the 493-day study that adopted momentum_only ran with
this behaviour. What changed is that the tie-break is now EXPLICIT,
configurable and tested rather than an accident of list order -- and we
now know it does not measurably affect P&L.

OPEN HYPOTHESIS, deliberately NOT adopted: per-side tie-breaks looked
better in both directions (nearest_atm on CEs +0.1401R vs +0.1210R;
chain_order on PEs +0.1242R vs +0.0975R). Both gaps are within noise at
n~5,000, and picking the best of a 2x2 grid on the same data it was
measured on is exactly the in-sample overfitting this project's own
condor entry warns about. Would need out-of-sample or live confirmation
before being taken seriously.

## Historical data corruption is real but confined to far-OTM strikes (added 2026-08-04)

Root-caused after the consistency checker was fixed to flag only
non-persisting spikes (see `historical_consistency.py`). Findings, all
measured rather than assumed:

  - The corruption is genuine. At flagged bars, ADJACENT STRIKES CARRY
    IDENTICAL OI -- e.g. 2024-10-23 11:10, strikes 24050 and 24100 both
    read exactly 33,450 despite different LTP and IV. Two distinct
    contracts cannot share open interest. IV also frequently reads 0.00
    on exactly those bars.
  - It concentrates at the EDGE of the ATM+/-10 offset window that
    `historical_source.py` fetches: 79% of spikes sit within 4 strikes
    of the window edge, and none at all deeper than 8 strikes in.
    Consistent with the offset->strike mapping slipping as spot moves
    across a rounding boundary, which the edge offsets cross most often.
  - Contamination by distance from spot (50 sampled days per dataset):

        band          2024-08..2026-08     2020-08..2024-07
        0-150pts          0.000%               0.000%
        150-250pts        0.000%               0.133%
        250-350pts        0.013%               0.526%
        350+pts           0.293%               1.059%

WHAT THIS MEANS PER STRATEGY (using real live leg distances):
  - Momentum and price-action both select near-the-money inside a
    premium band; their strikes sit within ~150pts of spot, where
    contamination measured ZERO in both datasets. Backtest conclusions
    for these two stand.
  - Directional spread's live legs sat 13pts and 87pts from spot --
    also inside the clean band.
  - IRON CONDOR IS THE EXPOSED ONE. Its live legs sat 37 / 337 / 613 /
    913 pts from spot: three of four are in the contaminated zone, the
    hedges worst of all. Condor backtest results over reconstructed
    history should be treated as unreliable, and this compounds the
    coverage gap already documented for it (its wings routinely fall
    outside the ATM+/-10 window entirely). Worth remembering that 36
    swept condor configs never produced a tradeable edge -- these
    results were partly built on the dirtiest slice of the data.

NOT FIXED, deliberately: the reconstruction is not rewritten to repair
these bars. The clean-band evidence says it isn't needed for three of
four strategies, and the condor has no adopted config to protect. If
the condor is ever revisited seriously, fix the data first -- a
same-strike-adjacent-duplicate detector could repair or drop the
affected bars rather than pass them through silently.

## Verify whether Dhan exposes a real settlement price (added 2026-08-04)

2026-08-04 incident: condor and directional spread both had their own
contract expiry that day and both sat unsettled past close, traced to
two compounding issues -- see `main_condor.py`'s `EXPIRY_SETTLEMENT_CUTOFF`
comment and `tests/test_expiry_settlement_timing.py` for the full
evidence trail. Fixed for now: settlement fires at 15:40 (NSE's real
F&O close, confirmed against a published schedule) instead of waiting
for the nominal 15:30 `market_is_open()` transition, using the last
available LTP at that point.

That "last available LTP" is still a proxy, not verified to be the best
one. Checked directly: option premiums in the 15:15-15:40 window swing
hard (one strike moved 85% in 11 minutes on 2026-08-04), so whichever
single LTP snapshot happens to land closest to 15:40 could still be an
outlier rather than a true closing/settlement price. Dhan's option-chain
response was not inspected field-by-field for a dedicated settlement-
price field distinct from `last_price` -- attempted 2026-08-04 evening
but the live `/v2/optionchain/expirylist` endpoint only responds during
market hours, so this returned a 401 unrelated to the token itself.

Next live session: probe the raw Dhan option-chain JSON response
(`dhan_source._fetch_raw_chain`) for any field beyond `last_price` that
might carry a real exchange-published settlement value, and switch
expiry settlement to use it if one exists.

## Forward-validate the pure price-action live runner (added 2026-08-04, updated same day)

`main_price_action.py` was built 2026-08-04, ahead of this project's own
usual bar ("build once the backtest edge holds without an outlier, or a
larger sample confirms it" -- see the original version of this entry
below the strikethrough). Built at the user's explicit request despite
that caveat, not because the 2-year backtest cleared it (19-24 distinct
setups, 26% drawdown, one outlier trade for ~half the P&L).

UPDATE 2026-08-04, same day: the backfill was extended to the real
6-year depth (2020-08-03..2026-08-04, 1,490 days -- see the historical-
data-corruption entry above) and the sweep re-run. Full results, both
pairs, 1 lot, R:R 1.0-4.0:

    daily_hourly: n=140-225 per cell, win 32.9-50.2%, expectancy
      +0.06 to +0.44R, total Rs 1,459-38,065 (rises with R:R)
    intraday:     n=88-108 per cell, win 44.3-68.5%, expectancy
      +0.62 to +0.94R, total Rs 63,087-96,167 (rises with R:R)

Investigated the "outlier-dependent" concern directly rather than
re-assert it: intraday@1:3's 9 weekend-gap trades (opened Friday
afternoon, resolved Monday) account for 62% of that cell's total. But
checked whether those fills were real or backtest artifacts by looking
at price action for the 30 minutes AFTER each recorded fill -- 7 of 9
PERSISTED (the price held near the fill level, including the actual
2024-08-05 NIFTY selloff), only 2 of 9 showed any reversion, and both
of those were small. Conclusion: carrying a stop/target position across
a weekend is not a bug in either pair (neither has a same-day-exit rule
in the code, and none should be added -- an options market that doesn't
trade over the weekend legitimately resolves at the next available
price, gap and all). The 6-year numbers are trusted as reported, not
provisional on stripping outliers.

Both pairs positive at every R:R tested, both consistent in shape with
the earlier 2-year read (intraday stronger per-trade, daily_hourly more
frequent). Sample size grew roughly proportionally with the 3x longer
window (497->1,490 days, 19-24->88-225 trades per cell), which is
itself mild evidence the edge isn't a fluke of the shorter window.

Still open before trusting live output further:
  1. Watch live/paper results accumulate for real -- even 88-225
     historical trades per cell is a backtest sample, not a live one,
     and this project's own standard is to prefer live confirmation
     over any amount of backtesting.
  2. See the historical-data-corruption entry above: momentum and
     price-action both trade near-ATM (inside the zero-contamination
     band), so this sweep is NOT exposed to the far-OTM data issue that
     compromises the condor's numbers.

`config_price_action.py` carries TOTAL_CAPITAL=50,000, MAX_LOTS_PER_TRADE=1.
MAX_RISK_PER_TRADE_PCT is 15% (not momentum's 1%) -- with lots hard-
capped at 1, this constant only ever GATES whether a single lot is
affordable, never scales size up, and at 1% it silently sized every
signal to 0 lots against this strategy's own PREMIUM_MIN/MAX band (see
that constant's own comment; caught by
`test_forced_signal_opens_a_tracked_position_via_auto_approve` before
it could ship as a strategy that looks alive but never actually trades).

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
